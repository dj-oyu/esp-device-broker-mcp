"""Native handle-relative reader; deliberately not connected to MCP/build yet.

Only local NTFS is supported. Ancestors remain open without delete/write sharing.
This is not an OS sandbox or protection against administrators/kernel code.
"""

import ctypes as c
import hashlib
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import PureWindowsPath
from typing import Any

from .private_input_contract import InputFile, _inventory
from .private_input_registration import PrivateInputRegistration
from .private_input_store import inventory_digest


class PrivateInputReadError(ValueError):
    pass


class _UnicodeString(c.Structure):
    _fields_ = [("length", c.c_ushort), ("maximum", c.c_ushort), ("buffer", c.c_wchar_p)]


class _ObjectAttributes(c.Structure):
    _fields_ = [
        ("length", c.c_ulong),
        ("root", c.c_void_p),
        ("name", c.POINTER(_UnicodeString)),
        ("attributes", c.c_ulong),
        ("security", c.c_void_p),
        ("qos", c.c_void_p),
    ]


class _IoStatus(c.Structure):
    _fields_ = [("status", c.c_void_p), ("information", c.c_size_t)]


class _Tag(c.Structure):
    _fields_ = [("attributes", c.c_ulong), ("tag", c.c_ulong)]


class _Standard(c.Structure):
    _fields_ = [
        ("allocation", c.c_longlong),
        ("size", c.c_longlong),
        ("links", c.c_ulong),
        ("delete_pending", c.c_ubyte),
        ("directory", c.c_ubyte),
    ]


def _root_parts(value: str) -> tuple[str, ...]:
    if not re.match(r"^[A-Za-z]:\\", value) or "/" in value:
        raise PrivateInputReadError("private_input_root_unsupported")
    parts = value[3:].split("\\")
    if any(
        not p or p in {".", ".."} or p.endswith((".", " ")) or any(x in p for x in ":*?<>|\x00")
        for p in parts
    ):
        raise PrivateInputReadError("private_input_root_unsupported")
    return (value[:3], *parts)


class _Native:
    k: Any
    n: Any

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise PrivateInputReadError("private_input_windows_required")
        from ctypes import wintypes as w

        self.k = c.WinDLL("kernel32", use_last_error=True)
        self.n = c.WinDLL("ntdll")
        self.k.CreateFileW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            w.DWORD,
            w.LPVOID,
            w.DWORD,
            w.DWORD,
            w.HANDLE,
        ]
        self.k.CreateFileW.restype = w.HANDLE
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.k.CloseHandle.restype = w.BOOL
        self.k.GetDriveTypeW.argtypes = [w.LPCWSTR]
        self.k.GetDriveTypeW.restype = w.UINT
        self.k.GetVolumeInformationByHandleW.argtypes = [
            w.HANDLE,
            w.LPWSTR,
            w.DWORD,
            c.POINTER(w.DWORD),
            c.POINTER(w.DWORD),
            c.POINTER(w.DWORD),
            w.LPWSTR,
            w.DWORD,
        ]
        self.k.GetVolumeInformationByHandleW.restype = w.BOOL
        self.k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD]
        self.k.GetFileInformationByHandleEx.restype = w.BOOL
        self.k.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID]
        self.k.ReadFile.restype = w.BOOL
        self.n.NtCreateFile.argtypes = [
            c.POINTER(w.HANDLE),
            w.DWORD,
            c.POINTER(_ObjectAttributes),
            c.POINTER(_IoStatus),
            w.LPVOID,
            w.ULONG,
            w.ULONG,
            w.ULONG,
            w.ULONG,
            w.LPVOID,
            w.ULONG,
        ]
        self.n.NtCreateFile.restype = c.c_long

    def close(self, handle: int) -> None:
        self.k.CloseHandle(handle)

    def info(self, handle: int, directory: bool) -> _Standard:
        tag, standard = _Tag(), _Standard()
        if not self.k.GetFileInformationByHandleEx(
            handle, 9, c.byref(tag), c.sizeof(tag)
        ) or not self.k.GetFileInformationByHandleEx(
            handle, 1, c.byref(standard), c.sizeof(standard)
        ):
            raise PrivateInputReadError("private_input_handle_info_failed")
        if tag.attributes & 0x400 or bool(standard.directory) != directory:
            raise PrivateInputReadError("private_input_type_rejected")
        if standard.delete_pending or (not directory and standard.links != 1):
            raise PrivateInputReadError("private_input_links_rejected")
        return standard

    def anchor(self, drive: str, stack: ExitStack) -> int:
        if self.k.GetDriveTypeW(drive) != 3:  # DRIVE_FIXED
            raise PrivateInputReadError("private_input_volume_unsupported")
        handle = self.k.CreateFileW(drive, 0x80000000, 1, None, 3, 0x02200000, None)
        if handle in (None, c.c_void_p(-1).value):
            raise PrivateInputReadError("private_input_open_failed")
        stack.callback(self.close, handle)
        self.info(handle, True)
        fs = c.create_unicode_buffer(32)
        if (
            not self.k.GetVolumeInformationByHandleW(handle, None, 0, None, None, None, fs, 32)
            or fs.value != "NTFS"
        ):
            raise PrivateInputReadError("private_input_volume_unsupported")
        return int(handle)

    def child(self, parent: int, name: str, directory: bool, stack: ExitStack) -> int:
        buf = c.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        if length > 65532:
            raise PrivateInputReadError("private_input_name_limit")
        u = _UnicodeString(length, length + 2, c.cast(buf, c.c_wchar_p))
        attr = _ObjectAttributes(
            c.sizeof(_ObjectAttributes), parent, c.pointer(u), 0x40, None, None
        )
        io, handle = _IoStatus(), c.c_void_p()
        status = self.n.NtCreateFile(
            c.byref(handle),
            0x80100000,
            c.byref(attr),
            c.byref(io),
            None,
            0,
            1,
            1,
            0x200020 | (1 if directory else 0x40),
            None,
            0,
        )
        if status < 0 or not handle.value:
            raise PrivateInputReadError("private_input_open_failed")
        stack.callback(self.close, handle.value)
        self.info(handle.value, directory)
        return handle.value

    def read(self, handle: int, size: int, cancelled: Callable[[], bool]) -> bytes:
        if self.info(handle, False).size != size:
            raise PrivateInputReadError("private_input_size_changed")
        result = bytearray()
        while len(result) <= size:
            if cancelled():
                raise PrivateInputReadError("private_input_cancelled")
            length = min(65536, size + 1 - len(result))
            buf, count = c.create_string_buffer(length), c.c_ulong()
            if not self.k.ReadFile(handle, buf, length, c.byref(count), None):
                raise PrivateInputReadError("private_input_read_failed")
            if not count.value:
                break
            result.extend(buf.raw[: count.value])
        if len(result) != size or self.info(handle, False).size != size:
            raise PrivateInputReadError("private_input_size_changed")
        return bytes(result)


def read_registered_windows(
    registration: PrivateInputRegistration,
    manifest: tuple[InputFile, ...],
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> Iterator[tuple[str, bytes]]:
    """Yield only declared bytes; callers must close the generator on early exit.

    The registered root must come from trusted local configuration. This function
    does not authorize import, create receipts, or mutate the source/cache.
    """
    _inventory(manifest)
    if (
        len(manifest) > registration.max_files
        or sum(f.size for f in manifest) > registration.max_bytes
        or inventory_digest(manifest) != registration.inventory_sha256
    ):
        raise PrivateInputReadError("private_input_inventory_mismatch")
    parts = _root_parts(str(registration.local_root))
    native = _Native()
    try:
        with ExitStack() as roots:
            current = native.anchor(parts[0], roots)
            for part in parts[1:]:
                if cancelled():
                    raise PrivateInputReadError("private_input_cancelled")
                current = native.child(current, part, True, roots)
            for item in manifest:
                if cancelled():
                    raise PrivateInputReadError("private_input_cancelled")
                with ExitStack() as handles:
                    parent = current
                    components = PureWindowsPath(item.path).parts
                    for part in components[:-1]:
                        parent = native.child(parent, part, True, handles)
                    handle = native.child(parent, components[-1], False, handles)
                    payload = native.read(handle, item.size, cancelled)
                    if hashlib.sha256(payload).hexdigest() != item.sha256:
                        raise PrivateInputReadError("private_input_bytes_changed")
                yield item.path, payload
    except OSError:
        raise PrivateInputReadError("private_input_os_error") from None
