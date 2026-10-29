"""Exercise the extracted Linux executable, with mock-only loopback HTTP and ephemeral auth."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from release_linux import read_tar, verify_archive


def stop_owned(process: subprocess.Popen) -> None:
    # PyInstaller onefile has a bootloader parent that forwards SIGTERM to its child.
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def invoke(exe: Path, args: list[str], cwd: Path, env: dict[str, str]) -> str:
    process = subprocess.Popen(
        [str(exe), *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
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


def refuses(exe: Path, command: str, config: Path, folder: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(exe), command, "--config", str(config)],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    if result.returncode != 1:
        raise RuntimeError(f"packaged_{command}_check_failed")
    return result.stdout


def smoke(archive: Path, ref: str, commit: str) -> None:
    manifest = verify_archive(archive, ref, commit)
    with tempfile.TemporaryDirectory(prefix="cardputer-linux-smoke-") as temporary:
        folder = Path(temporary)
        bundle = folder / "bundle"
        bundle.mkdir()
        for name, data in read_tar(archive).items():
            # read_tar has already required an exact, flat-or-examples member set.
            target = bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        exe = bundle / "cardputer"
        exe.chmod(0o755)
        # No source tree or Python on PATH: clients are executable processes.
        env = {key: value for key, value in os.environ.items() if key in {"TMPDIR", "LANG"}}
        env["HOME"] = str(folder / "home")
        env["XDG_STATE_HOME"] = str(folder / "xdg")
        assert "serve" in invoke(exe, ["--help"], folder, env)
        assert invoke(exe, ["--version"], folder, env).strip() == manifest["version"]
        assert "launch-console" in invoke(
            exe, ["-m", "cardputer_mcp.tunnel", "--help"], folder, env
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = folder / "mock.toml"
        config.write_text(
            f'backend = "mock"\nhost = "127.0.0.1"\nport = {port}\n'
            f"state_dir = {json.dumps(str(folder / 'state'))}\n"
            "allow_mock_execution = false\n",
            encoding="utf-8",
        )
        # The frozen gateway and web UI import their stacks, then refuse a TOML
        # without their table; neither listens nor starts Tailscale here.
        if refuses(exe, "gateway", config, folder, env) != "gateway_config_missing\n":
            raise RuntimeError("packaged_gateway_check_failed")
        if refuses(exe, "webui", config, folder, env) != "webui_config_missing\n":
            raise RuntimeError("packaged_webui_check_failed")
        env["CARDPUTER_LOCAL_TOKEN"] = secrets.token_hex(32)
        env["CARDPUTER_REMOTE_TOKEN"] = secrets.token_hex(32)
        process = subprocess.Popen(
            [str(exe), "--auth-source", "environment", "serve", "--config", str(config)],
            cwd=folder,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            client = ["--url", f"http://127.0.0.1:{port}/mcp", "--auth-source", "environment"]
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
            # Authentication must remain enforced in the frozen broker.
            try:
                invoke(exe, ["--url", f"http://127.0.0.1:{port}/mcp", "status"], folder, env)
            except RuntimeError as error:
                assert str(error) == "packaged_command_failed"
            else:
                raise AssertionError("packaged_broker_accepted_anonymous_client")
            serialized = json.dumps(status) + json.dumps(logs)
            assert all(
                env[key] not in serialized
                for key in ("CARDPUTER_LOCAL_TOKEN", "CARDPUTER_REMOTE_TOKEN")
            )
        finally:
            try:
                stop_owned(process)
            finally:
                env.clear()
        with socket.socket() as check:
            assert check.connect_ex(("127.0.0.1", port)) != 0
    print(
        "PASS: archive allowlist/hashes/version; executable help/version/Tunnel dispatch; mock HTTP"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    smoke(args.archive, args.ref, args.commit)
