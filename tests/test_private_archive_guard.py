"""Generated private receipts must not lose bindings to ordinary offline archive."""

import pytest
from test_private_build_finalize import fixture as fixture

from cardputer_mcp.private_build_finalize import commit_private_candidate
from cardputer_mcp.store import Store


@pytest.mark.parametrize("kind", ["jobs", "plans", "both"])
@pytest.mark.parametrize("pinned", [False, True])
def test_private_bindings_never_archive(fixture, tmp_path, kind, pinned):
    builder, store, artifacts, args, _ = fixture
    receipt = commit_private_candidate(builder, store, artifacts, **args)
    if not pinned:
        # Fixture setup only. Production deliberately has no implicit unpin API.
        store.db.execute("DELETE FROM private_artifact_pins")
        store.db.commit()
    plan_hash = store.job("fixture")["plan_hash"]
    selection = {}
    if kind in {"jobs", "both"}:
        selection["jobs"] = ["fixture"]
    if kind in {"plans", "both"}:
        selection["plans"] = [plan_hash]
    destination = tmp_path / "ordinary-archive.db"
    with pytest.raises(ValueError, match="private_artifact_retained"):
        store.archive_rows(destination, selection, {})
    assert not destination.exists()
    assert store.plan(plan_hash) is not None
    assert artifacts.resolve(receipt=receipt, principal="owner") == args["files"]
    assert store.db.execute("SELECT COUNT(*) FROM private_artifact_pins").fetchone()[0] == pinned


def test_unrelated_public_job_still_archives(fixture, tmp_path):
    builder, store, artifacts, args, _ = fixture
    receipt = commit_private_candidate(builder, store, artifacts, **args)
    store.put_plan("public-hash", {"job_id": "public", "test": "diagnostics"})
    store.add_job("public", "public-hash")
    store.ledger.transition("public", "running")
    store.ledger.transition("public", "succeeded")
    destination = tmp_path / "public.db"
    store.archive_rows(destination, {"jobs": ["public"], "plans": ["public-hash"]}, {})
    assert store.job("public") is None
    assert artifacts.resolve(receipt=receipt, principal="owner") == args["files"]


def test_no_private_schema_preserves_ordinary_archive(tmp_path):
    store = Store(tmp_path / "ordinary.db")
    try:
        store.put_plan("public-hash", {"job_id": "public", "test": "diagnostics"})
        store.add_job("public", "public-hash")
        store.archive_rows(tmp_path / "public.db", {"jobs": ["public"]}, {})
        assert store.job("public") is None
    finally:
        store.db.close()


@pytest.mark.parametrize("corruption", ["record", "orphan_pin"])
def test_unknown_private_binding_blocks_destructive_archive(fixture, tmp_path, corruption):
    builder, store, artifacts, args, _ = fixture
    commit_private_candidate(builder, store, artifacts, **args)
    if corruption == "record":
        store.db.execute("UPDATE private_artifact_receipts SET record='{}'")
    else:
        store.db.execute("INSERT INTO private_artifact_pins VALUES (?,?)", ("f" * 64, "fixture"))
    store.db.commit()
    destination = tmp_path / "refused.db"
    with pytest.raises(ValueError, match="private_artifact_retained"):
        store.archive_rows(destination, {"jobs": ["unrelated"]}, {})
    assert not destination.exists()
    assert store.job("fixture") is not None
