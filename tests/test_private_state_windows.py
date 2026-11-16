"""Synthetic gate orchestration; native access-control proof is separate."""

import sqlite3
from types import SimpleNamespace

import pytest

from cardputer_mcp import private_state_windows as module
from cardputer_mcp.store import Store


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    store = Store(tmp_path / "fixture.sqlite3")
    events = []

    class Native:
        def anchor(self, name, stack):
            events.append(("anchor", name))
            stack.callback(events.append, ("close", name))
            return 1

        def child(self, parent, name, directory, stack):
            events.append(("child", name))
            stack.callback(events.append, ("close", name))
            return 2

        def verify_acl(self, parent):
            events.append(("parent_acl", parent))

        def state_file(self, parent, name, stack):
            events.append(("file", name))
            stack.callback(events.append, ("close", name))
            return 3

        def file_identity(self, handle):
            return (7, b"generated-file-id")

        def file_acl(self, handle):
            events.append(("file_acl", handle))

    native = Native()
    monkeypatch.setattr(module, "_StateNative", lambda: native)
    monkeypatch.setattr(module, "_root_parts", lambda _: ("X:\\", "protected"))
    try:
        yield store, events, native
    finally:
        store.db.close()


def test_readonly_state_gate_pins_parent_database_and_checks_sidecars(fixture):
    store, events, _ = fixture
    before = store.db.total_changes
    with module.verified_private_database(store):
        assert ("file", "fixture.sqlite3") in events
        assert ("close", "fixture.sqlite3") not in events
        assert ("parent_acl", 2) in events
        assert not store.db.in_transaction
    assert ("close", "fixture.sqlite3") in events
    assert store.db.total_changes == before


def test_unprotected_parent_fails_without_touching_database(fixture):
    store, events, native = fixture
    native.verify_acl = lambda _: (_ for _ in ()).throw(ValueError("synthetic acl denied"))
    with pytest.raises(ValueError):
        with module.verified_private_database(store):
            pytest.fail("gate opened")
    assert not any(kind == "file" for kind, _ in events)


def test_non_file_database_never_reaches_native_gate():
    db = sqlite3.connect(":memory:")
    try:
        with pytest.raises(ValueError, match="database_invalid"):
            with module.verified_private_database(SimpleNamespace(db=db)):
                pytest.fail("gate opened")
    finally:
        db.close()


@pytest.mark.parametrize(
    "owner,aces",
    [
        ("other", [(0, 0, 0x1F01FF, "owner"), (0, 0, 0x1F01FF, "S-1-5-18")]),
        ("owner", [(0, 0, 0x1F01FF, "everyone")]),
        ("owner", []),
        ("owner", [(0, 8, 0x1F01FF, "owner"), (0, 0, 0x1F01FF, "S-1-5-18")]),
    ],
)
def test_wrong_owner_or_broad_acl_is_rejected(owner, aces):
    acl = SimpleNamespace(
        GetAceCount=lambda: len(aces),
        GetAce=lambda i: ((aces[i][0], aces[i][1]), aces[i][2], aces[i][3]),
    )
    descriptor = SimpleNamespace(
        GetSecurityDescriptorOwner=lambda: owner, GetSecurityDescriptorDacl=lambda: acl
    )
    security = SimpleNamespace(
        SE_FILE_OBJECT=1,
        DACL_SECURITY_INFORMATION=4,
        OWNER_SECURITY_INFORMATION=1,
        GetSecurityInfo=lambda *_: descriptor,
        ConvertSidToStringSid=lambda sid: sid,
    )
    native = object.__new__(module._StateNative)
    native.sid, native.security = "owner", security
    with pytest.raises(ValueError):
        native.file_acl(1)


def test_native_file_identity_queries_volume_and_128bit_id():
    import ctypes

    native = object.__new__(module._StateNative)

    def information(handle, kind, output, size):
        assert handle == 17 and kind == 18 and size == 24
        raw = (123).to_bytes(8, "little") + bytes(range(16))
        ctypes.memmove(output, raw, len(raw))
        return True

    native.k = SimpleNamespace(GetFileInformationByHandleEx=information)
    assert native.file_identity(17) == (123, bytes(range(16)))
    native.k.GetFileInformationByHandleEx = lambda *_: False
    with pytest.raises(ValueError, match="identity_unavailable"):
        native.file_identity(17)
