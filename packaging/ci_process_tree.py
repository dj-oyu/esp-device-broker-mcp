"""CI-only ownership for a stdin-gated child and its descendants.

The caller starts only stdlib gate code, calls attach, then releases the gate.
POSIX callers must use start_new_session=True. Groups cannot contain descendants
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
from pathlib import Path
from typing import Any

from cardputer_mcp.windows_process import NativeWindowsAPI

_WINDOWS = os.name == "nt"
_CLEANUP_SECONDS = 8.0
_MAX_PROC_ENTRIES = 100000


class ProcessTreeError(RuntimeError):
    """Fixed CI failure codes; never native error text or process arguments."""


class ProcessTree:
    def __init__(self) -> None:
        self._api: Any = None
        self._job: int | None = None
        self._process: subprocess.Popen[Any] | None = None
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

    def attach(self, process: subprocess.Popen[Any]) -> None:
        """Bind the still-gated leader before it may import/run any test code."""
        if self._closed or self._process is not None:
            raise ProcessTreeError("ci_process_tree_attach_failed")
        self._process = process
        try:
            if process.poll() is not None:
                raise ValueError
            if self._job is not None:
                kernel = self._api._kernel
                kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                kernel.AssignProcessToJobObject.restype = ctypes.c_int32
                # PROCESS_SET_QUOTA | PROCESS_TERMINATE; temporary, not inherited.
                handle = kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
                if not handle:
                    raise ValueError
                try:
                    if not kernel.AssignProcessToJobObject(self._job, handle):
                        raise ValueError
                finally:
                    self._api.close_handle(int(handle))
            else:
                if sys.platform == "win32":
                    raise ValueError
                if os.getpgid(process.pid) != process.pid or os.getsid(process.pid) != process.pid:
                    raise ValueError
                self._group = process.pid
        except Exception:
            # The caller must keep the gate closed and call close even on failure.
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

    def close(self, process: subprocess.Popen[Any] | None) -> bool:
        """Kill this owned tree and reap its leader within one bounded budget.

        Return whether live descendants (excluding the leader) remained when
        cleanup began. A nonzero/expired leader does not release subtree ownership.
        Windows handle release requires authoritative zero active job members.
        Errors retain ownership state and report fixed failures.
        """
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
            if process.poll() is None:
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
                self._api.close_handle(self._job)
                self._job = None
            self._closed = True
            return self._descendants
        except Exception:
            raise ProcessTreeError("ci_process_tree_cleanup_failed") from None
