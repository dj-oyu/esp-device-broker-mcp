"""Generated input, native reader/state guard, synthetic source receipt only."""

import hashlib
import sys
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp import private_registration_ingest as module
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest
from cardputer_mcp.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


def test_native_registration_ingestion_owned_receipt(tmp_path, monkeypatch):
    state = new_protected_directory(tmp_path)
    source = tmp_path / "generated-input"
    source.mkdir()
    data = b"synthetic registration payload"
    (source / "fixture.bin").write_bytes(data)
    manifest = (InputFile("fixture.bin", len(data), hashlib.sha256(data).hexdigest()),)
    reg = PrivateInputRegistration(
        local_root=source,
        destination="assets",
        source_commit="a" * 40,
        recipe_sha256="b" * 64,
        inventory_sha256=inventory_digest(manifest),
    )
    builder = SimpleNamespace(
        settings=SimpleNamespace(
            build_projects={"fixture": SimpleNamespace(private_inputs={"fixture": reg})}
        ),
        prepare=lambda *_a, **_k: {"fingerprint": "b" * 64},
    )
    monkeypatch.setattr(
        module,
        "SourceSnapshots",
        lambda _: SimpleNamespace(
            cache_guard=lambda _: nullcontext(),
            _resolve=lambda *_: (source, {"commit": "a" * 40}),
        ),
    )
    store = Store(state / "generated.db")
    with closing(store.db):
        receipt = module.ingest_registered_input(
            builder,
            store,
            project_id="fixture",
            input_id="fixture",
            source_receipt="c" * 64,
            principal="owner",
            manifest=manifest,
            authorized=lambda: True,
            cancelled=lambda: False,
        )
        inputs = PrivateInputStore(store.db)
        assert inputs.resolve(
            receipt=receipt,
            registration=reg,
            project_id="fixture",
            input_id="fixture",
            principal="owner",
        ) == (("fixture.bin", data),)
        with pytest.raises(ValueError):
            inputs.resolve(
                receipt=receipt,
                registration=reg,
                project_id="fixture",
                input_id="fixture",
                principal="other",
            )
        assert store.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
