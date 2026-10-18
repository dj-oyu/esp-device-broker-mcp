"""One operator card follows a job; history never becomes an authorization."""

import ast
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.gui_timeline import TimelineSnapshot
from cardputer_mcp.models import (
    AcquireRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_timeline import MAX_CARDS, RESULTS, STAGES, STATES, OperatorTimeline
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend

CARD_KEYS = {
    "job_id",
    "plan_hash",
    "test",
    "backend",
    "state",
    "stage",
    "result",
    "created_utc",
    "finished_utc",
    "reviewed",
    "review",
    "image",
}


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def running(tmp_path, *, automatic=False, backend=None):
    clock = Clock()
    backend = backend or MockBackend(0.001)
    settings = {"backend": "serial", "serial": backend.config} if backend.name == "serial" else {}
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=automatic, **settings),
        backend=backend,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        serial_enabled=backend.name == "serial",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    plans = PlanApprovals(broker)
    try:
        yield broker, plans, OperatorTimeline(broker, plans), clock
    finally:
        if isinstance(backend, MockBackend) and backend.gate is not None:
            backend.gate.set()
        await broker.close()


def prepare(broker, job="one", test=Action.DIAGNOSTICS):
    if broker.session is None:
        broker.acquire(
            AcquireRequest(owner=Origin.LOCAL, lease_seconds=300), principal_id="synthetic-owner"
        )
    return broker.prepare(PrepareRequest(session=broker.session.capability, job_id=job, test=test))


def card(timeline, job="one"):
    snapshot = timeline.snapshot()
    for item in snapshot["cards"]:
        assert set(item) == CARD_KEYS
        assert item["state"] in STATES
        assert item["stage"] is None or item["stage"] in STAGES
        assert item["result"] is None or item["result"] in RESULTS
        assert type(item["reviewed"]) is bool
    return next(item for item in snapshot["cards"] if item["job_id"] == job)


@pytest.mark.parametrize("reviewed", [0, 1])
async def test_reviewed_failure_is_durable_display_metadata_after_reopen_and_restart(
    tmp_path, reviewed
):
    async with running(tmp_path) as (broker, plans, timeline, _):
        store = broker._store()
        store.add_job("old-failure", "a" * 64)
        store.set_job("old-failure", "needs_review", "serial_capture_timeout")
        store.db.execute("UPDATE jobs SET reviewed=? WHERE id='old-failure'", (reviewed,))
        store.db.commit()
        original = store.job("old-failure")
        epoch = broker.epoch
        for view in (timeline, OperatorTimeline(broker, plans)):
            changes = store.db.total_changes
            row = card(view, "old-failure")
            parsed = TimelineSnapshot.parse({"ok": True, **view.snapshot()}, 1000).cards[0]
            assert row["state"] == "needs_review" and row["reviewed"] is bool(reviewed)
            assert row["result"] == "serial_capture_timeout"
            assert parsed.needs_device_review is (reviewed == 0)
            assert parsed.review is parsed.image is None
            assert store.job("old-failure") == original and store.db.total_changes == changes
            assert broker.operator_snapshot()["needs_review"] is (reviewed == 0)
        assert broker.backend.calls == 0 and broker.queue.empty()
    async with running(tmp_path) as (broker, _, timeline, _):
        assert broker.epoch != epoch
        assert broker._store().job("old-failure") == original
        assert card(timeline, "old-failure") == row
        assert broker.status()["history"] == {"needs_review": 1}
        assert broker.status()["needs_review"] is (reviewed == 0)
        assert broker.backend.calls == 0 and broker.queue.empty()


@pytest.mark.parametrize("other", ["job", "device_request"])
async def test_reviewed_history_does_not_mask_another_current_review_or_guard(tmp_path, other):
    async with running(tmp_path) as (broker, _, timeline, _):
        store = broker._store()
        store.add_job("old-failure", "a" * 64)
        store.set_job("old-failure", "needs_review", "serial_capture_timeout")
        store.db.execute("UPDATE jobs SET reviewed=1 WHERE id='old-failure'")
        if other == "job":
            store.add_job("current-failure", "b" * 64)
            store.set_job("current-failure", "needs_review", "broker_restart_no_replay")
        else:
            store.db.execute(
                """INSERT INTO device_requests(id,state,lease_seconds)
                VALUES('current-request','needs_review',300)"""
            )
        store.db.commit()
        changes = store.db.total_changes
        assert card(timeline, "old-failure")["reviewed"] is True
        if other == "job":
            assert card(timeline, "current-failure")["reviewed"] is False
            assert timeline.snapshot()["cards"][0]["job_id"] == "current-failure"
        assert broker.operator_snapshot()["needs_review"] is True
        with pytest.raises(BrokerError, match="^recovery_review_required$"):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        assert store.db.total_changes == changes
        assert broker.backend.calls == 0 and broker.queue.empty()


@pytest.mark.parametrize(
    "value",
    [2, -1, 1.5, "true", "PRIVATE" * 100_000, b"\x01"],
    ids=["integer", "negative", "float", "text", "oversized-text", "bytes"],
)
async def test_malformed_stored_reviewed_flag_is_bounded_and_never_acknowledged(tmp_path, value):
    async with running(tmp_path) as (broker, _, timeline, _):
        store = broker._store()
        store.add_job("history", "a" * 64)
        store.set_job("history", "needs_review", "serial_capture_timeout")
        store.db.execute("UPDATE jobs SET reviewed=? WHERE id='history'", (value,))
        store.db.commit()
        row = card(timeline, "history")
        assert row["reviewed"] is False and row["state"] == "needs_review"
        assert len(json.dumps(row)) < 1000 and "PRIVATE" not in json.dumps(row)
        assert store.db.execute("SELECT reviewed FROM jobs").fetchone()[0] == value
        assert broker.backend.calls == 0


async def test_corrupt_rows_are_redacted_without_parsing_raw_diagnostics(tmp_path):
    async with running(tmp_path) as (broker, _, timeline, _):
        prepare(broker, "healthy")
        db = broker._store().db
        for job, state, plan_hash in (
            ("missing-plan", "succeeded", "f" * 64),
            ("corrupt-job", "SYNTHETIC_PRIVATE_SECRET", "SYNTHETIC_PRIVATE_SECRET"),
        ):
            db.execute(
                """INSERT INTO jobs(id,plan_hash,state,stage,result,created,finished,diagnostic)
                VALUES(?,?,?,?,?,?,?,?)""",
                (
                    job,
                    plan_hash,
                    state,
                    "SYNTHETIC_PRIVATE_SECRET",
                    "SYNTHETIC_PRIVATE_SECRET",
                    "2026-99-99 99:99:99",
                    "SYNTHETIC_PRIVATE_SECRET",
                    "{SYNTHETIC_PRIVATE_SECRET",
                ),
            )
        raw = dict(broker._store().plan(prepare(broker, "broken")["plan_hash"]))
        raw["capability"] = "SYNTHETIC_PRIVATE_SECRET"
        db.execute("INSERT INTO plans VALUES(?,?)", (digest_plan(raw), json.dumps(raw)))
        db.execute("INSERT INTO plans VALUES(?,?)", ("c" * 64, "{"))
        db.execute("INSERT INTO plans VALUES(?,?)", ("d" * 64, "x" * 100_000))
        db.commit()
        snapshot = timeline.snapshot()
        assert "SYNTHETIC_PRIVATE_SECRET" not in json.dumps(snapshot)
        invalid = next(item for item in snapshot["cards"] if item["job_id"] == "corrupt-job")
        assert invalid["state"] == "unknown"
        assert invalid["reviewed"] is False
        assert all(invalid[key] is None for key in CARD_KEYS - {"job_id", "state", "reviewed"})
        missing = next(item for item in snapshot["cards"] if item["job_id"] == "missing-plan")
        assert missing["state"] == "succeeded"
        assert missing["test"] is missing["backend"] is missing["review"] is None
        assert card(timeline, "healthy")["review"] is None


async def test_live_work_and_retained_images_survive_bounded_recent_history(tmp_path):
    async with running(tmp_path) as (broker, _, timeline, _):
        capture = prepare(broker, "old-image", test=Action.CAPTURE)
        broker.execute(
            ExecuteRequest(session=broker.session.capability, plan_hash=capture["plan_hash"])
        )
        await broker.queue.join()
        for index in range(70):
            broker._store().add_job(f"history-{index}", "a" * 64)
            broker._store().set_job(f"history-{index}", "succeeded", "mock_pass")
        prepare(broker, "pending-one")
        prepare(broker, "pending-two")
        snapshot = timeline.snapshot()
        assert len(snapshot["cards"]) == MAX_CARDS and snapshot["has_more"]
        assert {"old-image", "pending-one", "pending-two"} <= {
            item["job_id"] for item in snapshot["cards"]
        }
        broker._store().add_job("durable-queued", "b" * 64)
        snapshot = timeline.snapshot()
        assert snapshot["plans_available"] is False
        assert snapshot["cards"][0]["job_id"] == "durable-queued"
        assert {"old-image", "pending-one", "pending-two"} <= {
            item["job_id"] for item in snapshot["cards"]
        }
        assert all(item["review"] is None for item in snapshot["cards"])
        assert len(snapshot["cards"]) == MAX_CARDS


async def test_retained_image_does_not_read_or_parse_its_corrupt_diagnostic(tmp_path):
    async with running(tmp_path) as (broker, _, timeline, _):
        captured = prepare(broker, "completed", Action.CAPTURE)
        broker.execute(
            ExecuteRequest(session=broker.session.capability, plan_hash=captured["plan_hash"])
        )
        await broker.queue.join()
        db = broker._store().db
        db.execute(
            "UPDATE jobs SET diagnostic=? WHERE id='completed'", ("{" + "PRIVATE" * 100_000,)
        )
        db.commit()
        statements = []
        db.set_trace_callback(statements.append)
        completed = card(timeline, "completed")
        db.set_trace_callback(None)
        assert completed["state"] == "succeeded" and completed["image"] is not None
        assert "PRIVATE" not in json.dumps(completed)
        assert not any("SELECT * FROM jobs" in statement for statement in statements)
        assert broker.backend.calls == 1


async def test_plan_query_and_output_bounds_with_more_than_one_page(tmp_path):
    async with running(tmp_path) as (broker, _, timeline, _):
        for index in range(80):
            prepare(broker, f"pending-{index}")
        statements = []
        broker._store().db.set_trace_callback(statements.append)
        snapshot = timeline.snapshot()
        broker._store().db.set_trace_callback(None)
        assert len(snapshot["cards"]) == MAX_CARDS and snapshot["has_more"]
        assert all(item["review"] is None for item in snapshot["cards"])
        # Every timeline enumeration is bounded at the database, not sliced
        # after materializing all jobs or durable plan payloads.
        enumerations = [
            sql for sql in statements if "ORDER BY j.rowid" in sql or "ORDER BY p.rowid DESC" in sql
        ]
        assert enumerations and all("LIMIT 33" in sql for sql in enumerations)
        assert len(statements) < 3000


async def test_snapshot_never_prepares_or_opens_hardware(tmp_path):
    config = SerialSettings(
        selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number="PRIVATE_USB_SERIAL"),
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [
            PortInfo(port="COM42", vid=0xCAFE, pid=1, serial_number="PRIVATE_USB_SERIAL")
        ],
        factory=lambda: pytest.fail("must not open hardware"),
    )
    async with running(tmp_path, backend=backend) as (broker, _, timeline, _):
        prepare(broker, test=Action.HELLO)
        backend.prepare = lambda *args: pytest.fail("must not prepare hardware")
        backend.inventory = lambda: pytest.fail("must not enumerate USB ports")
        snapshot = timeline.snapshot()
        assert snapshot["cards"][0]["review"] is None
        assert "PRIVATE_USB_SERIAL" not in json.dumps(snapshot)
        assert not backend.hardware_connected and backend.calls == 0


async def test_unavailable_store_uses_fixed_error(tmp_path):
    async with running(tmp_path) as (broker, _, timeline, _):
        await broker.close()
        with pytest.raises(BrokerError, match="^operator_timeline_unavailable$"):
            timeline.snapshot()


def test_fixed_execution_failure_codes_have_bounded_timeline_representations():
    """Catch drift between actual producers and the presentation allowlist."""
    root = Path(__file__).resolve().parents[1] / "src/cardputer_mcp"
    constructors = {
        "FlashFailure",
        "FlashAdmissionError",
        "BuildLocationError",
        "FixedDiagnosticFailure",
        "BackendResult",
    }
    emitted = set()
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
                if node.func.id in constructors or (
                    path.name == "flash_manifest.py" and node.func.id == "_fail"
                ):
                    code = node.args[0]
                    if isinstance(code, ast.Constant) and isinstance(code.value, str):
                        emitted.add(code.value)
            if isinstance(node, ast.ClassDef):
                bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
                if "BuildPreflightFailure" in bases:
                    for statement in node.body:
                        if (
                            isinstance(statement, ast.Assign)
                            and any(
                                isinstance(x, ast.Name) and x.id == "code"
                                for x in statement.targets
                            )
                            and isinstance(statement.value, ast.Constant)
                            and isinstance(statement.value.value, str)
                        ):
                            emitted.add(statement.value.value)
    assert emitted
    assert emitted <= RESULTS, sorted(emitted - RESULTS)


async def test_unrecognized_native_failure_text_remains_private_in_timeline_and_gui(tmp_path):
    async with running(tmp_path) as (broker, _plans, timeline, _clock):
        prepared = prepare(broker, job="private-failure")
        history = broker._store()
        history.add_job("private-failure", prepared["plan_hash"])
        history.set_job("private-failure", "running")
        secret = "synthetic-private-native-output token=do-not-display"
        history.set_job("private-failure", "needs_review", secret)
        listing = timeline.snapshot()
        row = next(x for x in listing["cards"] if x["job_id"] == "private-failure")
        assert row["result"] is None and secret not in json.dumps(listing)
        shown = TimelineSnapshot.parse({"ok": True, **listing}, 0).cards[0]
        assert shown.result is None


async def test_capture_card_only_explicit_execute_and_read_then_cache_expiry(tmp_path):
    async with running(tmp_path) as (broker, plans, timeline, clock):
        prepared = prepare(broker, test=Action.CAPTURE)
        changes = broker._store().db.total_changes
        first = card(timeline)
        assert first["state"] == "prepared" and first["review"] is None
        assert broker.backend.calls == 0 and broker._store().db.total_changes == changes
        assert broker.session.capability not in json.dumps(first)
        broker.backend.gate = asyncio.Event()
        broker.execute(
            ExecuteRequest(session=broker.session.capability, plan_hash=prepared["plan_hash"])
        )
        assert card(timeline)["state"] == "queued"
        await asyncio.sleep(0)
        assert card(timeline)["state"] == "running"
        broker.backend.gate.set()
        await broker.queue.join()
        completed = card(timeline)
        assert completed["state"] == "succeeded"
        assert completed["image"]["delivery_state"] == "available"
        assert "image_base64" not in json.dumps(completed)
        assert broker.read_capture("one", "synthetic-owner").png
        assert broker.backend.calls == 1
        clock.now = 400
        expired = card(timeline)
        assert expired["state"] == "succeeded" and expired["image"] is None
        assert first["plan_hash"] == completed["plan_hash"] == expired["plan_hash"]


async def test_old_prepared_plan_after_restart_never_executes_from_reading_timeline(tmp_path):
    async with running(tmp_path) as (broker, _, timeline, _):
        prepare(broker, "old-prepared")
        assert card(timeline, "old-prepared")["state"] == "prepared"
        epoch = broker.epoch
        broker.release(LeaseRequest(session=broker.session.capability))
    async with running(tmp_path) as (broker, _, timeline, _):
        assert broker.epoch != epoch
        old = card(timeline, "old-prepared")
        assert old["state"] == "expired" and old["review"] is None
        assert broker.queue.empty() and broker.backend.calls == 0
        prepare(broker, "new-prepared")
        assert card(timeline, "new-prepared")["state"] == "prepared"
