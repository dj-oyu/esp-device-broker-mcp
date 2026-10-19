"""CI-only ownership for a stdin-gated child and its descendants.

start owns the launcher before it executes, including Windows venv redirectors
which spawn the interpreter before any Python stdin gate. The caller releases
that gate only after start returns. Groups cannot contain descendants
that deliberately leave them. Reap the direct child; adopted grandchildren are
not waitable here. Linux zombies are stopped, rather than live descendants.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from cardputer_mcp.windows_process import (
    _HANDLE,
    _SIZE_T,
    NativeWindowsAPI,
    ProcessHandles,
    _ProcessInformation,
    _StartupInfoEx,
)

_WINDOWS = os.name == "nt"
_CLEANUP_SECONDS = 8.0
_MAX_PROC_ENTRIES = 100000


class ProcessTreeError(RuntimeError):
    """Fixed CI failure codes; never native error text or process arguments."""


def _native_handle(fd: int) -> int:
    if sys.platform != "win32":
        raise ProcessTreeError("ci_process_tree_start_failed")
    import msvcrt

    return msvcrt.get_osfhandle(fd)


class WindowsChild:
    """Small Popen-compatible receipt; ownership stays with ProcessTree."""

    def __init__(
        self, api: Any, job: int, handles: ProcessHandles, pid: int, stdin: IO[bytes]
    ) -> None:
        self.api, self.handles, self.pid, self.stdin = api, handles, pid, stdin
        self.job: int | None = job
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                self.returncode = self.api.poll_process(self.handles.process)
            except Exception:
                raise ProcessTreeError("ci_process_tree_poll_failed") from None
        return self.returncode

    def wait(self, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if time.monotonic() >= deadline:
                raise ProcessTreeError("ci_process_tree_wait_failed")
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))

    def kill(self) -> None:
        if self.job is not None:
            try:
                self.api.terminate_job(self.job)
            except Exception:
                raise ProcessTreeError("ci_process_tree_cleanup_failed") from None


Child = subprocess.Popen[bytes] | WindowsChild


class ProcessTree:
    def __init__(self) -> None:
        self._api: Any = None
        self._job: int | None = None
        self._process: Child | None = None
        self._group: int | None = None
        self._closed = False
        self._descendants = False
        if _WINDOWS:
            try:
                self._api = NativeWindowsAPI()
                self._job = self._api.create_job()
                self._api.configure_job(self._job)
            except Exception:
                if self._job is not None:
                    try:
                        if self._api.active_count(self._job) == 0:
                            self._api.close_handle(self._job)
                            self._job = None
                    except Exception:
                        pass  # Explicit failure; do not claim handle cleanup.
                raise ProcessTreeError("ci_process_tree_create_failed") from None

    def start(self, args: Sequence[str], *, stdout: int | IO[bytes] = subprocess.DEVNULL) -> Child:
        """Atomically own the launcher before it can create an interpreter."""
        if self._closed or self._process is not None:
            raise ProcessTreeError("ci_process_tree_start_failed")
        if self._job is None:
            try:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                self.attach(process)
                return process
            except Exception:
                raise ProcessTreeError("ci_process_tree_start_failed") from None
        descriptors: list[int] = []
        attributes = ctypes.c_void_p()
        initialized = False
        input_stream: IO[bytes] | None = None
        try:
            if not args or any(not isinstance(arg, str) or "\0" in arg for arg in args):
                raise ValueError
            read_fd, write_fd = os.pipe()
            descriptors.extend((read_fd, write_fd))
            if isinstance(stdout, int):
                if stdout != subprocess.DEVNULL:
                    raise ValueError
                output_fd = os.open(os.devnull, os.O_WRONLY)
            else:
                output_fd = os.dup(stdout.fileno())
            descriptors.append(output_fd)
            for fd in (read_fd, output_fd):
                os.set_inheritable(fd, True)
            inherited = (_HANDLE * 2)(_native_handle(read_fd), _native_handle(output_fd))
            jobs = (_HANDLE * 1)(self._job)
            kernel = self._api._kernel
            size = _SIZE_T()
            kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not size.value:
                raise ValueError
            storage = ctypes.create_string_buffer(size.value)
            attributes = ctypes.cast(storage, ctypes.c_void_p)
            if not kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(size)):
                raise ValueError
            initialized = True
            for attribute, value in ((0x20002, inherited), (0x2000D, jobs)):
                if not kernel.UpdateProcThreadAttribute(
                    attributes, 0, attribute, value, ctypes.sizeof(value), None, None
                ):
                    raise ValueError
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100
            startup.StartupInfo.hStdInput = inherited[0]
            startup.StartupInfo.hStdOutput = startup.StartupInfo.hStdError = inherited[1]
            startup.lpAttributeList = attributes
            receipt = _ProcessInformation()
            command = ctypes.create_unicode_buffer(subprocess.list2cmdline(args))
            input_stream = os.fdopen(write_fd, "wb", buffering=0)
            descriptors.remove(write_fd)
            # CREATE_SUSPENDED | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW.
            if not kernel.CreateProcessW(
                args[0],
                command,
                None,
                None,
                True,
                0x08080004,
                None,
                None,
                ctypes.byref(startup),
                ctypes.byref(receipt),
            ):
                raise ValueError
            handles = ProcessHandles(process=int(receipt.hProcess), thread=int(receipt.hThread))
            child = WindowsChild(
                self._api, self._job, handles, int(receipt.dwProcessId), input_stream
            )
            self._process = child  # Retain ownership before resume/handle cleanup.
            self._api.resume_thread(handles.thread)
            self._api.close_handle(handles.thread)
            handles.thread = None
            return child
        except Exception:
            raise ProcessTreeError("ci_process_tree_start_failed") from None
        finally:
            cleanup_failed = False
            if initialized:
                try:
                    kernel.DeleteProcThreadAttributeList(attributes)
                except Exception:
                    cleanup_failed = True
            for fd in descriptors:
                try:
                    os.close(fd)
                except Exception:
                    cleanup_failed = True
            if self._process is None and input_stream is not None:
                try:
                    input_stream.close()
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise ProcessTreeError("ci_process_tree_start_failed") from None

    def attach(self, process: subprocess.Popen[bytes]) -> None:
        """POSIX-only receipt; Windows must use atomic start, never late attach."""
        if self._job is not None or self._closed or self._process is not None:
            raise ProcessTreeError("ci_process_tree_attach_failed")
        self._process = process
        try:
            if sys.platform == "win32" or process.poll() is not None:
                raise ValueError
            if os.getpgid(process.pid) != process.pid or os.getsid(process.pid) != process.pid:
                raise ValueError
            self._group = process.pid
        except Exception:
            raise ProcessTreeError("ci_process_tree_attach_failed") from None

    def _members(self, deadline: float) -> set[int]:
        if self._group is None:
            return set()
        if sys.platform == "win32":
            raise ProcessTreeError("ci_process_tree_cleanup_failed")
        try:
            os.killpg(self._group, 0)
        except ProcessLookupError:
            return set()
        if not sys.platform.startswith("linux"):
            # Other POSIX kernels expose no stdlib group-accounting interface.
            return {self._group}
        members: set[int] = set()
        with os.scandir("/proc") as entries:
            for count, entry in enumerate(entries):
                if count >= _MAX_PROC_ENTRIES or time.monotonic() >= deadline:
                    raise ProcessTreeError("ci_process_tree_cleanup_failed")
                if not entry.name.isdecimal():
                    continue
                try:
                    # stat's comm may contain spaces/parentheses. Only the
                    # fixed numeric suffix and state are used, never retained.
                    with (Path(entry.path) / "stat").open("rb") as stream:
                        fields = stream.read(4096).rpartition(b")")[2].split()
                    if int(fields[2]) == self._group and fields[0] not in (b"Z", b"X"):
                        members.add(int(entry.name))
                except (FileNotFoundError, ProcessLookupError):
                    continue
        return members

    def close(self, process: Child | None) -> bool:
        """Kill this owned tree and reap its leader within one bounded budget.

        Return whether live descendants (excluding the leader) remained when
        cleanup began. A nonzero/expired leader does not release subtree ownership.
        Windows handle release requires authoritative zero active job members.
        Errors retain ownership state and report fixed failures.
        """
        if process is None:
            process = self._process
        if self._process is not None and self._process is not process:
            raise ProcessTreeError("ci_process_tree_cleanup_failed")
        if self._closed:
            return self._descendants
        if process is None:
            try:
                if self._job is not None:
                    if self._api.active_count(self._job) != 0:
                        raise ValueError
                    self._api.close_handle(self._job)
                    self._job = None
                self._closed = True
                return False
            except Exception:
                raise ProcessTreeError("ci_process_tree_cleanup_failed") from None
        deadline = time.monotonic() + _CLEANUP_SECONDS
        try:
            if self._job is not None:
                count = self._api.active_count(self._job)
                self._descendants = count > int(process.poll() is None)
                if count:
                    self._api.terminate_job(self._job)
            elif self._group is not None:
                members = self._members(deadline)
                self._descendants = bool(members - {process.pid})
                if members:
                    if sys.platform == "win32":
                        raise ValueError
                    os.killpg(self._group, signal.SIGKILL)
            # An attach failure can leave an unassigned child. It is still gated
            # and cannot have descendants, so direct termination is sufficient.
            if process.poll() is None and not isinstance(process, WindowsChild):
                process.kill()
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            while True:
                if self._job is not None:
                    remaining = self._api.active_count(self._job) != 0
                else:
                    remaining = bool(self._members(deadline))
                if not remaining:
                    break
                if time.monotonic() >= deadline:
                    raise ProcessTreeError("ci_process_tree_cleanup_failed")
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            if self._job is not None:
                if isinstance(process, WindowsChild):
                    process.stdin.close()
                    for name in ("thread", "process"):
                        handle = getattr(process.handles, name)
                        if handle is not None:
                            self._api.close_handle(handle)
                            setattr(process.handles, name, None)
                self._api.close_handle(self._job)
                self._job = None
                if isinstance(process, WindowsChild):
                    process.job = None
            self._closed = True
            return self._descendants
        except Exception:
            raise ProcessTreeError("ci_process_tree_cleanup_failed") from None
