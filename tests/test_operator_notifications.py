"""Postcommit and thread-safe coalesced wakeups contain no job/output payloads."""

import pytest

from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.logs import Event, LogBook
from cardputer_mcp.store import Store


async def test_commits_wake_only_after_success_and_rollback_does_not_notify(tmp_path):
    events = []
    store = Store(tmp_path / "history.sqlite3")
    store.db.on_commit = lambda: events.append(store.job("job"))
    store.add_job("job", "f" * 64)
    assert events[-1]["state"] == "queued"
    with store.db:
        store.db.execute("UPDATE jobs SET state='running' WHERE id='job'")
        assert events[-1]["state"] == "queued"
    assert events[-1]["state"] == "running"
    count = len(events)
    with pytest.raises(ValueError), store.db:
        store.db.execute("UPDATE jobs SET state='failed' WHERE id='job'")
        raise ValueError("abort")
    assert len(events) == count and store.job("job")["state"] == "running"
    store.db.close()


def test_observer_failure_never_changes_commit_or_log_append(tmp_path):
    def broken():
        raise RuntimeError("observer")

    store = Store(tmp_path / "history.sqlite3", on_commit=broken)
    store.add_job("job", "f" * 64)
    assert store.job("job")["state"] == "queued"
    log = BuildLogs("owner", on_change=broken)
    log.append("warning: retained")
    assert log.sequence == 1
    events = LogBook(8, on_change=broken)
    events.append(Event.STARTED)
    assert events.sequence == 1
    store.db.close()


def test_redaction_expansion_keeps_retained_line_bound():
    logs = BuildLogs("owner")
    logs.redactions = ("x",)
    logs.append("x" * 4096)
    assert len(logs.entries[0]["text"]) <= 4096
    assert "x" not in logs.entries[0]["text"]
