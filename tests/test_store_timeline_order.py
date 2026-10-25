"""Pin timeline_order initialization on fresh and pre-existing history databases."""

import hashlib
import json
import sqlite3

import pytest

from cardputer_mcp.store import Store

TRIGGERS = ("timeline_job_registered", "timeline_plan_registered")


def legacy(path, *, plans=(), jobs=(), extra=""):
    """A pre-timeline history: plans/jobs rows, then optional extra schema."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE plans(hash TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE jobs(id TEXT PRIMARY KEY,plan_hash TEXT NOT NULL,state TEXT NOT NULL,
            stage TEXT,result TEXT,diagnostic TEXT,reviewed INTEGER NOT NULL DEFAULT 0,
            created TEXT DEFAULT CURRENT_TIMESTAMP,finished TEXT);
    """)
    for index, payload in enumerate(plans):
        db.execute("INSERT INTO plans VALUES(?,?)", (f"plan-{index}", payload))
    for job_id, created in jobs:
        db.execute(
            "INSERT INTO jobs(id,plan_hash,state,created) VALUES(?,?,'succeeded',?)",
            (job_id, "old-plan", created),
        )
    db.commit()
    # Extra schema (e.g. a partial timeline) arrives after the history rows.
    db.executescript(extra)
    db.close()


def timeline(store):
    return [
        (row[0], row[1])
        for row in store.db.execute("SELECT job_id,created FROM timeline_order ORDER BY sequence")
    ]


def schema(store):
    return {
        row[0]: hashlib.sha256(row[1].encode()).hexdigest()
        for row in store.db.execute(
            "SELECT name,sql FROM sqlite_master WHERE name='timeline_order' "
            "OR (type='trigger' AND tbl_name IN ('plans','jobs')) ORDER BY name"
        )
    }


def plan(store, job):
    payload = {"job_id": job, "test": "capture_home"}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    store.put_plan(digest, payload)
    return digest


def test_fresh_database_records_plan_then_job_registration_once(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        assert set(schema(store)) == {"timeline_order", *TRIGGERS}
        assert timeline(store) == []
        digest = plan(store, "first")
        store.add_job("first", digest)
        store.add_job("job-only", digest)
        rows = timeline(store)
        assert [job for job, _ in rows] == ["first", "job-only"]
        assert all(created is not None for _, created in rows)
        # Invalid or out-of-range plan job ids never register.
        store.db.execute("INSERT INTO plans VALUES('bad-1','not-json')")
        store.db.execute("INSERT INTO plans VALUES('bad-2',?)", (json.dumps({"job_id": 7}),))
        store.db.execute("INSERT INTO plans VALUES('bad-3',?)", (json.dumps({"job_id": "x" * 65}),))
        store.db.commit()
        assert [job for job, _ in timeline(store)] == ["first", "job-only"]
    finally:
        store.close()


def test_schema_text_is_stable(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        # Stored DDL text is part of every existing history; moving code must not change it.
        assert schema(store) == {
            "timeline_job_registered": (
                "1ef38ef5a6d46ad8f01668d336ded786d37b26d50a9717c6775e24e6a653cb57"
            ),
            "timeline_order": "86a471056153f092315be06516f553cc9e2c513b8e0af265a3467fbf60650138",
            "timeline_plan_registered": (
                "047787f5af13ecb10a4e1508397092927c598fec9d967f7ffb5a43a816a6b00b"
            ),
        }
    finally:
        store.close()


def test_pre_timeline_history_gets_undated_plan_prefix_then_dated_jobs(tmp_path):
    path = tmp_path / "history.sqlite3"
    legacy(
        path,
        plans=(
            json.dumps({"job_id": "undated-b"}),
            json.dumps({"job_id": "undated-a"}),
            json.dumps({"job_id": "executed"}),
            "not-json",
            json.dumps({"job_id": 7}),
            json.dumps({"job_id": ""}),
            json.dumps({"job_id": "x" * 65}),
            json.dumps({"job_id": "huge", "pad": "x" * 17000}),
            json.dumps({"job_id": "undated-b"}),
        ),
        jobs=(
            ("executed", "2024-01-02 00:00:00"),
            ("early", "2024-01-01 00:00:00"),
            ("tie-b", "2024-01-03 00:00:00"),
            ("tie-a", "2024-01-03 00:00:00"),
        ),
    )
    store = Store(path)
    try:
        assert set(schema(store)) == {"timeline_order", *TRIGGERS}
        assert timeline(store) == [
            ("undated-b", None),
            ("undated-a", None),
            ("early", "2024-01-01 00:00:00"),
            ("executed", "2024-01-02 00:00:00"),
            ("tie-b", "2024-01-03 00:00:00"),
            ("tie-a", "2024-01-03 00:00:00"),
        ]
        plans = store.db.execute("SELECT count(*) FROM plans").fetchone()[0]
        assert plans == 9
    finally:
        store.close()
    reopened = Store(path)
    try:
        assert [job for job, _ in timeline(reopened)][-1] == "tie-a"
        assert len(timeline(reopened)) == 6
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "triggers",
    [
        "",
        """CREATE TRIGGER timeline_job_registered AFTER INSERT ON jobs BEGIN
            INSERT OR IGNORE INTO timeline_order(job_id,created) VALUES(NEW.id,'legacy');
        END;""",
    ],
    ids=["table-without-triggers", "table-with-one-trigger"],
)
def test_incomplete_timeline_backfills_after_existing_rows(tmp_path, triggers):
    path = tmp_path / "history.sqlite3"
    legacy(
        path,
        plans=(json.dumps({"job_id": "undated"}), json.dumps({"job_id": "kept"})),
        jobs=(("kept", "2024-01-05 00:00:00"), ("early", "2024-01-01 00:00:00")),
        extra="""
        CREATE TABLE timeline_order (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL UNIQUE, created TEXT
        );
        INSERT INTO timeline_order(job_id,created) VALUES('kept','preexisting');
        """
        + triggers,
    )
    store = Store(path)
    try:
        assert set(schema(store)) == {"timeline_order", *TRIGGERS}
        assert timeline(store) == [
            ("kept", "preexisting"),
            ("undated", None),
            ("early", "2024-01-01 00:00:00"),
        ]
        # An existing trigger is kept as-is (CREATE TRIGGER IF NOT EXISTS).
        store.add_job("after", "old-plan")
        expected = "legacy" if triggers else None
        created = timeline(store)[-1]
        assert created[0] == "after"
        if expected is not None:
            assert created[1] == expected
        else:
            assert created[1] is not None
    finally:
        store.close()


def test_complete_triggers_skip_backfill_on_reopen(tmp_path):
    path = tmp_path / "history.sqlite3"
    store = Store(path)
    try:
        digest = plan(store, "first")
        store.add_job("first", digest)
        plan(store, "second")
    finally:
        store.close()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM timeline_order WHERE job_id IN ('first','second')")
    db.commit()
    db.close()
    reopened = Store(path)
    try:
        assert timeline(reopened) == []
        assert set(schema(reopened)) == {"timeline_order", *TRIGGERS}
    finally:
        reopened.close()
