"""Unreviewed history survives restart for every plan shape, intact or not."""

import pytest

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.store import Store


@pytest.mark.parametrize(
    "kind", ["device", "unknown", "profile", "missing", "corrupt", "digest", "job"]
)
def test_review_scope_is_conservative_and_survives_restart(tmp_path, kind):
    path = tmp_path / "history.sqlite3"
    store = Store(path)
    plan = {"job_id": "historical", "test": "derby_smoke"}
    if kind == "unknown":
        plan = {"job_id": "historical"}
    elif kind == "profile":
        plan.update(test="firmware_build", execution_context="registered_project")
    elif kind == "job":
        plan["job_id"] = "other-job"
    digest = digest_plan(plan)
    if kind != "missing":
        store.put_plan(digest, plan)
    if kind == "corrupt":
        store.db.execute("UPDATE plans SET payload='not-json' WHERE hash=?", (digest,))
    elif kind == "digest":
        store.db.execute("UPDATE plans SET payload='{}' WHERE hash=?", (digest,))
    store.add_job("historical", digest)
    store.set_job("historical", "needs_review", "interrupted")
    before = store.job("historical")
    store.close()
    reopened = Store(path)
    try:
        reopened.recover()
        assert reopened.needs_review()
        assert reopened.job("historical") == before
    finally:
        reopened.close()
