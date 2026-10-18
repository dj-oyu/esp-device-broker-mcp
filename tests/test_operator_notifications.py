"""Postcommit and thread-safe coalesced wakeups contain no job/output payloads."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest

from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.logs import Event, LogBook
from cardputer_mcp.operator_notifications import OperatorNotifications
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


async def test_thread_log_callbacks_coalesce_without_retaining_output():
    changes = OperatorNotifications()
    callbacks = []
    unsubscribe = changes.subscribe(lambda: callbacks.append(True))
    logs = BuildLogs("owner", on_change=changes.notify_logs)
    with ThreadPoolExecutor(max_workers=1) as pool:
        # The broker loop deliberately stays busy while the producer completes;
        # its pending callback stays bounded even under a burst of raw output.
        pool.submit(lambda: [logs.append("warning: secret=fixture") for _ in range(100)]).result()
    assert changes.revision() == (100, 100)
    await asyncio.sleep(0)
    assert callbacks == [True]
    assert "fixture" not in repr(vars(changes))
    events = LogBook(8, on_change=changes.notify)
    events.append(Event.STAGE, stage="idf_build")
    await asyncio.sleep(0)
    assert changes.revision() == (101, 100) and len(callbacks) == 2
    unsubscribe()
    changes.notify()
    await asyncio.sleep(0)
    assert len(callbacks) == 2


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
