"""Own one Windows Tunnel client subtree, never discover or terminate other PIDs.

The caller must verify the pinned official executable before calling ``start``.
Windows 10+ job-list process attributes assign the suspended child to our unnamed,
non-inheritable, kill-on-close job *inside CreateProcessW*. There is no interval
in which a successfully created child is outside the owned job. The only inherited
handle is a fresh NUL handle used for all three standard streams. No shell, native
output capture, ambient environment, breakaway flag, or process lookup is used.

The WindowsAPI facade allows fully synthetic tests on non-Windows hosts. Native
execution fails closed on unsupported platforms. Job accounting, not leader exit,
is authoritative for subtree shutdown. A failed stop retains every ownership
handle; callers must keep the owner and require review rather than claim success.

Primary API references:
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw
https://learn.microsoft.com/windows/win32/procthread/job-objects
"""

from __future__ import annotations

import ctypes
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

_ENV_KEYS = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "COMSPEC",
        "CARDPUTER_TUNNEL_API_KEY",
        "CARDPUTER_TUNNEL_BEARER",
    }
)
_SECRET_KEYS = frozenset({"CARDPUTER_TUNNEL_API_KEY", "CARDPUTER_TUNNEL_BEARER"})
_CREDENTIAL_ARGUMENTS = {
    "--control-plane.api-key": "env:CARDPUTER_TUNNEL_API_KEY",
    "--mcp.extra-headers": "Authorization: env:CARDPUTER_TUNNEL_BEARER",
    "--mcp.discovery-extra-headers": "Authorization: env:CARDPUTER_TUNNEL_BEARER",
}
_TERMINATED_EXIT_CODE = 1
# Retain failed launches until their owner is explicitly, successfully closed.
# No destructor is allowed to drop a live ownership handle or silently kill work.
_RETAINED_OWNERS: set[OwnedWindowsProcess] = set()


class WindowsProcessError(RuntimeError):
    """Fixed diagnostic codes only. ``owned`` retains uncertain failed launches."""

    def __init__(self, code: str, *, owned: OwnedWindowsProcess | None = None) -> None:
        super().__init__(code)
        self.owned = owned


@dataclass(repr=False)
class ProcessHandles:
    """Mutable receipt populated immediately after creation, even on later failure."""

    process: int | None = None
    thread: int | None = None
    auxiliary: list[int] = field(default_factory=list)

    def __repr__(self) -> str:
        return "<ProcessHandles redacted>"


class WindowsAPI(Protocol):
    """Injectable Win32 facade. Creation must assign the job atomically, suspended."""

    def create_job(self) -> int: ...
    def configure_job(self, job: int) -> None: ...
    def create_suspended(
        self,
        job: int,
        args: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        handles: ProcessHandles,
    ) -> None: ...
    def resume_thread(self, thread: int) -> None: ...
    def poll_process(self, process: int) -> int | None: ...
    def active_count(self, job: int) -> int: ...
    def terminate_job(self, job: int) -> None: ...
    def close_handle(self, handle: int) -> None: ...


class OwnedWindowsProcess:
    """An owned job and its leader; arguments and environment are never retained."""

    def __init__(self, api: WindowsAPI, job: int) -> None:
        self._api = api
        self._job: int | None = job
        self._handles = ProcessHandles()
        self._exit_code: int | None = None
        self._confirmed_stopped = False
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        return "<OwnedWindowsProcess redacted>"

    def poll(self) -> int | None:
        """Return the leader's exit status only; descendants may still be active."""
        with self._lock:
            if self._exit_code is not None or self._handles.process is None:
                return self._exit_code
            try:
                self._exit_code = self._api.poll_process(self._handles.process)
            except Exception:
                raise WindowsProcessError("owned_process_poll_failed", owned=self) from None
            return self._exit_code

    def active_count(self) -> int:
        """Count all members of this job; never infer this from leader status."""
        with self._lock:
            if self._job is None:
                return 0
            try:
                count = self._api.active_count(self._job)
                if count < 0:
                    raise ValueError
                return count
            except Exception:
                raise WindowsProcessError("owned_job_query_failed", owned=self) from None

    def _confirm_stopped(self) -> bool:
        return self.active_count() == 0 and (
            self._handles.process is None or self.poll() is not None
        )

    def stop(self, timeout: float = 5.0) -> bool:
        """Terminate only this job, confirming zero members and a reaped leader.

        A timeout or Win32 failure returns False without closing any handles.
        Repeated calls can establish shutdown after an earlier uncertain result.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise WindowsProcessError("invalid_stop_timeout")
        with self._lock:
            if self._confirmed_stopped or self._job is None:
                return True
            deadline = time.monotonic() + timeout
            try:
                if self._confirm_stopped():
                    self._confirmed_stopped = True
                    return True
            except WindowsProcessError:
                pass
            try:
                self._api.terminate_job(self._job)
            except Exception:
                # A concurrent natural exit can make termination fail. Only
                # authoritative accounting and the leader handle can settle it.
                pass
            while True:
                try:
                    if self._confirm_stopped():
                        self._confirmed_stopped = True
                        return True
                except WindowsProcessError:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.05, remaining))

    def close(self) -> None:
        """Release handles only after confirmed subtree shutdown; never kills."""
        with self._lock:
            if self._job is None:
                return
            if not self._confirmed_stopped:
                if not self._confirm_stopped():
                    raise WindowsProcessError("owned_job_still_active", owned=self)
                self._confirmed_stopped = True
            try:
                if self._handles.thread is not None:
                    self._api.close_handle(self._handles.thread)
                    self._handles.thread = None
                if self._handles.process is not None:
                    self._api.close_handle(self._handles.process)
                    self._handles.process = None
                while self._handles.auxiliary:
                    self._api.close_handle(self._handles.auxiliary[-1])
                    self._handles.auxiliary.pop()
                self._api.close_handle(self._job)
                self._job = None
            except Exception:
                raise WindowsProcessError("owned_handle_close_failed", owned=self) from None
            _RETAINED_OWNERS.discard(self)


def _validate(args: Sequence[str], env: Mapping[str, str], cwd: str) -> None:
    if isinstance(args, (str, bytes)) or not args:
        raise WindowsProcessError("invalid_owned_process_arguments")
    if any(not isinstance(arg, str) or "\0" in arg for arg in args):
        raise WindowsProcessError("invalid_owned_process_arguments")
    executable = PureWindowsPath(args[0])
    if not executable.is_absolute() or executable.name.casefold() != "tunnel-client.exe":
        raise WindowsProcessError("invalid_owned_process_executable")
    if not cwd or "\0" in cwd or not PureWindowsPath(cwd).is_absolute():
        raise WindowsProcessError("invalid_owned_process_directory")
    seen: set[str] = set()
    secrets: list[str] = []
    for key, value in env.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or key.upper() not in _ENV_KEYS
            or key.upper() in seen
            or "\0" in value
        ):
            raise WindowsProcessError("invalid_owned_process_environment")
        seen.add(key.upper())
        if key.upper() in _SECRET_KEYS and value:
            secrets.append(value)
    if any(secret in arg for secret in secrets for arg in args):
        raise WindowsProcessError("credentials_in_process_arguments")
    for argument in args[1:]:
        option, _, value = argument.partition("=")
        if option in _CREDENTIAL_ARGUMENTS and value != _CREDENTIAL_ARGUMENTS[option]:
            raise WindowsProcessError("credentials_in_process_arguments")
    if len(subprocess.list2cmdline(args).encode("utf-16-le", errors="surrogatepass")) // 2 >= 32767:
        raise WindowsProcessError("invalid_owned_process_arguments")


def start(
    args: Sequence[str],
    env: Mapping[str, str],
    cwd: str | Path,
    *,
    api: WindowsAPI | None = None,
) -> OwnedWindowsProcess:
    """Launch a caller-verified client with only the supplied whitelisted env.

    The caller owns release/hash verification. No executable is downloaded or
    inspected here. On launch failure ``WindowsProcessError.owned`` is non-None
    only if shutdown/handle release is unconfirmed; preserve it for review/retry.
    """
    directory = str(cwd)
    _validate(args, env, directory)
    facade = api if api is not None else NativeWindowsAPI()
    try:
        job = facade.create_job()
    except Exception:
        raise WindowsProcessError("owned_job_create_failed") from None
    owner = OwnedWindowsProcess(facade, job)
    try:
        facade.configure_job(job)
        facade.create_suspended(job, args, env, directory, owner._handles)
        if owner._handles.process is None or owner._handles.thread is None:
            raise WindowsProcessError("owned_process_handles_missing")
        facade.resume_thread(owner._handles.thread)
        facade.close_handle(owner._handles.thread)
        owner._handles.thread = None
        return owner
    except BaseException:
        # Atomic job assignment also covers failures before the process receipt
        # is returned. Never release a live job merely to hide a failed launch.
        _RETAINED_OWNERS.add(owner)
        cleaned = False
        if owner.stop():
            try:
                owner.close()
                cleaned = True
            except WindowsProcessError:
                pass
        raise WindowsProcessError(
            "owned_process_start_failed" if cleaned else "owned_process_start_needs_review",
            owned=None if cleaned else owner,
        ) from None


# Fixed-width Win32 types also make structure layout testable on Linux. Native
# ctypes.wintypes DWORD/LONG follow host C widths and are wrong on LP64 Linux.
_DWORD = ctypes.c_uint32
_WORD = ctypes.c_uint16
_BOOL = ctypes.c_int32
_HANDLE = ctypes.c_void_p
_SIZE_T = ctypes.c_size_t
_LARGE_INTEGER = ctypes.c_int64


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", _DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", _BOOL),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", _DWORD),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", _DWORD),
        ("dwY", _DWORD),
        ("dwXSize", _DWORD),
        ("dwYSize", _DWORD),
        ("dwXCountChars", _DWORD),
        ("dwYCountChars", _DWORD),
        ("dwFillAttribute", _DWORD),
        ("dwFlags", _DWORD),
        ("wShowWindow", _WORD),
        ("cbReserved2", _WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", _HANDLE),
        ("hStdOutput", _HANDLE),
        ("hStdError", _HANDLE),
    ]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", _HANDLE),
        ("hThread", _HANDLE),
        ("dwProcessId", _DWORD),
        ("dwThreadId", _DWORD),
    ]


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", _LARGE_INTEGER),
        ("PerJobUserTimeLimit", _LARGE_INTEGER),
        ("LimitFlags", _DWORD),
        ("MinimumWorkingSetSize", _SIZE_T),
        ("MaximumWorkingSetSize", _SIZE_T),
        ("ActiveProcessLimit", _DWORD),
        ("Affinity", _SIZE_T),
        ("PriorityClass", _DWORD),
        ("SchedulingClass", _DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", _SIZE_T),
        ("JobMemoryLimit", _SIZE_T),
        ("PeakProcessMemoryUsed", _SIZE_T),
        ("PeakJobMemoryUsed", _SIZE_T),
    ]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", _LARGE_INTEGER),
        ("TotalKernelTime", _LARGE_INTEGER),
        ("ThisPeriodTotalUserTime", _LARGE_INTEGER),
        ("ThisPeriodTotalKernelTime", _LARGE_INTEGER),
        ("TotalPageFaultCount", _DWORD),
        ("TotalProcesses", _DWORD),
        ("ActiveProcesses", _DWORD),
        ("TotalTerminatedProcesses", _DWORD),
    ]


class NativeWindowsAPI:
    """ctypes implementation; construction never spawns a process."""

    def __init__(self) -> None:
        version = getattr(sys, "getwindowsversion", None)
        loader = getattr(ctypes, "WinDLL", None)
        if os.name != "nt" or version is None or version().major < 10 or loader is None:
            raise WindowsProcessError("owned_process_requires_windows_10")
        try:
            self._kernel: Any = loader("kernel32", use_last_error=True)
            self._bind_functions()
        except Exception:
            raise WindowsProcessError("owned_windows_api_unavailable") from None

    def __repr__(self) -> str:
        return "<NativeWindowsAPI>"

    def _bind_functions(self) -> None:
        pointer = ctypes.c_void_p
        signatures: dict[str, tuple[list[Any], Any]] = {
            "CreateJobObjectW": ([pointer, ctypes.c_wchar_p], _HANDLE),
            "SetInformationJobObject": ([_HANDLE, _DWORD, pointer, _DWORD], _BOOL),
            "QueryInformationJobObject": (
                [_HANDLE, _DWORD, pointer, _DWORD, pointer],
                _BOOL,
            ),
            "TerminateJobObject": ([_HANDLE, _DWORD], _BOOL),
            "CloseHandle": ([_HANDLE], _BOOL),
            "CreateFileW": (
                [ctypes.c_wchar_p, _DWORD, _DWORD, pointer, _DWORD, _DWORD, _HANDLE],
                _HANDLE,
            ),
            "InitializeProcThreadAttributeList": ([pointer, _DWORD, _DWORD, pointer], _BOOL),
            "UpdateProcThreadAttribute": (
                [pointer, _DWORD, _SIZE_T, pointer, _SIZE_T, pointer, pointer],
                _BOOL,
            ),
            "DeleteProcThreadAttributeList": ([pointer], None),
            "CreateProcessW": (
                [
                    ctypes.c_wchar_p,
                    pointer,
                    pointer,
                    pointer,
                    _BOOL,
                    _DWORD,
                    pointer,
                    ctypes.c_wchar_p,
                    pointer,
                    pointer,
                ],
                _BOOL,
            ),
            "ResumeThread": ([_HANDLE], _DWORD),
            "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
            "GetExitCodeProcess": ([_HANDLE, pointer], _BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._kernel, name)
            function.argtypes = arguments
            function.restype = result

    def create_job(self) -> int:
        handle = self._kernel.CreateJobObjectW(None, None)
        if not handle:
            raise WindowsProcessError("owned_job_create_failed")
        return int(handle)

    def configure_job(self, job: int) -> None:
        limits = _ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway
        if not self._kernel.SetInformationJobObject(
            job,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise WindowsProcessError("owned_job_configure_failed")

    def create_suspended(
        self,
        job: int,
        args: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        handles: ProcessHandles,
    ) -> None:
        security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True)
        null = self._kernel.CreateFileW(
            "NUL",
            0xC0000000,
            3,
            ctypes.byref(security),
            3,
            0x80,
            None,
        )
        if not null or null == ctypes.c_void_p(-1).value:
            raise WindowsProcessError("owned_null_handle_failed")
        handles.auxiliary.append(int(null))
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={env[key]}" for key in sorted(env, key=str.upper)) + "\0\0"
        )
        initialized = False
        attributes = ctypes.c_void_p()
        try:
            size = _SIZE_T()
            self._kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not size.value:
                raise WindowsProcessError("owned_process_attributes_failed")
            storage = ctypes.create_string_buffer(size.value)
            attributes = ctypes.cast(storage, ctypes.c_void_p)
            if not self._kernel.InitializeProcThreadAttributeList(
                attributes,
                2,
                0,
                ctypes.byref(size),
            ):
                raise WindowsProcessError("owned_process_attributes_failed")
            initialized = True
            inherited = (_HANDLE * 1)(null)
            jobs = (_HANDLE * 1)(job)
            for attribute, value in ((0x20002, inherited), (0x2000D, jobs)):
                if not self._kernel.UpdateProcThreadAttribute(
                    attributes,
                    0,
                    attribute,
                    value,
                    ctypes.sizeof(value),
                    None,
                    None,
                ):
                    raise WindowsProcessError("owned_process_attributes_failed")
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = null
            startup.StartupInfo.hStdOutput = null
            startup.StartupInfo.hStdError = null
            startup.lpAttributeList = attributes
            process = _ProcessInformation()
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
            # CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT |
            # EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
            if not self._kernel.CreateProcessW(
                args[0],
                command,
                None,
                None,
                True,
                0x08080404,
                environment,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(process),
            ):
                raise WindowsProcessError("owned_process_create_failed")
            # Record ownership before any subsequent cleanup operation can fail.
            handles.process = int(process.hProcess)
            handles.thread = int(process.hThread)
        finally:
            ctypes.memset(ctypes.addressof(environment), 0, ctypes.sizeof(environment))
            if initialized:
                self._kernel.DeleteProcThreadAttributeList(attributes)
            self.close_handle(int(null))
            handles.auxiliary.remove(int(null))

    def resume_thread(self, thread: int) -> None:
        # Exactly one suspension is expected. Unexpected state is fail-closed.
        if self._kernel.ResumeThread(thread) != 1:
            raise WindowsProcessError("owned_process_resume_failed")

    def poll_process(self, process: int) -> int | None:
        status = int(self._kernel.WaitForSingleObject(process, 0))
        if status == 0x102:  # WAIT_TIMEOUT
            return None
        if status != 0:  # WAIT_OBJECT_0 is the only confirmed terminated state
            raise WindowsProcessError("owned_process_wait_failed")
        code = _DWORD()
        if not self._kernel.GetExitCodeProcess(process, ctypes.byref(code)):
            raise WindowsProcessError("owned_process_exit_code_failed")
        # Exit code 259 is valid once the process handle is signaled.
        return int(code.value)

    def active_count(self, job: int) -> int:
        accounting = _BasicAccounting()
        if not self._kernel.QueryInformationJobObject(
            job,
            1,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise WindowsProcessError("owned_job_query_failed")
        return int(accounting.ActiveProcesses)

    def terminate_job(self, job: int) -> None:
        if not self._kernel.TerminateJobObject(job, _TERMINATED_EXIT_CODE):
            raise WindowsProcessError("owned_job_terminate_failed")

    def close_handle(self, handle: int) -> None:
        if not self._kernel.CloseHandle(handle):
            raise WindowsProcessError("owned_handle_close_failed")
