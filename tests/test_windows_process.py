"""Synthetic ownership tests. No tunnel executable or native process is run."""

from __future__ import annotations

import ctypes
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from cardputer_mcp import windows_process as wp

ARGS = [r"C:\Program Files\Tunnel\tunnel-client.exe", "run"]
CWD = r"C:\Program Files\Tunnel"
ENV = {
    "SYSTEMROOT": r"C:\Windows",
    "CARDPUTER_TUNNEL_API_KEY": "synthetic-api-secret",
    "CARDPUTER_TUNNEL_BEARER": "Bearer synthetic-bearer",
}


class FakeAPI:
    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.events: list[tuple[str, int]] = []
        self.count = 0
        self.exit_code: int | None = None
        self.terminate_works = True
        self.query_works = True
        self.close_fail: int | None = None

    def _check(self, event: str, handle: int) -> None:
        self.events.append((event, handle))
        if self.fail == event:
            raise OSError("synthetic-api-secret must never appear in error output")

    def create_job(self) -> int:
        self._check("create_job", 10)
        return 10

    def configure_job(self, job: int) -> None:
        self._check("configure_job", job)

    def create_suspended(
        self,
        job: int,
        args: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        handles: wp.ProcessHandles,
    ) -> None:
        assert list(args) == ARGS
        assert dict(env) == ENV
        assert cwd == CWD
        self._check("create_suspended", job)
        self.count = 1
        handles.process = 20
        handles.thread = 30
        self._check("receipt_recorded", job)

    def resume_thread(self, thread: int) -> None:
        self._check("resume_thread", thread)

    def poll_process(self, process: int) -> int | None:
        self._check("poll_process", process)
        return self.exit_code

    def active_count(self, job: int) -> int:
        self._check("active_count", job)
        if not self.query_works:
            raise OSError("synthetic-api-secret")
        return self.count

    def terminate_job(self, job: int) -> None:
        self._check("terminate_job", job)
        if self.terminate_works:
            self.count = 0
            self.exit_code = 1

    def close_handle(self, handle: int) -> None:
        self._check("close_handle", handle)
        if handle == self.close_fail:
            raise OSError("synthetic-api-secret")


def launch(api: FakeAPI) -> wp.OwnedWindowsProcess:
    return wp.start(ARGS, ENV, CWD, api=api)


def test_launch_is_suspended_in_job_before_resume() -> None:
    api = FakeAPI()
    owner = launch(api)
    assert api.events == [
        ("create_job", 10),
        ("configure_job", 10),
        ("create_suspended", 10),
        ("receipt_recorded", 10),
        ("resume_thread", 30),
        ("close_handle", 30),
    ]
    assert owner.poll() is None
    assert owner.active_count() == 1
    assert owner.stop(timeout=0)
    owner.close()
    assert api.events[-2:] == [("close_handle", 20), ("close_handle", 10)]
    assert owner.poll() == 1
    assert owner.active_count() == 0
    assert owner.stop(timeout=0)
    previous = list(api.events)
    owner.close()
    assert api.events == previous


def test_leader_exit_does_not_mean_descendants_stopped() -> None:
    api = FakeAPI()
    owner = launch(api)
    api.exit_code = 42
    api.count = 2
    assert owner.poll() == 42
    assert owner.active_count() == 2
    with pytest.raises(wp.WindowsProcessError, match="owned_job_still_active"):
        owner.close()
    assert ("close_handle", 10) not in api.events
    assert owner.stop(timeout=0)
    assert ("terminate_job", 10) in api.events
    owner.close()
    assert owner.poll() == 42


def test_timeout_retains_every_handle_and_repeated_stop_can_finish() -> None:
    api = FakeAPI()
    owner = launch(api)
    api.terminate_works = False
    assert not owner.stop(timeout=0)
    assert api.events.count(("close_handle", 20)) == 0
    assert api.events.count(("close_handle", 10)) == 0
    with pytest.raises(wp.WindowsProcessError, match="owned_job_still_active"):
        owner.close()
    api.terminate_works = True
    assert owner.stop(timeout=0)
    owner.close()


def test_zero_active_members_still_requires_reaped_leader() -> None:
    api = FakeAPI()
    owner = launch(api)
    api.count = 0
    api.terminate_works = False
    assert owner.poll() is None
    assert not owner.stop(timeout=0)
    assert ("close_handle", 20) not in api.events
    api.exit_code = 0
    assert owner.stop(timeout=0)
    owner.close()


def test_query_failure_retains_handles_and_is_redacted() -> None:
    api = FakeAPI()
    owner = launch(api)
    api.query_works = False
    with pytest.raises(wp.WindowsProcessError) as caught:
        owner.active_count()
    assert str(caught.value) == "owned_job_query_failed"
    assert caught.value.owned is owner
    assert not owner.stop(timeout=0)
    assert ("close_handle", 10) not in api.events
    api.query_works = True
    assert owner.stop(timeout=0)
    owner.close()


def test_poll_failure_is_not_mistaken_for_process_exit() -> None:
    api = FakeAPI()
    owner = launch(api)
    api.fail = "poll_process"
    api.count = 0
    with pytest.raises(wp.WindowsProcessError, match="owned_process_poll_failed"):
        owner.poll()
    assert not owner.stop(timeout=0)
    api.fail = ""
    assert owner.stop(timeout=0)
    owner.close()


@pytest.mark.parametrize(
    "stage",
    [
        "configure_job",
        "create_suspended",
        "receipt_recorded",
        "resume_thread",
    ],
)
def test_launch_failures_terminate_reap_and_close_only_owned_handles(stage: str) -> None:
    api = FakeAPI(stage)
    with pytest.raises(wp.WindowsProcessError) as caught:
        launch(api)
    assert caught.value.owned is None
    assert "synthetic-api-secret" not in str(caught.value)
    assert "synthetic-api-secret" not in repr(caught.value)
    assert ("close_handle", 10) in api.events
    if stage in {"receipt_recorded", "resume_thread"}:
        assert ("terminate_job", 10) in api.events
        assert ("poll_process", 20) in api.events
        assert ("close_handle", 20) in api.events
        assert ("close_handle", 30) in api.events
    assert all(handle in {10, 20, 30} for _, handle in api.events)


def test_create_job_failure_is_fixed_code() -> None:
    with pytest.raises(wp.WindowsProcessError, match="owned_job_create_failed") as caught:
        launch(FakeAPI("create_job"))
    assert caught.value.owned is None


def test_uncertain_launch_preserves_owner_for_manager_review() -> None:
    api = FakeAPI("resume_thread")
    api.query_works = False
    with pytest.raises(wp.WindowsProcessError, match="needs_review") as caught:
        launch(api)
    owner = caught.value.owned
    assert owner is not None
    assert owner in wp._RETAINED_OWNERS
    assert ("close_handle", 10) not in api.events
    assert ("close_handle", 20) not in api.events
    api.query_works = True
    assert owner.stop(timeout=0)
    owner.close()
    assert owner not in wp._RETAINED_OWNERS


def test_handle_close_failure_is_retryable_without_double_closing() -> None:
    api = FakeAPI()
    owner = launch(api)
    assert owner.stop(timeout=0)
    api.close_fail = 10
    with pytest.raises(wp.WindowsProcessError, match="owned_handle_close_failed"):
        owner.close()
    assert api.events.count(("close_handle", 20)) == 1
    api.close_fail = None
    owner.close()
    assert api.events.count(("close_handle", 20)) == 1


def test_repr_does_not_contain_arguments_paths_credentials_or_handles() -> None:
    api = FakeAPI()
    owner = launch(api)
    assert repr(owner) == "<OwnedWindowsProcess redacted>"
    assert repr(wp.ProcessHandles(20, 30, [40])) == "<ProcessHandles redacted>"
    assert owner.stop(timeout=0)
    owner.close()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf")])
def test_invalid_timeout_cannot_close_ownership(timeout: float) -> None:
    api = FakeAPI()
    owner = launch(api)
    with pytest.raises(wp.WindowsProcessError, match="invalid_stop_timeout"):
        owner.stop(timeout=timeout)
    assert api.events.count(("terminate_job", 10)) == 0
    assert owner.stop(timeout=0)
    owner.close()


@pytest.mark.parametrize(
    ("args", "env", "cwd"),
    [
        ([], ENV, CWD),
        (["tunnel-client.exe", "run"], ENV, CWD),
        ([r"C:\cmd.exe", "run"], ENV, CWD),
        (ARGS + ["bad\0arg"], ENV, CWD),
        (ARGS, ENV, "relative"),
        (ARGS, {**ENV, "HTTP_PROXY": "bad"}, CWD),
        (ARGS, {**ENV, "systemroot": "duplicate"}, CWD),
        (ARGS, {**ENV, "TEMP": "bad\0value"}, CWD),
        (ARGS + ["--api-key=synthetic-api-secret"], ENV, CWD),
        (ARGS + ["--header=Bearer synthetic-bearer"], ENV, CWD),
    ],
)
def test_validation_precedes_any_native_action(
    args: list[str],
    env: dict[str, str],
    cwd: str,
) -> None:
    api = FakeAPI()
    with pytest.raises(wp.WindowsProcessError):
        wp.start(args, env, cwd, api=api)
    assert api.events == []


@pytest.mark.parametrize(
    "argument",
    [
        "--control-plane.api-key=unrelated-literal-secret",
        "--control-plane.api-key",
        "--mcp.extra-headers=Authorization: unrelated-literal-secret",
        "--mcp.discovery-extra-headers=Authorization: unrelated-literal-secret",
    ],
)
def test_credential_options_allow_only_fixed_environment_references(argument: str) -> None:
    api = FakeAPI()
    with pytest.raises(wp.WindowsProcessError, match="credentials_in_process_arguments"):
        wp.start(ARGS + [argument], ENV, CWD, api=api)
    assert api.events == []


def test_long_utf16_command_rejected_before_native_action() -> None:
    api = FakeAPI()
    with pytest.raises(wp.WindowsProcessError, match="invalid_owned_process_arguments"):
        wp.start(ARGS + ["😀" * 17000], ENV, CWD, api=api)
    assert api.events == []


def test_non_windows_fails_closed_without_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wp.os, "name", "posix")
    with pytest.raises(wp.WindowsProcessError, match="requires_windows_10"):
        wp.start(ARGS, ENV, CWD)


class FakeKernel:
    """Fake Win32 DLL that inspects ctypes arguments but never executes anything."""

    def __init__(self, fail: str = "") -> None:
        self.fail = fail
        self.events: list[str] = []
        self.attributes: dict[int, list[int]] = {}
        self.flags = 0
        self.active = 0
        self.exited = False
        self.closed: list[int] = []
        self.environment: Any = None
        self.environment_text = ""
        self.command = ""
        self.application = ""
        self.directory = ""
        self.standard_handles: tuple[int, int, int] | None = None
        self.inherit: bool | None = None
        self.limit_flags = 0
        self.null_inheritable = False
        self.process_attributes: Any = "not_set"
        self.thread_attributes: Any = "not_set"

    def CreateJobObjectW(self, security: Any, name: Any) -> int:
        assert security is None and name is None
        self.events.append("create_job")
        return 10

    def SetInformationJobObject(self, job: int, kind: int, limits: Any, size: int) -> int:
        assert job == 10 and kind == 9 and size == ctypes.sizeof(wp._ExtendedLimit)
        self.limit_flags = ctypes.cast(
            limits, ctypes.POINTER(wp._ExtendedLimit)
        ).contents.BasicLimitInformation.LimitFlags
        return 1

    def CreateFileW(
        self,
        path: str,
        access: int,
        share: int,
        security: Any,
        creation: int,
        flags: int,
        template: Any,
    ) -> int:
        assert (path, access, share, creation, flags, template) == (
            "NUL",
            0xC0000000,
            3,
            3,
            0x80,
            None,
        )
        self.null_inheritable = bool(
            ctypes.cast(security, ctypes.POINTER(wp._SecurityAttributes)).contents.bInheritHandle
        )
        return 40

    def InitializeProcThreadAttributeList(
        self,
        attributes: Any,
        count: int,
        flags: int,
        size: Any,
    ) -> int:
        assert count == 2 and flags == 0
        ctypes.cast(size, ctypes.POINTER(wp._SIZE_T)).contents.value = 64
        if attributes is None:
            return 0
        return 0 if self.fail == "initialize" else 1

    def UpdateProcThreadAttribute(
        self,
        attributes: Any,
        flags: int,
        attribute: int,
        values: Any,
        size: int,
        previous: Any,
        result: Any,
    ) -> int:
        assert flags == 0 and size == ctypes.sizeof(wp._HANDLE)
        assert previous is None and result is None
        self.attributes[attribute] = list(values)
        self.events.append(f"attribute_{attribute}")
        return 0 if self.fail == "attributes" else 1

    def DeleteProcThreadAttributeList(self, attributes: Any) -> None:
        self.events.append("delete_attributes")

    def CreateProcessW(
        self,
        application: str,
        command: Any,
        process_attributes: Any,
        thread_attributes: Any,
        inherit: bool,
        flags: int,
        environment: Any,
        cwd: str,
        startup: Any,
        process: Any,
    ) -> int:
        self.application = application
        self.command = command.value
        self.environment = environment
        self.environment_text = "".join(environment)
        self.directory = cwd
        self.process_attributes = process_attributes
        self.thread_attributes = thread_attributes
        self.inherit = inherit
        self.flags = flags
        info = ctypes.cast(startup, ctypes.POINTER(wp._StartupInfoEx)).contents
        assert info.StartupInfo.cb == ctypes.sizeof(wp._StartupInfoEx)
        assert info.StartupInfo.dwFlags == 0x100
        self.standard_handles = (
            info.StartupInfo.hStdInput,
            info.StartupInfo.hStdOutput,
            info.StartupInfo.hStdError,
        )
        self.events.append("create_suspended_in_job")
        if self.fail == "create":
            return 0
        receipt = ctypes.cast(process, ctypes.POINTER(wp._ProcessInformation)).contents
        receipt.hProcess = 20
        receipt.hThread = 30
        self.active = 1
        return 1

    def ResumeThread(self, thread: int) -> int:
        assert thread == 30
        self.events.append("resume")
        return 0xFFFFFFFF if self.fail == "resume" else 1

    def CloseHandle(self, handle: int) -> int:
        self.closed.append(handle)
        return 0 if self.fail == "close_null" and handle == 40 else 1

    def QueryInformationJobObject(
        self,
        job: int,
        kind: int,
        accounting: Any,
        size: int,
        result: Any,
    ) -> int:
        assert job == 10 and kind == 1 and result is None
        assert size == ctypes.sizeof(wp._BasicAccounting)
        ctypes.cast(
            accounting, ctypes.POINTER(wp._BasicAccounting)
        ).contents.ActiveProcesses = self.active
        return 1

    def WaitForSingleObject(self, process: int, timeout: int) -> int:
        assert process == 20 and timeout == 0
        return 0 if self.exited else 0x102

    def GetExitCodeProcess(self, process: int, code: Any) -> int:
        assert process == 20 and self.exited
        ctypes.cast(code, ctypes.POINTER(wp._DWORD)).contents.value = 259
        return 1

    def TerminateJobObject(self, job: int, exit_code: int) -> int:
        assert job == 10 and exit_code == 1
        self.active = 0
        self.exited = True
        self.events.append("terminate_owned_job")
        return 1


def native_with_kernel(kernel: FakeKernel) -> wp.NativeWindowsAPI:
    api = object.__new__(wp.NativeWindowsAPI)
    api._kernel = kernel
    return api


def test_native_ctypes_launch_is_atomic_restricted_and_environment_only() -> None:
    kernel = FakeKernel()
    owner = wp.start(ARGS, ENV, CWD, api=native_with_kernel(kernel))
    assert kernel.limit_flags == 0x2000
    assert kernel.attributes == {0x20002: [40], 0x2000D: [10]}
    assert kernel.flags == 0x08080404
    assert kernel.null_inheritable
    assert kernel.inherit is True
    assert kernel.process_attributes is None and kernel.thread_attributes is None
    assert kernel.standard_handles == (40, 40, 40)
    assert kernel.application == ARGS[0]
    assert kernel.command == '"C:\\Program Files\\Tunnel\\tunnel-client.exe" run'
    assert kernel.directory == CWD
    assert "synthetic-api-secret" in kernel.environment_text
    assert "synthetic-api-secret" not in kernel.command
    assert kernel.environment_text.endswith("\0\0")
    entries = [entry for entry in kernel.environment_text.split("\0") if entry]
    assert entries == [f"{key}={ENV[key]}" for key in sorted(ENV)]
    assert all(character == "\0" for character in kernel.environment)
    assert kernel.events.index("create_suspended_in_job") < kernel.events.index("resume")
    assert kernel.closed == [40, 30]
    assert owner.stop(timeout=0)
    owner.close()
    assert kernel.closed == [40, 30, 20, 10]


@pytest.mark.parametrize("stage", ["initialize", "attributes", "create", "resume"])
def test_native_failures_cleanup_without_launching_unowned_child(stage: str) -> None:
    kernel = FakeKernel(stage)
    with pytest.raises(wp.WindowsProcessError) as caught:
        wp.start(ARGS, ENV, CWD, api=native_with_kernel(kernel))
    assert caught.value.owned is None
    assert 10 in kernel.closed
    assert 40 in kernel.closed
    if stage != "initialize":
        assert "delete_attributes" in kernel.events
        if kernel.environment is not None:
            assert all(character == "\0" for character in kernel.environment)
    if stage == "resume":
        assert "terminate_owned_job" in kernel.events
        assert {20, 30}.issubset(kernel.closed)
    else:
        assert "resume" not in kernel.events


def test_failed_null_cleanup_after_creation_still_retains_process_receipt() -> None:
    kernel = FakeKernel("close_null")
    with pytest.raises(wp.WindowsProcessError, match="needs_review") as caught:
        wp.start(ARGS, ENV, CWD, api=native_with_kernel(kernel))
    owner = caught.value.owned
    assert owner is not None
    assert "resume" not in kernel.events
    assert "terminate_owned_job" in kernel.events
    assert kernel.exited
    assert 20 in kernel.closed and 30 in kernel.closed
    assert 10 not in kernel.closed
    kernel.fail = ""
    owner.close()
    assert 10 in kernel.closed


def test_native_exit_code_still_active_is_valid_after_handle_signaled() -> None:
    kernel = FakeKernel()
    api = native_with_kernel(kernel)
    assert api.poll_process(20) is None
    kernel.exited = True
    assert api.poll_process(20) == 259


def test_win32_struct_layout_on_64_bit_host() -> None:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("64-bit ABI layout assertion")
    assert ctypes.sizeof(wp._SecurityAttributes) == 24
    assert ctypes.sizeof(wp._StartupInfo) == 104
    assert ctypes.sizeof(wp._StartupInfoEx) == 112
    assert ctypes.sizeof(wp._ProcessInformation) == 24
    assert ctypes.sizeof(wp._BasicLimit) == 64
    assert ctypes.sizeof(wp._ExtendedLimit) == 144
    assert ctypes.sizeof(wp._BasicAccounting) == 48
