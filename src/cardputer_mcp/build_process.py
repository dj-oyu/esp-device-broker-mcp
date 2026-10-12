"""Fixed local commands with bounded output and owned process-tree teardown."""

import ctypes
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import BinaryIO, cast

from . import windows_process as win


class PipeWindowsAPI(win.NativeWindowsAPI):
    def __init__(self, stdout_handle: int, stderr_handle: int) -> None:
        super().__init__()
        self.stdout_handle = stdout_handle
        self.stderr_handle = stderr_handle

    def create_suspended(
        self,
        job: int,
        args: Sequence[str],
        env: Mapping[str, str],
        cwd: str,
        handles: win.ProcessHandles,
    ) -> None:
        security = win._SecurityAttributes(ctypes.sizeof(win._SecurityAttributes), None, True)
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
            raise win.WindowsProcessError("owned_null_handle_failed")
        handles.auxiliary.append(int(null))
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={env[key]}" for key in sorted(env, key=str.upper)) + "\0\0"
        )
        initialized = False
        attributes = ctypes.c_void_p()
        try:
            size = win._SIZE_T()
            self._kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(size))
            if not size.value:
                raise win.WindowsProcessError("owned_process_attributes_failed")
            storage = ctypes.create_string_buffer(size.value)
            attributes = ctypes.cast(storage, ctypes.c_void_p)
            if not self._kernel.InitializeProcThreadAttributeList(
                attributes,
                2,
                0,
                ctypes.byref(size),
            ):
                raise win.WindowsProcessError("owned_process_attributes_failed")
            initialized = True
            inherited = (win._HANDLE * 3)(null, self.stdout_handle, self.stderr_handle)
            jobs = (win._HANDLE * 1)(job)
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
                    raise win.WindowsProcessError("owned_process_attributes_failed")
            startup = win._StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = null
            startup.StartupInfo.hStdOutput = self.stdout_handle
            startup.StartupInfo.hStdError = self.stderr_handle
            startup.lpAttributeList = attributes
            process = win._ProcessInformation()
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
                raise win.WindowsProcessError("owned_process_create_failed")
            # Record ownership before any subsequent cleanup operation can fail.
            handles.process = int(process.hProcess)
            handles.thread = int(process.hThread)
        finally:
            ctypes.memset(ctypes.addressof(environment), 0, ctypes.sizeof(environment))
            if initialized:
                self._kernel.DeleteProcThreadAttributeList(attributes)
            self.close_handle(int(null))
            handles.auxiliary.remove(int(null))



class BuildProcess:
    """Retain uncertain ownership; never stop a searched PID or process name."""
    def __init__(self) -> None:
        self.stop_requested = threading.Event()
        self.active = False
        self.uncertain = False
        self.owner: win.OwnedWindowsProcess | None = None

    def run(self, args: Sequence[str], env: Mapping[str, str], cwd: Path,
            emit: Callable[[str, str], None], timeout: float = 1800) -> int:
        if self.active or self.uncertain:
            raise ValueError("build_process_busy")
        if self.stop_requested.is_set():
            raise ValueError("build_interrupted_needs_review")
        self.active = True
        readers: list[threading.Thread] = []
        pipes: list[BinaryIO] = []
        process = None
        try:
            if os.name == "nt":
                import msvcrt
                stdout_read, stdout_write = os.pipe()
                stderr_read, stderr_write = os.pipe()
                try:
                    get_handle = getattr(msvcrt, "get_osfhandle", None)
                    set_inheritable = getattr(os, "set_handle_inheritable", None)
                    if get_handle is None or set_inheritable is None:
                        raise win.WindowsProcessError("owned_windows_api_unavailable")
                    out = get_handle(stdout_write)
                    err = get_handle(stderr_write)
                    set_inheritable(out, True)
                    set_inheritable(err, True)
                    api = PipeWindowsAPI(out, err)
                    job = api.create_job()
                    self.owner = win.OwnedWindowsProcess(api, job)
                    api.configure_job(job)
                    if self.stop_requested.is_set():
                        raise ValueError("build_interrupted_needs_review")
                    api.create_suspended(job, args, env, str(cwd), self.owner._handles)
                    thread = self.owner._handles.thread
                    if self.owner._handles.process is None or thread is None:
                        raise win.WindowsProcessError("owned_process_handles_missing")
                    if self.stop_requested.is_set():
                        raise ValueError("build_interrupted_needs_review")
                    api.resume_thread(thread)
                    api.close_handle(thread)
                    self.owner._handles.thread = None
                except BaseException:
                    os.close(stdout_read)
                    os.close(stderr_read)
                    raise
                finally:
                    os.close(stdout_write)
                    os.close(stderr_write)
                pipes = [cast(BinaryIO, os.fdopen(stdout_read, "rb", buffering=0)),
                         cast(BinaryIO, os.fdopen(stderr_read, "rb", buffering=0))]
            else:
                if self.stop_requested.is_set():
                    raise ValueError("build_interrupted_needs_review")
                process = subprocess.Popen(list(args), cwd=cwd, env=dict(env),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, start_new_session=True)
                assert process.stdout is not None and process.stderr is not None
                pipes = [cast(BinaryIO, process.stdout), cast(BinaryIO, process.stderr)]

            def consume(pipe: BinaryIO, stream: str) -> None:
                pending = bytearray()
                oversized = False
                while chunk := pipe.read(1024):
                    for byte in chunk:
                        if byte == 10:
                            if oversized:
                                emit("warning: oversized output line omitted", stream)
                            else:
                                emit(pending.decode("utf-8", errors="replace").rstrip("\r"), stream)
                            pending.clear()
                            oversized = False
                        elif not oversized:
                            pending.append(byte)
                            if len(pending) > 4096:
                                pending.clear()
                                oversized = True
                if oversized:
                    emit("warning: oversized output line omitted", stream)
                elif pending:
                    emit(pending.decode("utf-8", errors="replace"), stream)

            for pipe, stream in zip(pipes, ("stdout", "stderr"), strict=True):
                reader = threading.Thread(target=consume, args=(pipe, stream), daemon=True)
                readers.append(reader)
                reader.start()
            deadline = time.monotonic() + timeout
            while True:
                if self.owner is not None:
                    code = self.owner.poll()
                else:
                    assert process is not None
                    code = process.poll()
                if code is not None:
                    break
                if self.stop_requested.wait(0.02) or time.monotonic() >= deadline:
                    raise ValueError("build_interrupted_needs_review")
            if self.stop_requested.is_set():
                raise ValueError("build_interrupted_needs_review")
            return code
        finally:
            self.uncertain = True
            stopped = True
            if self.owner is not None:
                stopped = self.owner.stop(5)
                if stopped:
                    self.owner.close()
                    self.owner = None
            elif process is not None:
                kill_group = getattr(os, "killpg", None)
                kill_signal = getattr(signal, "SIGKILL", None)
                if kill_group is None or kill_signal is None:
                    raise ValueError("build_tree_stop_unconfirmed")
                try:
                    kill_group(process.pid, kill_signal)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                try:
                    kill_group(process.pid, 0)
                    stopped = False
                except ProcessLookupError:
                    pass
            for reader in readers:
                reader.join(timeout=5)
                stopped = stopped and not reader.is_alive()
            if stopped:
                for pipe in pipes:
                    pipe.close()
            self.uncertain = not stopped
            self.active = not stopped
            if not stopped:
                raise ValueError("build_tree_stop_unconfirmed")
