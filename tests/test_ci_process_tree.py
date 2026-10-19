"""CI helper tests start only synthetic stdlib children, never broker/device work."""

import ctypes
import importlib.util
import json
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
import os, pathlib, sys, time
marker = pathlib.Path(sys.argv[1])
pathlib.Path(sys.argv[2]).write_text(str(os.getpid()), encoding='ascii')
n = 0
while True:
    marker.write_text(str(n), encoding='ascii')
    n += 1
    time.sleep(0.02)
"""
_LEADER = """
import os, pathlib, subprocess, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')
if sys.stdin.buffer.read(1) != b'1':
    raise SystemExit(9)
child = subprocess.Popen([sys.executable, '-I', '-S', '-c', sys.argv[4], sys.argv[2], sys.argv[6]])
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


def assert_native_member(tree, pid):
    if os.name != "nt":
        return
    kernel = tree._api._kernel
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    kernel.IsProcessInJob.restype = ctypes.c_int32
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION.
    assert handle, "synthetic_pid_open_failed"
    member = ctypes.c_int32()
    try:
        assert kernel.IsProcessInJob(handle, tree._job, ctypes.byref(member)), "job_query_failed"
        assert member.value == 1, "synthetic_pid_outside_owned_job"
    finally:
        tree._api.close_handle(int(handle))


def native_cleanup_probe(tree, process):
    """Diagnostic-only snapshot, independent of the helper's classification.

    Only numeric metadata from this owned job is retained. Never enumerate host
    processes, read paths/arguments/environment, or keep temporary handles alive
    across the actual close call, which would perturb accounting.
    """
    if os.name != "nt":
        return {}

    class JobPids(ctypes.Structure):
        _fields_ = [
            ("assigned", ctypes.c_uint32),
            ("listed", ctypes.c_uint32),
            ("pids", ctypes.c_size_t * 1024),
        ]

    result = {"leader_pid": process.pid, "leader_before": process.poll()}
    try:
        kernel = tree._api._kernel
        result["active_count"] = tree._api.active_count(tree._job)
        snapshot = JobPids()
        queried = kernel.QueryInformationJobObject(
            tree._job, 3, ctypes.byref(snapshot), ctypes.sizeof(snapshot), None
        )
        result.update(
            query_ok=int(bool(queried)), assigned=snapshot.assigned, listed=snapshot.listed
        )
        if not queried or snapshot.assigned != snapshot.listed or snapshot.listed > 1024:
            result["probe_error"] = 1
        else:
            result["job_pids"] = tuple(snapshot.pids[: snapshot.listed])
            result["candidates"] = []
            result["image_roles"] = []
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            kernel.IsProcessInJob.restype = ctypes.c_int32
            kernel.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            kernel.QueryFullProcessImageNameW.restype = ctypes.c_int32
            for pid in result["job_pids"]:
                handle = kernel.OpenProcess(0x101000, False, pid)
                if not handle:
                    result["candidates"].append((int(pid), -1, -ctypes.get_last_error()))
                    if len(result["image_roles"]) < 64:
                        result["image_roles"].append((int(pid), "unavailable"))
                    continue
                try:
                    member = ctypes.c_int32()
                    query = kernel.IsProcessInJob(handle, tree._job, ctypes.byref(member))
                    state = kernel.WaitForSingleObject(handle, 0)
                    result["candidates"].append(
                        (int(pid), member.value if query else -1, int(state))
                    )
                    # Classify only a confirmed member of this owned job.
                    # Never retain or log its path, arguments, or environment.
                    if len(result["image_roles"]) < 64:
                        role = "unavailable"
                        if query and not member.value:
                            role = "outside_job"
                        elif query and member.value:
                            name = ctypes.create_unicode_buffer(4096)
                            length = ctypes.c_uint32(len(name))
                            if kernel.QueryFullProcessImageNameW(
                                handle, 0, name, ctypes.byref(length)
                            ):
                                basename = name.value.rpartition("\\")[2].casefold()
                                role = {
                                    "python.exe": "python",
                                    "pythonw.exe": "pythonw",
                                    "conhost.exe": "console_host",
                                }.get(basename, "other")
                        result["image_roles"].append((int(pid), role))
                finally:
                    tree._api.close_handle(int(handle))
    except Exception:
        result["probe_error"] = 1
    result["leader_after"] = process.poll()
    return result


@pytest.mark.parametrize("mode", ["live", "0", "7"])
def test_real_owned_tree_stops_grandchild_even_after_leader_exit(tmp_path, mode):
    tree = ProcessTree()
    ready, heartbeat, pidfile = (tmp_path / name for name in ("gate", "heartbeat", "pid"))
    grandpid = tmp_path / "grandpid"
    process = None
    try:
        process = tree.start(
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
                str(grandpid),
            ],
        )
        wait_for(lambda: ready.exists() and ready.stat().st_size > 0)
        assert not heartbeat.exists() and not pidfile.exists()
        # venv launcher PID and actual interpreter PID are separate on Windows.
        assert_native_member(tree, process.pid)
        assert_native_member(tree, int(ready.read_text(encoding="ascii")))
        assert process.stdin is not None
        process.stdin.write(b"1")
        process.stdin.close()
        wait_for(
            lambda: (
                heartbeat.exists()
                and heartbeat.stat().st_size > 0
                and pidfile.exists()
                and pidfile.stat().st_size > 0
                and grandpid.exists()
                and grandpid.stat().st_size > 0
            )
        )
        assert_native_member(tree, int(pidfile.read_text(encoding="ascii")))
        assert_native_member(tree, int(grandpid.read_text(encoding="ascii")))
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
def test_real_leader_without_spawned_application_child(
    tmp_path, exit_before_close, record_property
):
    tree = ProcessTree()
    pidfile = tmp_path / "interpreter"
    # Use the base executable on Windows to avoid the venv redirector. The OS
    # may still create another owned job member; the native probe checks it.
    executable = (
        getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    )
    process = tree.start(
        [
            executable,
            "-I",
            "-S",
            "-c",
            "import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "sys.stdin.buffer.read(1)",
            str(pidfile),
        ],
    )
    try:
        wait_for(lambda: pidfile.exists() and pidfile.stat().st_size > 0)
        actual_pid = int(pidfile.read_text())
        assert actual_pid == process.pid
        assert_native_member(tree, actual_pid)
        if exit_before_close:
            process.stdin.write(b"1")
            process.stdin.close()
            assert process.wait(timeout=4) == 0
        probe = native_cleanup_probe(tree, process)
        expected_descendants = False
        if os.name == "nt":
            # Both retained diagnostics carry complete bounded numeric JSON;
            # pytest's assertion repr shortens nested dictionaries.
            encoded_probe = json.dumps(probe, sort_keys=True, separators=(",", ":"))
            assert len(encoded_probe) <= 65536, "ci_cleanup_probe_oversize"
            record_property("ci_cleanup_probe", encoded_probe)
            print("ci_cleanup_probe=" + encoded_probe, file=sys.stderr, flush=True)
            assert probe.get("query_ok") == 1 and "probe_error" not in probe
            assert probe["assigned"] == probe["listed"] == len(probe["job_pids"])
            assert len(probe["candidates"]) == probe["listed"]
            assert all(
                member in (0, 1) and state in (0, 0x102) for _, member, state in probe["candidates"]
            )
            # A Windows launch can have another live job member even though
            # this Python script does not create a child. The independent OS
            # probe defines the expected ownership result, not that script.
            expected_descendants = any(
                pid != process.pid and member == 1 and state == 0x102
                for pid, member, state in probe["candidates"]
            )
        result = tree.close(process)
        if result is not expected_descendants:
            pytest.fail("ci_cleanup_probe=" + json.dumps(probe, sort_keys=True), pytrace=False)
        assert process.poll() is not None
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
        self.count = 0
        self.calls = []
        self.exitcode = None
        self.fail_query = self.fail_terminate = False
        self.fail_stage = None
        self._kernel = self
        self.members = {123: 0x102}
        self.transient_counts = []
        self.InitializeProcThreadAttributeList = Function(self.initialize)
        self.UpdateProcThreadAttribute = Function(self.update)
        self.DeleteProcThreadAttributeList = Function(lambda value: self.calls.append("delete"))
        self.CreateProcessW = Function(self.create)
        self.QueryInformationJobObject = Function(self.query_members)
        self.OpenProcess = Function(self.open_process)
        self.IsProcessInJob = Function(self.is_process_in_job)
        self.WaitForSingleObject = Function(self.wait_process)
        self.attributes = {}

    def query_members(self, job, category, snapshot, size, returned):
        assert job == 10 and category == 3 and returned is None
        assert size == ctypes.sizeof(tree_module._JobPids)
        if self.fail_query:
            return False
        rows = list(self.members)
        out = ctypes.cast(snapshot, ctypes.POINTER(tree_module._JobPids)).contents
        out.assigned = out.listed = len(rows)
        for index, pid in enumerate(rows):
            out.pids[index] = pid
        self.calls.append(("members", tuple(rows)))
        return True

    def open_process(self, access, inherit, pid):
        assert access == 0x101000 and inherit is False
        return pid + 1000 if pid in self.members else 0

    def is_process_in_job(self, handle, job, member):
        assert job == 10 and handle - 1000 in self.members
        ctypes.cast(member, ctypes.POINTER(ctypes.c_int32))[0] = 1
        return True

    def wait_process(self, handle, timeout):
        assert timeout == 0 and handle - 1000 in self.members
        return self.members[handle - 1000]

    def create_job(self):
        self.calls.append("create")
        return 10

    def configure_job(self, job):
        assert job == 10
        self.calls.append("kill_on_close")

    def initialize(self, attributes, count, reserved, size):
        assert count == 2 and reserved == 0
        ctypes.cast(size, ctypes.POINTER(tree_module._SIZE_T))[0] = 128
        return self.fail_stage != "initialize"

    def update(self, attributes, flags, kind, value, size, previous, returned):
        assert flags == 0 and previous is returned is None
        self.attributes[kind] = list(value)
        self.calls.append(("attribute", kind))
        return self.fail_stage != "attribute"

    def create(self, executable, command, pa, ta, inherit, flags, env, cwd, startup, receipt):
        assert self.attributes[0x2000D] == [10]
        assert len(self.attributes[0x20002]) == 2  # Only stdin/stdout; stderr shares stdout.
        assert flags == 0x08080004 and inherit is True and env is cwd is None
        info = ctypes.cast(startup, ctypes.POINTER(tree_module._StartupInfoEx)).contents
        assert info.StartupInfo.hStdInput == self.attributes[0x20002][0]
        assert info.StartupInfo.hStdOutput == info.StartupInfo.hStdError
        assert info.StartupInfo.hStdOutput == self.attributes[0x20002][1]
        self.calls.append("create_suspended_in_job")
        if self.fail_stage == "create":
            return False
        result = ctypes.cast(receipt, ctypes.POINTER(tree_module._ProcessInformation)).contents
        result.hProcess, result.hThread, result.dwProcessId = 100, 200, 123
        self.count = 1
        return True

    def resume_thread(self, handle):
        assert handle == 200 and "create_suspended_in_job" in self.calls
        self.calls.append("resume")
        if self.fail_stage == "resume":
            raise OSError("SYNTHETIC_PRIVATE_RESUME")

    def poll_process(self, handle):
        assert handle == 100
        return self.exitcode

    def close_handle(self, handle):
        self.calls.append(("close", handle))
        if self.fail_stage == "thread_close" and handle == 200:
            raise OSError("SYNTHETIC_PRIVATE_THREAD_CLOSE")

    def active_count(self, job):
        assert job == 10
        if self.fail_query:
            raise OSError("SYNTHETIC_PRIVATE_QUERY")
        if self.transient_counts:
            self.count = self.transient_counts.pop(0)
            if self.count == 0:
                self.members = {}
        self.calls.append(("count", self.count))
        return self.count

    def terminate_job(self, job):
        assert job == 10
        self.calls.append("terminate")
        if self.fail_terminate:
            raise OSError("SYNTHETIC_PRIVATE_TERMINATE")
        self.count = 0
        self.exitcode = 1
        self.members = {}


def windows_tree(monkeypatch):
    api = JobAPI()
    monkeypatch.setattr(tree_module, "_WINDOWS", True)
    monkeypatch.setattr(tree_module, "NativeWindowsAPI", lambda: api)
    monkeypatch.setattr(tree_module, "_native_handle", lambda fd: fd + 1000)
    return tree_module.ProcessTree(), api


def test_windows_job_list_precedes_resume_and_accounting_outlives_leader(monkeypatch):
    tree, api = windows_tree(monkeypatch)
    process = tree.start(["synthetic-python", "-c", "pass"])
    assert api.calls == [
        "create",
        "kill_on_close",
        ("attribute", 0x20002),
        ("attribute", 0x2000D),
        "create_suspended_in_job",
        "resume",
        ("close", 200),
        "delete",
    ]
    process.returncode = 7  # Leader has exited; one job member is still live.
    api.members = {123: 0, 456: 0x102}
    assert tree.close(process) is True
    assert api.calls[-4:] == ["terminate", ("count", 0), ("close", 100), ("close", 10)]


def test_windows_success_waits_for_natural_member_exit_before_classifying(monkeypatch):
    tree, api = windows_tree(monkeypatch)
    process = tree.start(["synthetic-python"])
    process.returncode = 0
    api.members = {123: 0, 456: 0x102}
    api.count = 1
    api.transient_counts = [1, 1, 0]
    assert tree.close(process) is False
    assert "terminate" not in api.calls
    assert ("close", 10) in api.calls
    assert tree._cleanup_probe["accounted"] == 0


def test_windows_success_still_fails_persistent_owned_member(monkeypatch):
    monkeypatch.setattr(tree_module, "_NATURAL_EXIT_SECONDS", 0.02)
    tree, api = windows_tree(monkeypatch)
    process = tree.start(["synthetic-python"])
    process.returncode = 0
    api.members = {123: 0, 456: 0x102}
    api.count = 1
    assert tree.close(process) is True
    assert "terminate" in api.calls
    assert tree._cleanup_probe["candidates"] == ((456, 1, 0x102),)
    assert tree._cleanup_probe["accounted"] == 1


@pytest.mark.parametrize("failure", ["query", "terminate", "active_timeout"])
def test_windows_failed_cleanup_retains_job_and_fixed_error(monkeypatch, failure):
    tree, api = windows_tree(monkeypatch)
    process = tree.start(["synthetic-python"])
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
    api.exitcode = 1
    assert tree.close(process) is False


@pytest.mark.parametrize("stage", ["initialize", "attribute", "create", "resume"])
def test_windows_partial_start_keeps_ownership_and_close_none_cleans_it(monkeypatch, stage):
    tree, api = windows_tree(monkeypatch)
    api.fail_stage = stage
    with pytest.raises(ProcessTreeError, match="^ci_process_tree_start_failed$"):
        tree.start(["synthetic-python"])
    assert tree.close(None) is False
    if stage == "resume":
        assert "terminate" in api.calls and ("close", 200) in api.calls
        assert ("close", 100) in api.calls
    else:
        assert "resume" not in api.calls
    assert api.calls[-1] == ("close", 10)


def test_windows_thread_close_failure_retains_handles_until_confirmed_cleanup(monkeypatch):
    tree, api = windows_tree(monkeypatch)
    api.fail_stage = "thread_close"
    with pytest.raises(ProcessTreeError, match="^ci_process_tree_start_failed$"):
        tree.start(["synthetic-python"])
    assert tree._process.handles.thread == 200 and tree._process.handles.process == 100
    with pytest.raises(ProcessTreeError, match="^ci_process_tree_cleanup_failed$"):
        tree.close(None)
    assert api.count == 0 and tree._job == 10 and ("close", 10) not in api.calls
    assert tree._process.handles.thread == 200 and tree._process.handles.process == 100
    api.fail_stage = None
    assert tree.close(None) is False
    assert tree._process.handles.thread is tree._process.handles.process is None
    assert api.calls[-1] == ("close", 10)


def test_windows_retrospective_attach_is_rejected_before_any_assignment(monkeypatch):
    tree, api = windows_tree(monkeypatch)
    with pytest.raises(ProcessTreeError, match="^ci_process_tree_attach_failed$"):
        tree.attach(object())
    assert tree.close(None) is False
    assert api.calls == ["create", "kill_on_close", ("count", 0), ("close", 10)]
