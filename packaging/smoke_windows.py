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

async def probe():
    async with connect(os.environ['CARDPUTER_SMOKE_URL'],
                       os.environ['CARDPUTER_REMOTE_TOKEN']) as client:
        catalog = await client.list_tools()
        names = {tool.name for tool in catalog.tools}
        assert {'prepare_test', 'execute_test', 'read_build_logs'} <= names
        prepared_tool = next(tool for tool in catalog.tools if tool.name == 'prepare_test')
        assert 'firmware_build' in json.dumps(prepared_tool.model_dump())
        logs_tool = next(tool for tool in catalog.tools if tool.name == 'read_build_logs')
        assert logs_tool.annotations.read_only_hint is True
        identity = await call(client, 'client_identity')
        assert identity['ok'] is True and identity['authenticated'] is True
        assert identity['origin'] == 'remote_mcp'
        logs = await call(client, 'read_build_logs', {'job_id': 'unstarted-smoke'})
        assert logs['ok'] is False and logs['error'] == 'build_log_not_available'
        acquired = await call(client, 'acquire_session', {'owner': 'remote_mcp'})
        assert acquired['ok'] is True
        try:
            result = await call(client, 'prepare_test', {
                'session': acquired['session'], 'job_id': 'build-api-smoke',
                'test': 'firmware_build', 'project_id': 'unregistered-smoke',
            })
            assert result['ok'] is False and result['error'] == 'backend_preflight_failed'
        finally:
            released = await call(client, 'release_session', {'session': acquired['session']})
            assert released['ok'] is True

async def main():
    async with asyncio.timeout(30):
        await probe()
asyncio.run(main())
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
            raise RuntimeError("packaged_build_api_probe_failed")
    finally:
        child_env.clear()
    print("PASS: extracted EXE authenticated catalog/build/log API boundaries; no build executed")

def smoke(archive: Path, ref: str, commit: str) -> None:
    manifest = verify_archive(archive, ref, commit)
    with tempfile.TemporaryDirectory(prefix="cardputer-exe-smoke-") as temporary:
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
        env["CARDPUTER_LOCAL_TOKEN"] = secrets.token_hex(32)
        env["CARDPUTER_REMOTE_TOKEN"] = secrets.token_hex(32)
        process = subprocess.Popen(
            [str(exe), "--auth-source", "environment", "serve", "--require-auth",
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
            assert status["backend"] == "mock"
            assert status["hardware_connected"] is False
            assert status["active_job"] is None
            logs = json.loads(invoke(exe, [*client, "logs"], folder, env))
            assert logs["ok"] is True and logs["gap"] is False
            frozen_build_api_smoke(f"http://127.0.0.1:{port}/mcp", folder, env)
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
