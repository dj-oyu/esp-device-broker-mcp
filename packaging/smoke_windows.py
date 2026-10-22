"""Exercise the extracted EXE, with mock-only loopback HTTP and ephemeral auth."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from release_windows import verify_archive


def stop_owned(process: subprocess.Popen) -> None:
    if process.poll() is None:
        if os.name == "nt":
            # PyInstaller onefile has a bootloader parent and application child.
            # Target only this still-owned process tree, never search by name.
            result = subprocess.run(
                [str(Path(os.environ["SYSTEMROOT"]) / "System32/taskkill.exe"),
                 "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=15, check=False,
            )
            if result.returncode != 0 and process.poll() is None:
                raise RuntimeError("owned_smoke_process_cleanup_failed")
        else:
            process.terminate()
    process.wait(timeout=15)


def invoke(exe: Path, args: list[str], cwd: Path, env: dict[str, str]) -> str:
    process = subprocess.Popen(
        [str(exe), *args], cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        stdout, _ = process.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        stop_owned(process)
        process.communicate(timeout=15)
        raise RuntimeError("packaged_command_timeout") from None
    if process.returncode != 0:
        # Never publish process output: a future auth regression could echo it.
        raise RuntimeError("packaged_command_failed")
    return stdout



def frozen_build_api_smoke(url: str, cwd: Path, env: dict[str, str]) -> None:
    """The official SDK client probes the running extracted EXE, never a Python broker."""
    client_python = Path(__file__).resolve().parents[1] / ".venv/Scripts/python.exe"
    if not client_python.is_file():
        raise RuntimeError("packaged_sdk_probe_python_missing")
    script = """
import asyncio
import json
import os
from cardputer_mcp.client import call, connect

phase = 10

async def probe():
    global phase
    async with connect(os.environ['CARDPUTER_SMOKE_URL'],
                       os.environ['CARDPUTER_REMOTE_TOKEN']) as client:
        phase = 11
        catalog = await client.list_tools()
        names = {tool.name for tool in catalog.tools}
        assert {'prepare_test', 'execute_test', 'read_build_logs'} <= names
        phase = 12
        prepared_tool = next(tool for tool in catalog.tools if tool.name == 'prepare_test')
        assert 'firmware_build' in json.dumps(prepared_tool.model_dump())
        logs_tool = next(tool for tool in catalog.tools if tool.name == 'read_build_logs')
        assert logs_tool.annotations.read_only_hint is True
        phase = 13
        identity = await call(client, 'client_identity')
        assert identity['ok'] is True and identity['authenticated'] is True
        assert identity['origin'] == 'remote_mcp'
        phase = 14
        logs = await call(client, 'read_build_logs', {'job_id': 'unstarted-smoke'})
        assert logs['ok'] is False and logs['error'] == 'build_log_not_available'
        phase = 15
        assert 'session' not in prepared_tool.input_schema.get('required', [])
        result = await call(client, 'prepare_test', {
            'job_id': 'build-api-smoke', 'test': 'firmware_build',
            'project_id': 'unregistered-smoke',
        })
        assert result['ok'] is False
        assert result['error'] == 'registered_build_project_required'

async def main():
    async with asyncio.timeout(30):
        await probe()
try:
    asyncio.run(main())
except Exception:
    raise SystemExit(phase)
"""
    child_env = dict(env)
    child_env["CARDPUTER_SMOKE_URL"] = url
    try:
        completed = subprocess.run(
            [str(client_python), "-c", script], cwd=cwd, env=child_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=45, check=False,
        )
        if completed.returncode != 0:
            phase = {
                10: "connect", 11: "catalog", 12: "schema", 13: "identity",
                14: "logs", 15: "registered_project",
            }.get(completed.returncode, "unknown")
            raise RuntimeError("packaged_build_api_probe_failed_at_" + phase)
    finally:
        child_env.clear()
    print("PASS: extracted EXE authenticated catalog/build/log API boundaries; no build executed")

def frozen_registry_smoke(exe: Path, url: str, folder: Path, env: dict) -> None:
    from cardputer_mcp.key_registry import KeyRegistry
    from cardputer_mcp.models import Origin
    from cardputer_mcp.registry_defaults import prepare_default_key_directory

    path = Path(env["LOCALAPPDATA"]) / "DeviceBroker" / "keys.sqlite3"
    prepare_default_key_directory(path)
    registry = KeyRegistry(path, random_bytes=lambda size: b"\x17" * size)
    key_env = dict(env)
    try:
        issued = registry.issue("synthetic packaged reader", Origin.REMOTE)
        key_env["CARDPUTER_CLIENT_KEY"] = issued.token
        arguments = ["--url", url, "--auth-source", "key-environment", "status"]
        assert json.loads(invoke(exe, arguments, folder, key_env))["ok"] is True
        registry.revoke(issued.metadata.key_id)
        try:
            invoke(exe, arguments, folder, key_env)
        except RuntimeError as error:
            assert str(error) == "packaged_command_failed"
        else:
            raise AssertionError("packaged_broker_accepted_revoked_default_key")
    finally:
        registry.close()
        key_env.clear()
    print("PASS: extracted EXE default private registry hot issue/revoke without restart")


def smoke(archive: Path, ref: str, commit: str) -> None:
    manifest = verify_archive(archive, ref, commit)
    with tempfile.TemporaryDirectory(prefix="cardputer-exe-smoke-") as temporary, \
         tempfile.TemporaryDirectory(prefix="cardputer-registry-smoke-",
                                     dir=os.environ["LOCALAPPDATA"]) as private_state:
        folder = Path(temporary)
        bundle = folder / "bundle"
        with zipfile.ZipFile(archive) as handle:
            # verify_archive has already required an exact, safe member set.
            handle.extractall(bundle)
        exe = bundle / "cardputer.exe"
        # Python and source files are absent from this cwd; clients are EXE processes.
        env = {key: value for key, value in os.environ.items()
               if key.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMSPEC"}}
        assert "serve" in invoke(exe, ["--help"], folder, env)
        assert invoke(exe, ["--version"], folder, env).strip() == manifest["version"]
        # Cover the fixed re-exec route without starting a native Tunnel client.
        assert "launch-console" in invoke(
            exe, ["-m", "cardputer_mcp.tunnel", "--help"], folder, env
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = folder / "mock.toml"
        config.write_text(
            f'backend = "mock"\nhost = "127.0.0.1"\nport = {port}\n'
            f'state_dir = {json.dumps(str(folder / "state"))}\n'
            'allow_mock_execution = false\n', encoding="utf-8",
        )
        env["LOCALAPPDATA"] = private_state
        env["CARDPUTER_LOCAL_TOKEN"] = secrets.token_hex(32)
        env["CARDPUTER_REMOTE_TOKEN"] = secrets.token_hex(32)
        process = subprocess.Popen(
            [str(exe), "--auth-source", "environment", "serve",
             "--config", str(config)], cwd=folder, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            client = ["--url", f"http://127.0.0.1:{port}/mcp",
                      "--auth-source", "environment"]
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError("packaged_mock_broker_exited")
                try:
                    status = json.loads(invoke(exe, [*client, "status"], folder, env))
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("packaged_mock_readiness_timeout") from None
                    time.sleep(0.2)
            assert status["ok"] is True
            assert not (Path(private_state) / "DeviceBroker/keys.sqlite3").exists()
            assert status["backend"] == "mock"
            assert status["hardware_connected"] is False
            assert status["active_job"] is None
            logs = json.loads(invoke(exe, [*client, "logs"], folder, env))
            assert logs["ok"] is True and logs["gap"] is False
            frozen_registry_smoke(exe, f"http://127.0.0.1:{port}/mcp", folder, env)
            assert json.loads(invoke(exe, [*client, "status"], folder, env))["ok"] is True
            frozen_build_api_smoke(f"http://127.0.0.1:{port}/mcp", folder, env)
            # Exercise the packaged OS-identity request client and the two fixed
            # privileged CLI actions, with dummy tokens and one synthetic capture.
            launch_probe = Path(__file__).with_name("operator_launch_smoke.py")
            launch_python = Path(__file__).resolve().parents[1] / ".venv/Scripts/python.exe"
            launch_result = subprocess.run(
                [str(launch_python), str(launch_probe), "--exe", str(exe),
                 "--url", f"http://127.0.0.1:{port}/mcp", "--cwd", str(folder)],
                cwd=folder, env=dict(env), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=90, check=False,
            )
            if launch_result.returncode != 0:
                raise RuntimeError("packaged_operator_launch_probe_failed")
            # Authentication must remain enforced in the frozen broker.
            try:
                invoke(exe, ["--url", f"http://127.0.0.1:{port}/mcp", "status"], folder, env)
            except RuntimeError as error:
                assert str(error) == "packaged_command_failed"
            else:
                raise AssertionError("packaged_broker_accepted_anonymous_client")
            serialized = json.dumps(status) + json.dumps(logs)
            assert all(env[key] not in serialized for key in
                       ("CARDPUTER_LOCAL_TOKEN", "CARDPUTER_REMOTE_TOKEN"))
        finally:
            try:
                stop_owned(process)
            finally:
                env.clear()
        with socket.socket() as check:
            assert check.connect_ex(("127.0.0.1", port)) != 0
    print("PASS: ZIP allowlist/hashes/version; EXE help/version/Tunnel dispatch; authenticated mock HTTP")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        from check_receipt_source import check
        check()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    smoke(args.archive, args.ref, args.commit)
