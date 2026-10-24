"""JobLedger: the single runtime writer of job state, with legality enforced in SQL."""

import ast
import inspect
import json
import sqlite3
from pathlib import Path

import pytest

import cardputer_mcp
from cardputer_mcp.job_ledger import STATES, TERMINAL, TRANSITIONS, JobLedger, allowed
from cardputer_mcp.store import Store

PAIRS = [(source, target) for source in STATES for target in STATES]
EXPECTED_TABLE = {
    ("queued", "running"),
    ("queued", "cancelled"),
    ("queued", "needs_review"),
    ("running", "succeeded"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("running", "needs_review"),
}


@pytest.fixture
def store(tmp_path):
    history = Store(tmp_path / "history.sqlite3")
    yield history
    history.close()


def seed(store: Store, state: str, job_id: str = "job") -> dict:
    store.add_job(job_id, "digest")
    store.db.execute(
        "UPDATE jobs SET state=?, result='seeded', diagnostic=?, finished=? WHERE id=?",
        (
            state,
            json.dumps({"seed": True}),
            "2000-01-01 00:00:00" if state in TERMINAL else None,
            job_id,
        ),
    )
    store.db.commit()
    return dict(store.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def row(store: Store, job_id: str = "job") -> dict:
    return dict(store.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def test_the_table_is_exactly_what_the_code_writes_today():
    assert {(s, t) for s, t in PAIRS if allowed(s, t)} == EXPECTED_TABLE
    assert "awaiting_approval" not in {s for sources in TRANSITIONS.values() for s in sources}
    # Nothing ever leaves a terminal state through the ledger.
    assert not any(allowed(source, target) for source in TERMINAL for target in STATES)


@pytest.mark.parametrize(("source", "target"), PAIRS, ids=[f"{s}->{t}" for s, t in PAIRS])
def test_every_pair_is_written_or_refused_in_sql(store, source, target):
    before = seed(store, source)
    refused_events: list[None] = []
    store.ledger.on_refused = lambda: refused_events.append(None)
    if target == "queued":
        with pytest.raises(ValueError, match="^job_transition_target_unknown$"):
            store.ledger.transition("job", target, "new_result")
        assert row(store) == before
        return
    written = store.ledger.transition("job", target, "new_result", {"code": "new_result"})
    after = row(store)
    if (source, target) in EXPECTED_TABLE:
        assert written is True
        assert after["state"] == target
        assert after["result"] == "new_result"
        assert json.loads(after["diagnostic"]) == {"code": "new_result"}
        assert (after["finished"] is not None) is (target in TERMINAL)
        assert store.ledger.refusals == {}
        assert refused_events == []
    else:
        assert written is False
        assert after == before
        assert store.ledger.refusals == {(source, target): 1}
        assert refused_events == [None]


def test_entering_running_clears_result_and_diagnostic(store):
    seed(store, "queued")
    assert store.ledger.transition("job", "running") is True
    after = row(store)
    assert (after["state"], after["result"], after["diagnostic"], after["finished"]) == (
        "running",
        None,
        None,
        None,
    )


@pytest.mark.parametrize("target", sorted(TRANSITIONS))
def test_a_state_change_between_read_and_write_is_refused(store, target):
    """The caller read an allowed from-state, but the row moved before the write."""
    source = sorted(TRANSITIONS[target])[0]
    seed(store, source)
    observed = store.job("job")["state"]
    assert allowed(observed, target)
    store.db.execute("UPDATE jobs SET state='cancelled', result='first_reason' WHERE id='job'")
    store.db.commit()
    committed = row(store)
    assert store.ledger.transition("job", target, "late_reason") is False
    assert row(store) == committed
    assert store.ledger.refusals == {("cancelled", target): 1}


def test_a_missing_row_is_refused_and_counted(store):
    assert store.ledger.transition("ghost", "running") is False
    assert store.ledger.refusals == {("missing", "running"): 1}


def test_expect_narrows_but_never_widens_the_table(store):
    seed(store, "running")
    assert store.ledger.transition("job", "cancelled", "r", expect={"queued"}) is False
    assert row(store)["state"] == "running"
    for invalid in ({"succeeded"}, set(), {"queued", "awaiting_approval"}):
        with pytest.raises(ValueError, match="^job_transition_expectation_invalid$"):
            store.ledger.transition("job", "cancelled", "r", expect=invalid)
    assert row(store)["state"] == "running"
    assert store.ledger.transition("job", "cancelled", "r", expect={"running"}) is True


def test_a_failing_refusal_observer_never_affects_the_caller(store):
    seed(store, "succeeded")

    def broken():
        raise RuntimeError("observer failed")

    store.ledger.on_refused = broken
    assert store.ledger.transition("job", "needs_review", "late") is False
    assert row(store)["state"] == "succeeded"


def test_commit_then_notify_order_is_kept(tmp_path):
    seen: list[str] = []
    history = Store(tmp_path / "history.sqlite3")
    try:
        history.add_job("job", "digest")

        def on_written(job_id: str, state: str) -> None:
            # A fresh connection sees the committed state before the notification.
            other = sqlite3.connect(tmp_path / "history.sqlite3")
            try:
                seen.append(
                    other.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
                )
            finally:
                other.close()

        history.ledger.on_written = on_written
        assert history.ledger.transition("job", "running") is True
    finally:
        history.close()
    assert seen == ["running"]


@pytest.mark.parametrize("state", STATES)
def test_annotate_never_changes_state_result_or_finished(store, state):
    before = seed(store, state)
    written = store.ledger.annotate("job", {"job_log_unavailable": True})
    after = row(store)
    assert written is (state in TERMINAL)
    for column in ("state", "result", "finished", "reviewed", "plan_hash"):
        assert after[column] == before[column]
    expected = {"seed": True, "job_log_unavailable": True} if state in TERMINAL else {"seed": True}
    assert json.loads(after["diagnostic"]) == expected
    assert store.ledger.refusals == {}


def test_annotate_a_missing_row_is_a_no_op(store):
    assert store.ledger.annotate("ghost", {"job_log_unavailable": True}) is False


@pytest.mark.parametrize("state", STATES)
def test_mark_reviewed_only_acknowledges_needs_review(store, state):
    before = seed(store, state)
    assert store.ledger.mark_reviewed("job") is (state == "needs_review")
    after = row(store)
    assert after["reviewed"] == (1 if state == "needs_review" else before["reviewed"])
    assert {k: v for k, v in after.items() if k != "reviewed"} == {
        k: v for k, v in before.items() if k != "reviewed"
    }


def test_ledger_methods_are_synchronous():
    for name in ("transition", "annotate", "mark_reviewed"):
        assert not inspect.iscoroutinefunction(getattr(JobLedger, name))


def test_runtime_code_writes_job_state_only_through_the_ledger():
    """`Store.set_job` stays as a test fixture; runtime modules never call it."""
    package = Path(cardputer_mcp.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(text)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "set_job"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
        if path.name not in ("store.py", "job_ledger.py") and "UPDATE jobs" in text:
            offenders.append(f"{path.name}:UPDATE jobs")
    assert offenders == []
