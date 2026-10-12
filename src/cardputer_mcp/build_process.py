"""Fixed local commands with bounded output and owned process-tree teardown."""

import ctypes
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .windows_process import (
    NativeWindowsAPI, OwnedWindowsProcess, ProcessHandles, WindowsProcessError,
    _SecurityAttributes, _SIZE_T, _HANDLE, _StartupInfoEx, _ProcessInformation,
)


class PipeWindowsAPI(NativeWindowsAPI):
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
            inherited = (_HANDLE * 3)(null, self.stdout_handle, self.stderr_handle)
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
            startup.StartupInfo.hStdOutput = self.stdout_handle
            startup.StartupInfo.hStdError = self.stderr_handle
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



class BuildProcess:
    """Retain uncertain ownership; never stop a searched PID or process name."""
    def __init__(self) -> None:
        self.stop_requested = threading.Event()
        self.active = False
        self.uncertain = False
        self.owner: OwnedWindowsProcess | None = None

    def run(self, args: Sequence[str], env: Mapping[str, str], cwd: Path,
            emit: Callable[[str, str], None], timeout: float = 1800) -> int:
        if self.active or self.uncertain:
            raise ValueError("build_process_busy")
        self.active = True
        readers: list[threading.Thread] = []
        pipes: list[object] = []
        process = None
        try:
            if os.name == "nt":
                import msvcrt
                stdout_read, stdout_write = os.pipe()
                stderr_read, stderr_write = os.pipe()
                try:
                    out = msvcrt.get_osfhandle(stdout_write)
                    err = msvcrt.get_osfhandle(stderr_write)
                    os.set_handle_inheritable(out, True)
                    os.set_handle_inheritable(err, True)
                    api = PipeWindowsAPI(out, err)
                    self.owner = OwnedWindowsProcess(api, api.create_job())
                    api.configure_job(self.owner._job)
                    api.create_suspended(self.owner._job, args, env, str(cwd), self.owner._handles)
                    api.resume_thread(self.owner._handles.thread)
                    api.close_handle(self.owner._handles.thread)
                    self.owner._handles.thread = None
                except BaseException:
                    os.close(stdout_read)
                    os.close(stderr_read)
                    raise
                finally:
                    os.close(stdout_write)
                    os.close(stderr_write)
                pipes = [os.fdopen(stdout_read, "rb", buffering=0),
                         os.fdopen(stderr_read, "rb", buffering=0)]
            else:
                process = subprocess.Popen(list(args), cwd=cwd, env=dict(env),
                                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, start_new_session=True)
                pipes = [process.stdout, process.stderr]

            def consume(pipe: object, stream: str) -> None:
                pending = bytearray()
                while chunk := pipe.read(1024):
                    for byte in chunk:
                        if byte == 10 or len(pending) >= 4096:
                            emit(pending.decode("utf-8", errors="replace").rstrip("\r"), stream)
                            pending.clear()
                        if byte != 10:
                            pending.append(byte)
                if pending:
                    emit(pending.decode("utf-8", errors="replace"), stream)

            for pipe, stream in zip(pipes, ("stdout", "stderr"), strict=True):
                reader = threading.Thread(target=consume, args=(pipe, stream), daemon=True)
                readers.append(reader)
                reader.start()
            deadline = time.monotonic() + timeout
            while True:
                code = self.owner.poll() if self.owner else process.poll()
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
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
                try:
                    os.killpg(process.pid, 0)
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
