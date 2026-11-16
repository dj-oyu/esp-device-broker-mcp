"""Native guard and real reprepare; generated data, synthetic public source receipt."""

import hashlib
import sys
from contextlib import closing, nullcontext
from types import SimpleNamespace

import pytest
from test_private_state_windows_native import new_protected_directory

from cardputer_mcp.config import BuildProject
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_activation_preflight import preflight_private_activation
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputReader, PrivateInputStore, inventory_digest
from cardputer_mcp.store import Store

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows required")


@pytest.mark.parametrize("tamper", [False, True])
def test_real_reprepare_with_native_state_guard(tmp_path, monkeypatch, tamper):
    protected = new_protected_directory(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "CMakeLists.txt").write_text("# synthetic source")
    profile = source / "profile.ps1"
    profile.write_text("# synthetic, never executed")
    payload = b"generated"
    reg = PrivateInputRegistration(
        local_root=tmp_path / "not-read",
        destination="assets",
        source_commit="a" * 40,
        recipe_sha256="b" * 64,
        inventory_sha256=inventory_digest(
            (InputFile("fixture", len(payload), hashlib.sha256(payload).hexdigest()),)
        ),
    )
    project = BuildProject(
        project_dir=source, trusted_profile=profile, private_inputs={"fixture": reg}
    )
    builder = FirmwareBuild(SimpleNamespace(build_projects={"p": project}))
    # Only the public source receipt verification boundary is synthesized.
    monkeypatch.setattr(builder, "_project", lambda *_: (project, {"commit": "a" * 40}))
    store = Store(protected / "synthetic.db")
    with closing(store.db):
        inputs = PrivateInputStore(store.db)
        builder.private_store_factory = lambda: nullcontext(inputs)
        recipe = builder.prepare("p", "owner", source_receipt="c" * 64)["fingerprint"]
        reg = reg.model_copy(update={"recipe_sha256": recipe})
        project.private_inputs["fixture"] = reg
        receipt = inputs.ingest(
            registration=reg, project_id="p", input_id="fixture",
            principal="owner", files=(("fixture", payload),),
        )
        plan = builder.prepare(
            "p", "owner", source_receipt="c" * 64,
            private_input_id="fixture", private_input_receipt=receipt,
        ) | {"build_principal": "owner", "test": "firmware_build"}
        builder.private_store_factory = lambda: pytest.fail("preflight invoked mutating factory")
        if tamper:
            store.db.execute("UPDATE private_input_bytes SET payload=?", (b"tampered",))
            store.db.commit()
        calls = []
        original = PrivateInputReader.resolve

        def resolve(reader, **kwargs):
            assert reader.connection is store.db
            calls.append("read")
            # The native parent pin must still be held inside real reprepare.
            with pytest.raises(PermissionError):
                protected.rename(tmp_path / "moved")
            return original(reader, **kwargs)

        monkeypatch.setattr(PrivateInputReader, "resolve", resolve)
        statements = []
        store.db.set_trace_callback(statements.append)
        changes = store.db.total_changes
        try:
            if tamper:
                with pytest.raises(ValueError):
                    preflight_private_activation(
                        builder, store, plan, principal="owner", authorized=lambda: True
                    )
            else:
                result = preflight_private_activation(
                    builder, store, plan, principal="owner", authorized=lambda: True
                )
                assert result == {
                    "status": "preflight_checked",
                    "runtime": "not_installed",
                    "activation": "not_granted",
                }
                assert len(calls) == 2
        finally:
            store.db.set_trace_callback(None)
        assert calls
        assert changes == store.db.total_changes
        assert statements and all(
            sql.lstrip().upper().startswith(("SELECT ", "PRAGMA DATABASE_LIST"))
            for sql in statements
        )
        assert builder.private_runtime is None
