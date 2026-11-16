"""Read-only Windows private-state preflight; no ACL/configuration mutation.

Must be held for the entire private operation. Does not install a runtime adapter.
The protected parent excludes other users; hostile same-identity code is not covered.
"""

import ctypes as c
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from .private_input_windows import _IoStatus, _ObjectAttributes, _root_parts, _UnicodeString
from .private_workspace_windows import _Scratch

if TYPE_CHECKING:
    from .store import Store


class _StateNative(_Scratch):
    def state_file(self, parent: int, name: str, handles: ExitStack) -> int:
        if not name or any(char in name for char in "/\\:\x00") or name in {".", ".."}:
            raise ValueError("private_state_path_invalid")
        buf = c.create_unicode_buffer(name)
        length = len(name.encode("utf-16-le"))
        if length > 65532:
            raise ValueError("private_state_path_invalid")
        u = _UnicodeString(length, length + 2, c.cast(buf, c.c_wchar_p))
        attr = _ObjectAttributes(
            c.sizeof(_ObjectAttributes), parent, c.pointer(u), 0x40, None, None
        )
        handle, io = c.c_void_p(), _IoStatus()
        # Metadata+security read only; share read/write for SQLite, never delete.
        status = self.n.NtCreateFile(
            c.byref(handle), 0x120080, c.byref(attr), c.byref(io), None, 0, 3, 1, 0x200060, None, 0
        )
        if status < 0 or not handle.value:
            raise ValueError("private_state_open_failed")
        handles.callback(self.close, handle.value)
        self.info(handle.value, False)  # no reparse, no multiple hardlinks
        return handle.value

    def file_acl(self, handle: int) -> None:
        security = self.security
        descriptor = security.GetSecurityInfo(
            handle,
            security.SE_FILE_OBJECT,
            security.DACL_SECURITY_INFORMATION | security.OWNER_SECURITY_INFORMATION,
        )
        owner = descriptor.GetSecurityDescriptorOwner()
        sid = security.ConvertSidToStringSid(self.sid)
        if owner is None or security.ConvertSidToStringSid(owner) != sid:
            raise ValueError("private_state_owner_unexpected")
        acl = descriptor.GetSecurityDescriptorDacl()
        if acl is None:
            raise ValueError("private_state_acl_missing")
        seen = set()
        for i in range(acl.GetAceCount()):
            (kind, flags), mask, ace_sid = acl.GetAce(i)
            value = security.ConvertSidToStringSid(ace_sid)
            # Inherited file ACEs are valid under the explicitly protected parent.
            if kind != 0 or mask != 0x1F01FF or value not in {sid, "S-1-5-18"} or flags & 8:
                raise ValueError("private_state_acl_unexpected")
            seen.add(value)
        if seen != {sid, "S-1-5-18"}:
            raise ValueError("private_state_acl_incomplete")


@contextmanager
def verified_private_database(store: "Store") -> Iterator[None]:
    """Pin native ancestors/DB identity and validate DB/sidecar access restrictions.

    SQLite WAL/SHM handles are closed after metadata checks so SQLite can recycle
    sidecars. The protected parent remains pinned. Same-user SQLite is trusted.
    No table creation or permission update occurs here; failure closes only the
    private path, not ordinary broker functionality.
    """
    entries = tuple(store.db.execute("PRAGMA database_list"))
    main = [row[2] for row in entries if row[1] == "main"]
    if len(main) != 1 or not main[0] or any(row[1] not in {"main", "temp"} for row in entries):
        raise ValueError("private_state_database_invalid")
    path = Path(main[0])
    parts = _root_parts(str(path.parent))
    native = _StateNative()
    with ExitStack() as held:
        parent = native.anchor(parts[0], held)
        for part in parts[1:]:
            parent = native.child(parent, part, True, held)
        native.verify_acl(parent)  # explicit protected current-user/SYSTEM parent
        handle = native.state_file(parent, path.name, held)
        native.file_acl(handle)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = path.with_name(path.name + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            with ExitStack() as sidecar_handles:
                handle = native.state_file(parent, sidecar.name, sidecar_handles)
                native.file_acl(handle)
        yield
