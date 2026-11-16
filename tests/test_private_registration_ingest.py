"""Generated registration adapter tests; no native roots or actual private bytes."""

import hashlib
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from cardputer_mcp import private_registration_ingest as module
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest
from cardputer_mcp.store import Store


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    data = b"generated only"
    manifest = (InputFile("fixture", len(data), hashlib.sha256(data).hexdigest()),)
    registration = PrivateInputRegistration(
        local_root=tmp_path / "not-opened",
        destination="assets",
        source_commit="a" * 40,
        recipe_sha256="b" * 64,
        inventory_sha256=inventory_digest(manifest),
    )
    project = SimpleNamespace(private_inputs={"fixture": registration})
    builder = SimpleNamespace(
        settings=SimpleNamespace(build_projects={"fixture": project}),
        prepare=lambda *_a, **_k: {"fingerprint": "b" * 64},
    )
    store = Store(tmp_path / "generated.db")
    events = []
    metadata = {"commit": "a" * 40}
    monkeypatch.setattr(
        module,
        "SourceSnapshots",
        lambda _: SimpleNamespace(
            cache_guard=lambda _: nullcontext(), _resolve=lambda *_: (tmp_path, metadata)
        ),
    )

    def read(*args, **kwargs):
        events.append("read")
        yield "fixture", data

    monkeypatch.setattr(module, "read_registered_windows", read)

    @contextmanager
    def guard(_):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    args = dict(
        project_id="fixture",
        input_id="fixture",
        source_receipt="c" * 64,
        principal="owner",
        manifest=manifest,
        authorized=lambda: True,
        cancelled=lambda: False,
        state_guard=guard,
    )
    try:
        yield builder, store, registration, metadata, events, args, data
    finally:
        store.db.close()


def test_registered_source_recipe_import_is_owned_and_guarded(fixture):
    builder, store, reg, _, events, args, data = fixture
    receipt = module.ingest_registered_input(builder, store, **args)
    assert events == ["enter", "read", "exit"]
    assert PrivateInputStore(store.db).resolve(
        receipt=receipt,
        registration=reg,
        project_id="fixture",
        input_id="fixture",
        principal="owner",
    ) == (("fixture", data),)
    with pytest.raises(ValueError):
        PrivateInputStore(store.db).resolve(
            receipt=receipt,
            registration=reg,
            project_id="fixture",
            input_id="fixture",
            principal="other",
        )


@pytest.mark.parametrize("failure", ["authority", "cancel", "source", "recipe", "guard"])
def test_import_preconditions_fail_before_private_read(fixture, failure):
    builder, store, _, metadata, events, args, _ = fixture
    if failure == "authority":
        args["authorized"] = lambda: False
    elif failure == "cancel":
        args["cancelled"] = lambda: True
    elif failure == "source":
        metadata["commit"] = "d" * 40
    elif failure == "recipe":
        builder.prepare = lambda *_a, **_k: {"fingerprint": "d" * 64}
    else:

        @contextmanager
        def deny(_):
            raise ValueError("synthetic protected DB denied")
            yield

        args["state_guard"] = deny
    with pytest.raises(ValueError):
        module.ingest_registered_input(builder, store, **args)
    assert "read" not in events
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='private_input_bytes'"
        ).fetchone()[0]
        == 0
    )


def test_registration_change_during_reader_never_returns_receipt(fixture, monkeypatch):
    builder, store, reg, _, _, args, data = fixture

    def read(*_a, **_k):
        builder.settings.build_projects["fixture"].private_inputs["fixture"] = reg.model_copy(
            update={"recipe_sha256": "d" * 64}
        )
        yield "fixture", data

    monkeypatch.setattr(module, "read_registered_windows", read)
    with pytest.raises(ValueError):
        module.ingest_registered_input(builder, store, **args)
    assert store.db.execute("SELECT COUNT(*) FROM private_input_receipts").fetchone()[0] == 0
