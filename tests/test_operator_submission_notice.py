"""Local CLI consent distinguishes an existing request using synthetic plans only."""

import pytest
from test_device_observation_cli import write_plan

from cardputer_mcp.operator import approve_plan
from cardputer_mcp.store import Store


@pytest.mark.parametrize("state", [None, "cancelled", "succeeded"])
def test_cli_discloses_whether_exact_job_was_submitted(tmp_path, state):
    digest, port = write_plan(tmp_path)
    store = Store(tmp_path / "history.sqlite3")
    try:
        if state is not None:
            store.add_job("synthetic-log-observation", digest)
            store.set_job("synthetic-log-observation", state)
    finally:
        store.close()
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return "APPROVE " + digest

    assert approve_plan(tmp_path, digest, confirm)["approved"] is True
    assert len(prompts) == 1
    assert "queue it immediately" not in prompts[0]
    assert "does not submit execution" in prompts[0]
    assert port.opens == 0 and port.writes == []


def test_cli_rejects_submission_meaning_changed_during_consent(tmp_path):
    digest, port = write_plan(tmp_path)
    store = Store(tmp_path / "history.sqlite3")
    try:

        def confirm(_prompt):
            store.add_awaiting_job("synthetic-log-observation", digest)
            return "APPROVE " + digest

        with pytest.raises(ValueError, match="^stale_plan_review$"):
            approve_plan(tmp_path, digest, confirm)
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        assert port.opens == 0 and port.writes == []
    finally:
        store.close()


def test_submitted_job_requires_live_gui_review_before_any_cli_prompt(tmp_path):
    digest, port = write_plan(tmp_path)
    store = Store(tmp_path / "history.sqlite3")
    try:
        store.add_awaiting_job("synthetic-log-observation", digest)
        prompts = []
        with pytest.raises(ValueError, match="^submitted_plan_requires_gui_approval$"):
            approve_plan(tmp_path, digest, lambda prompt: prompts.append(prompt) or "")
        assert not prompts
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        assert port.opens == 0 and port.writes == []
    finally:
        store.close()


def test_single_short_approve_discloses_target_without_copying_a_token(tmp_path):
    digest, port = write_plan(tmp_path)
    prompts = []
    result = approve_plan(tmp_path, digest, lambda prompt: prompts.append(prompt) or "APPROVE")
    assert result == {"approved": True, "plan_hash": digest}
    assert len(prompts) == 1
    for expected in ("Target:", "Recovery:", "consent", "does not prove", digest):
        assert expected in prompts[0]
    assert "Type APPROVE to approve this displayed plan" in prompts[0]
    assert port.opens == 0 and port.writes == []
