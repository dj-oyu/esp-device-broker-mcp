"""Local approval disclosure only; no device or real credential is used."""

import pytest

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.operator import approve_plan
from cardputer_mcp.store import Store


@pytest.mark.parametrize("answer", ["", "yes", "DEVICE FREE AND FIRMWARE VERIFIED"])
def test_capture_requires_specific_home_attestation(tmp_path, answer):
    plan = {"backend": "serial", "test": "capture_home", "job_id": "fixture"}
    digest = digest_plan(plan)
    store = Store(tmp_path / "history.sqlite3")
    store.put_plan(digest, plan)
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return answer

    try:
        with pytest.raises(ValueError, match="device_attestation_required"):
            approve_plan(tmp_path, digest, confirm)
        assert len(prompts) == 1
        assert "DEVICE HOME AND FIRMWARE VERIFIED" in prompts[0]
        assert "Esc" in prompts[0] and "unsaved state" in prompts[0]
        assert "AI service" in prompts[0] and "five minutes" in prompts[0]
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        store.close()


def test_capture_home_attestation_does_not_replace_exact_hash_approval(tmp_path):
    plan = {"backend": "serial", "test": "capture_home", "job_id": "fixture"}
    digest = digest_plan(plan)
    store = Store(tmp_path / "history.sqlite3")
    store.put_plan(digest, plan)
    try:
        answers = iter(["DEVICE HOME AND FIRMWARE VERIFIED", "APPROVE wrong"])
        with pytest.raises(ValueError, match="approval_not_granted"):
            approve_plan(tmp_path, digest, lambda _: next(answers))
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        answers = iter(["DEVICE HOME AND FIRMWARE VERIFIED", f"APPROVE {digest}"])
        approved = approve_plan(tmp_path, digest, lambda _: next(answers))
        assert approved == {"approved": True, "plan_hash": digest}
    finally:
        store.close()
