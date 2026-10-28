"""Linux start scripts, run under bash against a stub `cardputer`; no broker, USB or network."""

import os
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "start-cardputer.sh"
STACK = ROOT / "start-cardputer-stack.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    BASH is None
    or (sys.platform == "win32" and not os.environ.get("CARDPUTER_RUN_LAUNCHER_TESTS")),
    reason="needs a POSIX bash (set CARDPUTER_RUN_LAUNCHER_TESTS=1 to try Git Bash on Windows)",
)

# Stands in for the packaged executable. STUB_MODE: ok (default), exit, busy.
STUB = r"""#!/usr/bin/env bash
mode=${STUB_MODE:-ok}
sub=""
for arg in "$@"; do
    case $arg in serve | status | gateway | webui) sub=$arg; break ;; esac
done
case $sub in
    serve)
        printf '%s\n' "$*" >"$STUB_STATE/serve.args"
        pwd -P >"$STUB_STATE/serve.cwd"
        echo "${#CARDPUTER_LOCAL_TOKEN} ${#CARDPUTER_REMOTE_TOKEN}" >"$STUB_STATE/serve.tokens"
        echo $$ >"$STUB_STATE/serve.pid"
        if [ "$mode" = exit ]; then echo 'serial_runtime_opt_in_required'; exit 1; fi
        : >"$STUB_STATE/ready"
        trap 'exit 0' TERM
        while :; do sleep 0.2; done
        ;;
    status)
        [ -n "$CARDPUTER_LOCAL_TOKEN" ] && [ -e "$STUB_STATE/ready" ] || exit 1
        job=null
        [ "$mode" = busy ] && job='{"job_id": "j"}'
        printf '{\n  "active_job": %s,\n  "backend": "mock",\n' "$job"
        printf '  "hardware_connected": false,\n  "ok": true\n}\n'
        ;;
    gateway | webui)
        echo "$(env | grep -c -E '^(CARDPUTER|OPENAI)_') $*" >"$STUB_STATE/$sub.seen"
        echo "${sub}_config_missing"
        ;;
esac
"""


def base_env():
    # Git Bash needs SYSTEMROOT/WINDIR to use sockets; they are absent on Linux.
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
    return {key: os.environ[key] for key in keep if key in os.environ}


def q(path):
    """Shell-quoted POSIX spelling, so Windows paths survive Git Bash."""
    return shlex.quote(Path(path).as_posix())


@pytest.fixture
def setup(tmp_path):
    exe = tmp_path / "cardputer"
    exe.write_text(STUB)
    exe.chmod(0o755)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    profile = profile_dir / "device.local.toml"
    profile.write_text('backend = "mock"\n')
    state = tmp_path / "stub"
    state.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    yield {
        "exe": exe,
        "profile": profile,
        "state": state,
        "logs": tmp_path / "logs",
        "port": port,
        "url": f"http://127.0.0.1:{port}/mcp",
    }
    pid = state / "serve.pid"
    if pid.exists():
        subprocess.run(["kill", pid.read_text().strip()], capture_output=True, check=False)


def run(setup, script, *args, mode="ok", pre="", post="", extra_env=None):
    command = (
        f"{pre}\n. {q(script)} --exe {q(setup['exe'])} --profile {q(setup['profile'])} "
        f"--url {setup['url']} --log-dir {q(setup['logs'])} {' '.join(args)}\n"
        f'rc=$?\necho "rc=$rc"\n{post}\n'
        'echo "tokens=${#CARDPUTER_LOCAL_TOKEN},${#CARDPUTER_REMOTE_TOKEN}"\n'
        '[ -z "${CARDPUTER_BROKER_PID-}" ] || kill "$CARDPUTER_BROKER_PID" 2>/dev/null\n'
    )
    env = {
        **base_env(),
        "HOME": str(setup["state"]),
        "STUB_STATE": str(setup["state"]),
        "STUB_MODE": mode,
        **(extra_env or {}),
    }
    return subprocess.run(
        [BASH, "-c", command], env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_scripts_parse():
    for script in (BROKER, STACK):
        assert subprocess.run([BASH, "-n", str(script)], check=False).returncode == 0


@pytest.mark.parametrize("script", [BROKER, STACK])
def test_running_instead_of_sourcing_is_refused(script):
    result = subprocess.run([BASH, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "Source this script" in result.stderr


def test_ready_broker_keeps_tokens_in_this_shell_and_out_of_argv(setup):
    result = run(setup, BROKER)
    assert "rc=0" in result.stdout and "Ready: authenticated mock broker is idle" in result.stdout
    assert "tokens=64,64" in result.stdout
    args = (setup["state"] / "serve.args").read_text()
    assert "--auth-source environment serve --config" in args
    assert "--require-auth" in args and "--enable-serial" in args
    assert (setup["state"] / "serve.tokens").read_text().split() == ["64", "64"]
    # Compared by name: Git Bash spells the temporary folder differently from Windows.
    assert (setup["state"] / "serve.cwd").read_text().strip().endswith("/profile")
    assert (setup["logs"] / "broker.log").is_file()
    # The tokens are never printed.
    assert "CARDPUTER_LOCAL_TOKEN=" not in result.stdout + result.stderr


def test_no_serial_leaves_out_enable_serial(setup):
    result = run(setup, BROKER, "--no-serial")
    assert "rc=0" in result.stdout
    assert "--enable-serial" not in (setup["state"] / "serve.args").read_text()


def test_existing_listener_is_refused_before_any_credential_exists(setup):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", setup["port"]))
        listener.listen()
        result = run(setup, BROKER)
    assert "rc=1" in result.stdout and "listener_exists" in result.stderr
    assert "tokens=0,0" in result.stdout
    assert not (setup["state"] / "serve.args").exists()


def test_failed_start_restores_the_previous_tokens(setup):
    result = run(
        setup,
        BROKER,
        mode="exit",
        pre="export CARDPUTER_LOCAL_TOKEN=old CARDPUTER_REMOTE_TOKEN=older",
        post='echo "now=$CARDPUTER_LOCAL_TOKEN,$CARDPUTER_REMOTE_TOKEN"',
    )
    assert "rc=1" in result.stdout and "broker_exited" in result.stderr
    assert "serial_runtime_opt_in_required" in result.stderr  # last log lines are shown
    assert "now=old,older" in result.stdout


def test_busy_broker_is_not_reported_ready(setup):
    result = run(setup, BROKER, "--ready-timeout", "10", mode="busy")
    assert "rc=1" in result.stdout and "broker_not_idle" in result.stderr
    assert "tokens=0,0" in result.stdout


@pytest.mark.parametrize(
    "bad",
    [
        ["--url", "http://example.com:8765/mcp"],
        ["--ready-timeout", "5"],
        ["--bogus"],
    ],
)
def test_bad_options_are_refused_before_starting(setup, bad):
    command = (
        f". {q(BROKER)} --exe {q(setup['exe'])} --profile {q(setup['profile'])} {' '.join(bad)}"
    )
    env = {**base_env(), "HOME": str(setup["state"]), "STUB_STATE": str(setup["state"])}
    result = subprocess.run(
        [BASH, "-c", command + "\necho rc=$?"], env=env, capture_output=True, text=True, check=False
    )
    assert "rc=2" in result.stdout
    assert not (setup["state"] / "serve.args").exists()


def test_stack_starts_helpers_without_any_credentials(setup):
    result = run(
        setup,
        STACK,
        extra_env={"OPENAI_API_KEY": "must-not-leak", "CARDPUTER_TUNNEL_API_KEY": "nor-this"},
    )
    assert "rc=0" in result.stdout
    for helper in ("gateway", "webui"):
        seen = (setup["state"] / f"{helper}.seen").read_text()
        assert seen.startswith("0 "), seen  # no CARDPUTER_* or OPENAI_* variables
        assert f"{helper} --config" in seen
    assert "must-not-leak" not in result.stdout + result.stderr
