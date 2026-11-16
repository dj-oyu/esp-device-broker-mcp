"""Disconnected Windows NTFS build staging for trusted in-process plans.

Creates a protected DACL on new objects only. Not a sandbox: build processes run
as the broker identity and must be stopped/joined before context exit. Same-user
hostile code, administrators, and kernel attackers are outside this boundary.
"""

import ctypes as c
import hashlib
import importlib
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

from .private_input_contract import InputFile, _hash, _inventory
from .private_input_windows import (
    _IoStatus,
    _Native,
    _ObjectAttributes,
    _root_parts,
    _UnicodeString,
)
from .private_workspace_plan import WorkspacePlan


class WindowsWorkspaceError(ValueError):
    pass


class _Scratch(_Native):
    def __init__(self) -> None:
        super().__init__()
        win32api = importlib.import_module("win32api")
        win32con = importlib.import_module("win32con")
        security = importlib.import_module("win32security")

        with win32security_token(security, win32api, win32con) as token:
            self.sid = security.GetTokenInformation(token, security.TokenUser)[0]
        self.security = security
        sid = security.ConvertSidToStringSid(self.sid)
        self.descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)", 1
        )
        self.a = c.WinDLL("advapi32", use_last_error=True)
        self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            c.c_wchar_p,
            c.c_ulong,
            c.POINTER(c.c_void_p),
            c.POINTER(c.c_ulong),
        ]
        self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = c.c_int
        self.k.LocalFree.argtypes = [c.c_void_p]
        self.k.LocalFree.restype = c.c_void_p
        self.k.WriteFile.argtypes = [
            c.c_void_p,
            c.c_void_p,
            c.c_ulong,
            c.POINTER(c.c_ulong),
            c.c_void_p,
        ]
        self.k.WriteFile.restype = c.c_int
        self.k.FlushFileBuffers.argtypes = [c.c_void_p]
        self.k.FlushFileBuffers.restype = c.c_int
        self.k.SetFileInformationByHandle.argtypes = [c.c_void_p, c.c_int, c.c_void_p, c.c_ulong]
        self.k.SetFileInformationByHandle.restype = c.c_int
        self.sddl = f"D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"

    def open_object(self, parent: int, name: str, directory: bool, *, create: bool) -> int:
        # Single validated component only; never resolve caller-provided paths.
        if not name or name in {".", ".."} or any(x in name for x in "\\/:\x00"):
            raise WindowsWorkspaceError("private_workspace_component_invalid")
        raw = name.encode("utf-16-le")
        if len(raw) > 65532:
            raise WindowsWorkspaceError("private_workspace_component_invalid")
        buf = c.create_unicode_buffer(name)
        u = _UnicodeString(len(raw), len(raw) + 2, c.cast(buf, c.c_wchar_p))
        sd = c.c_void_p()
        if create and not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            self.sddl, 1, c.byref(sd), None
        ):
            raise WindowsWorkspaceError("private_workspace_acl_failed")
        try:
            attr = _ObjectAttributes(
                c.sizeof(_ObjectAttributes), parent, c.pointer(u), 0x40, sd, None
            )
            io, handle = _IoStatus(), c.c_void_p()
            status = self.n.NtCreateFile(
                c.byref(handle),
                0xC0130000,
                c.byref(attr),
                c.byref(io),
                None,
                0,
                3,
                2 if create else 1,
                0x200020 | (1 if directory else 0x40),
                None,
                0,
            )
            if status < 0 or not handle.value:
                raise WindowsWorkspaceError("private_workspace_open_failed")
            return handle.value
        finally:
            if sd.value:
                self.k.LocalFree(sd)

    def verify_acl(self, handle: int) -> None:
        s = self.security
        descriptor = s.GetSecurityInfo(handle, s.SE_FILE_OBJECT, s.DACL_SECURITY_INFORMATION)
        if not descriptor.GetSecurityDescriptorControl()[0] & s.SE_DACL_PROTECTED:
            raise WindowsWorkspaceError("private_workspace_acl_unprotected")
        acl = descriptor.GetSecurityDescriptorDacl()
        expected = {s.ConvertSidToStringSid(self.sid), "S-1-5-18"}
        seen = set()
        if acl is None:
            raise WindowsWorkspaceError("private_workspace_acl_missing")
        for i in range(acl.GetAceCount()):
            (kind, flags), mask, sid = acl.GetAce(i)
            text = s.ConvertSidToStringSid(sid)
            if kind != 0 or flags != 3 or mask != 0x1F01FF or text not in expected:
                raise WindowsWorkspaceError("private_workspace_acl_unexpected")
            seen.add(text)
        if seen != expected:
            raise WindowsWorkspaceError("private_workspace_acl_incomplete")

    def remove(self, handle: int) -> None:
        delete = c.c_ubyte(1)
        if not self.k.SetFileInformationByHandle(handle, 4, c.byref(delete), c.sizeof(delete)):
            raise WindowsWorkspaceError("private_workspace_cleanup_failed")

    def clean(self, handle: int, path: Path) -> None:
        # Paths are used only to enumerate names under pinned ancestors; each
        # object is opened relative/no-follow before deletion. Never recurse into
        # a reparse point; failures retain scratch and fail the operation.
        with os.scandir(path) as listing:
            names = [entry.name for entry in listing]
        for name in names:
            child = None
            try:
                # OPEN_REPARSE_POINT with no directory constraint lets cleanup
                # remove junctions themselves, never their destinations.
                child = self.open_cleanup(handle, name)
                from .private_input_windows import _Tag

                tag = _Tag()
                if not self.k.GetFileInformationByHandleEx(child, 9, c.byref(tag), c.sizeof(tag)):
                    raise WindowsWorkspaceError("private_workspace_cleanup_failed")
                if tag.attributes & 0x10 and not tag.attributes & 0x400:
                    self.clean(child, path / name)
                self.remove(child)
            finally:
                if child is not None:
                    self.close(child)

    def open_cleanup(self, parent: int, name: str) -> int:
        buf = c.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        u = _UnicodeString(length, length + 2, c.cast(buf, c.c_wchar_p))
        attr = _ObjectAttributes(
            c.sizeof(_ObjectAttributes), parent, c.pointer(u), 0x40, None, None
        )
        handle, io = c.c_void_p(), _IoStatus()
        status = self.n.NtCreateFile(
            c.byref(handle),
            0x80110000,
            c.byref(attr),
            c.byref(io),
            None,
            0,
            3,
            1,
            0x200020,
            None,
            0,
        )
        if status < 0 or not handle.value:
            raise WindowsWorkspaceError("private_workspace_cleanup_failed")
        return handle.value


@contextmanager
def win32security_token(security: Any, api: Any, con: Any) -> Iterator[Any]:
    token = security.OpenProcessToken(api.GetCurrentProcess(), con.TOKEN_QUERY)
    try:
        yield token
    finally:
        token.Close()


@contextmanager
def materialize_windows_workspace(
    plan: WorkspacePlan, *, container: Path, cancelled: Callable[[], bool] = lambda: False
) -> Iterator[Path]:
    """Use only trusted container and authenticated plan; not an authorization API."""
    _hash(plan.identity)
    if any(type(data) is not bytes for _, data in plan.files):
        raise WindowsWorkspaceError("private_workspace_bytes_required")
    _inventory(tuple(InputFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in plan.files))
    parts = _root_parts(str(container))
    native = _Scratch()

    def check() -> None:
        if cancelled():
            raise WindowsWorkspaceError("private_workspace_cancelled")

    with ExitStack() as roots:
        check()
        parent = native.anchor(parts[0], roots)
        for part in parts[1:]:
            check()
            parent = native.child(parent, part, True, roots)
        name = "private-work-" + secrets.token_hex(16)
        root = native.open_object(parent, name, True, create=True)
        path = container / name
        try:
            native.info(root, True)
            native.verify_acl(root)
            for filename, payload in plan.files:
                check()
                with ExitStack() as children:
                    current = root
                    for part in filename.split("/")[:-1]:
                        try:
                            child = native.open_object(current, part, True, create=True)
                        except WindowsWorkspaceError:
                            child = native.open_object(current, part, True, create=False)
                        children.callback(native.close, child)
                        native.info(child, True)
                        native.verify_acl(child)
                        current = child
                    child = native.open_object(current, filename.split("/")[-1], False, create=True)
                    children.callback(native.close, child)
                    native.info(child, False)
                    native.verify_acl(child)
                    for offset in range(0, len(payload), 65536):
                        check()
                        block = payload[offset : offset + 65536]
                        count = c.c_ulong()
                        if not native.k.WriteFile(
                            child, block, len(block), c.byref(count), None
                        ) or count.value != len(block):
                            raise WindowsWorkspaceError("private_workspace_write_failed")
                    if not native.k.FlushFileBuffers(child):
                        raise WindowsWorkspaceError("private_workspace_write_failed")
                with ExitStack() as verification:
                    # Directory ancestors above remain protected by ACL; hostile
                    # processes with the broker identity are not in scope.
                    current = root
                    for part in filename.split("/")[:-1]:
                        current = native.child(current, part, True, verification)
                    file = native.child(current, filename.split("/")[-1], False, verification)
                    if native.read(file, len(payload), cancelled) != payload:
                        raise WindowsWorkspaceError("private_workspace_write_mismatch")
            check()
            yield path
        finally:
            try:
                native.clean(root, path)
                native.remove(root)
            finally:
                native.close(root)

