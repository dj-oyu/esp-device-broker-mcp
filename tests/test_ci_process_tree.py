"""CI helper tests start only synthetic stdlib children, never broker/device work."""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ci_process_tree", Path(__file__).resolve().parents[1] / "packaging" / "ci_process_tree.py"
)
assert _SPEC is not None and _SPEC.loader is not None
tree_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tree_module)
ProcessTree, ProcessTreeError = tree_module.ProcessTree, tree_module.ProcessTreeError

_GRANDCHILD = """
import pathlib, sys, time
marker = pathlib.Path(sys.argv[1])
n = 0
while True:
    marker.write_text(str(n), encoding='ascii')
    n += 1
    time.sleep(0.02)
"""
_LEADER = """
import pathlib, subprocess, sys, time
pathlib.Path(sys.argv[1]).write_text('gated', encoding='ascii')
if sys.stdin.buffer.read(1) != b'1':
    raise SystemExit(9)
child = subprocess.Popen([sys.executable, '-I', '-S', '-c', sys.argv[4], sys.argv[2]])
pathlib.Path(sys.argv[3]).write_text(str(child.pid), encoding='ascii')
if sys.argv[5] != 'live':
    raise SystemExit(int(sys.argv[5]))
time.sleep(60)
"""


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("synthetic child did not reach expected checkpoint")
        time.sleep(0.01)


@pytest.mark.parametrize("mode", ["live", "0", "7"])
def test_real_owned_tree_stops_grandchild_even_after_leader_exit(tmp_path, mode):
    tree = ProcessTree()
    ready, heartbeat, pidfile = (tmp_path / name for name in ("gate", "heartbeat", "pid"))
    process = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _LEADER,
                str(ready),
                str(heartbeat),
                str(pidfile),
                _GRANDCHILD,
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
        )
        wait_for(ready.exists)
        assert not heartbeat.exists() and not pidfile.exists()
        tree.attach(process)
        assert process.stdin is not None
        process.stdin.write(b"1")
        process.stdin.close()
        wait_for(lambda: heartbeat.exists() and heartbeat.stat().st_size > 0 and pidfile.exists())
        if mode != "live":
            assert process.wait(timeout=4) == int(mode)
        else:
            assert process.poll() is None
        started = time.monotonic()
        assert tree.close(process) is True
        assert time.monotonic() - started < 10
        assert process.poll() is not None
        stopped_at = heartbeat.read_bytes()
        time.sleep(0.15)
        assert heartbeat.read_bytes() == stopped_at
        assert tree.close(process) is True
    finally:
        tree.close(process)
        if process is not None and process.stdin is not None:
            process.stdin.close()


def test_close_empty_owner_after_popen_failure():
    tree = ProcessTree()
    assert tree.close(None) is False
    assert tree.close(None) is False


@pytest.mark.parametrize("exit_before_close", [False, True])
def test_real_leader_without_descendants_is_not_reported_as_a_leak(exit_before_close):
    tree = ProcessTree()
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    try:
        tree.attach(process)
        if exit_before_close:
            process.stdin.write(b"1")
            process.stdin.close()
            assert process.wait(timeout=4) == 0
        assert tree.close(process) is False and process.poll() is not None
    finally:
        tree.close(process)
        process.stdin.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX session boundary")
def test_attach_failure_keeps_gate_closed_and_direct_child_is_stopped(tmp_path):
    tree = ProcessTree()
    marker = tmp_path / "not-started"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import sys, pathlib; sys.stdin.buffer.read(1); pathlib.Path(sys.argv[1]).touch()",
            str(marker),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(ProcessTreeError, match="^ci_process_tree_attach_failed$"):
            tree.attach(process)
        assert tree.close(process) is False
        assert process.poll() is not None and not marker.exists()
    finally:
        tree.close(process)
        process.stdin.close()


class Function:
    def __init__(self, action):
        self.action = action

    def __call__(self, *args):
        return self.action(*args)


class JobAPI:
    def __init__(self):
        self.count = 1
        self.calls = []
        self.fail_assign = self.fail_query = self.fail_terminate = False
        self._kernel = self
        self.OpenProcess = Function(self.open)
        self.AssignProcessToJobObject = Function(self.assign)

    def create_job(self):
        self.calls.append("create")
        return 10

    def configure_job(self, job):
        assert job == 10
        self.calls.append("kill_on_close")

    def open(self, rights, inherit, pid):
        assert rights == 0x101 and inherit is False and pid == 123
        self.calls.append("open")
        return 20

    def assign(self, job, handle):
        assert job == 10 and handle == 20
        self.calls.append("assign")
        return not self.fail_assign

    def close_handle(self, handle):
        self.calls.append(("close", handle))

    def active_count(self, job):
        assert job == 10
        if self.fail_query:
            raise OSError("SYNTHETIC_PRIVATE_QUERY")
        self.calls.append(("count", self.count))
        return self.count

    def terminate_job(self, job):
        assert job == 10
        self.calls.append("terminate")
        if self.fail_terminate:
            raise OSError("SYNTHETIC_PRIVATE_TERMINATE")
        self.count = 0


class Process:
    pid = 123

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -1

    def wait(self, timeout):
        assert timeout > 0
        self.returncode = -1 if self.returncode is None else self.returncode
        return self.returncode


def windows_tree(monkeypatch):
    api = JobAPI()
    monkeypatch.setattr(tree_module, "_WINDOWS", True)
    monkeypatch.setattr(tree_module, "NativeWindowsAPI", lambda: api)
    return tree_module.ProcessTree(), api


def test_windows_assignment_precedes_release_and_accounting_outlives_leader(monkeypatch):
    tree, api = windows_tree(monkeypatch)
    process = Process()
    tree.attach(process)
    assert api.calls == ["create", "kill_on_close", "open", "assign", ("close", 20)]
    process.returncode = 7  # Leader has exited; one job member is still live.
    assert tree.close(process) is True
    assert api.calls[-3:] == ["terminate", ("count", 0), ("close", 10)]


@pytest.mark.parametrize("failure", ["query", "terminate", "active_timeout"])
def test_windows_failed_cleanup_retains_job_and_fixed_error(monkeypatch, failure):
    tree, api = windows_tree(monkeypatch)
    process = Process()
    tree.attach(process)
    api.fail_query = failure == "query"
    api.fail_terminate = failure == "terminate"
    if failure == "active_timeout":
        api.terminate_job = lambda job: None
        monkeypatch.setattr(tree_module, "_CLEANUP_SECONDS", 0.03)
    with pytest.raises(ProcessTreeError, match="^ci_process_tree_cleanup_failed$") as caught:
        tree.close(process)
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert ("close", 10) not in api.calls and tree._job == 10
    api.fail_query = api.fail_terminate = False
    api.count = 0
    assert tree.close(process) is False


def test_windows_failed_assignment_closes_temporary_handle_and_gated_child(monkeypatch):
    tree, api = windows_tree(monkeypatch)
    process = Process()
    api.fail_assign = True
    with pytest.raises(ProcessTreeError, match="^ci_process_tree_attach_failed$"):
        tree.attach(process)
    assert ("close", 20) in api.calls
    api.count = 0  # The failed assignment left an empty job, with child gated.
    assert tree.close(process) is False and process.poll() is not None
    assert api.calls[-1] == ("close", 10)
