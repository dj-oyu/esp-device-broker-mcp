"""App-session events/logs and flash outcome summaries survive RAM loss and restart.

Synthetic devices only (mock app link, fake flash driver); never hardware evidence.
"""

import asyncio
import json
import os

import pytest
import test_flash_policy

from cardputer_mcp import job_logs
from cardputer_mcp.app_events import AppEvents
from cardputer_mcp.app_models import AppEventCursor, AppEventsRequest, AppSessionOptions
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.job_logs import JobLogStore
from cardputer_mcp.models import (
    AcquireRequest,
    BuildLogRequest,
    CancelRequest,
    ExecuteRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Workflow

env = test_flash_policy.env
SECRET = "SYNTHETIC_TOKEN_VALUE"
PRIVATE = "C:/synthetic/private/path"


def _broker(tmp_path, principal_valid=lambda *args: True):
    return Broker(
        Settings(state_dir=tmp_path / "state"),
        lock_path=tmp_path / "device.lock",
        principal_valid=principal_valid,
    )


async def _run_app(broker, job_id, *, cancel=False):
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    prepared = broker.prepare(
        PrepareRequest(
            session=token,
            job_id=job_id,
            test=Workflow.APP_SESSION,
            app=AppSessionOptions(app_id="local.hello", duration_ms=1000),
        )
    )
    broker.execute(ExecuteRequest(session=token, plan_hash=prepared["plan_hash"]))
    if cancel:
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            run = broker.app_sessions.jobs.get(job_id)
            if run is not None and run.state == "running":
                break
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.005)
        broker.cancel(CancelRequest(session=token, job_id=job_id))
    await asyncio.wait_for(broker.queue.join(), 5)


@pytest.mark.parametrize("cancel", [False, True])
def test_app_session_events_survive_restart_owner_only(tmp_path, cancel):
    async def scenario():
        broker = _broker(tmp_path)
        await broker.start()
        try:
            await _run_app(broker, "app-one", cancel=cancel)
            ram = broker.read_app_events(AppEventsRequest(job_id="app-one"), "owner")
            assert "source" not in ram
            ram_kinds = [event["kind"] for event in ram["entries"]]
            # RAM eviction (bounded app job table) falls back to the durable copy.
            broker.app_sessions.jobs.clear()
            evicted = broker.read_app_events(AppEventsRequest(job_id="app-one"), "owner")
            assert evicted["source"] == "durable_job_log"
            assert [event["kind"] for event in evicted["entries"]] == ram_kinds
        finally:
            await broker.close()
        restarted = _broker(tmp_path)
        await restarted.start()
        try:
            report = restarted.read_app_events(AppEventsRequest(job_id="app-one"), "owner")
            kinds = [event["kind"] for event in report["entries"]]
            assert report["source"] == "durable_job_log" and report["expired"] is False
            assert "started" in kinds and kinds[-1] == "finished"
            assert report["state"] == ("cancelled" if cancel else "succeeded")
            assert kinds == ram_kinds
            with pytest.raises(BrokerError, match="^app_events_not_available$"):
                restarted.read_app_events(AppEventsRequest(job_id="app-one"), "other")
            with pytest.raises(BrokerError, match="^app_events_not_available$"):
                restarted.read_app_events(AppEventsRequest(job_id="missing"), "owner")
            # Cursor paging over the durable copy; a foreign epoch resets with a gap.
            first = restarted.read_app_events(AppEventsRequest(job_id="app-one", limit=1), "owner")
            assert len(first["entries"]) == 1 and first["has_more"] is True
            cursor = AppEventCursor(**first["next_cursor"])
            rest = restarted.read_app_events(
                AppEventsRequest(job_id="app-one", cursor=cursor), "owner"
            )
            assert [event["kind"] for event in rest["entries"]] == kinds[1:]
            foreign = AppEventCursor(epoch="0" * 32, control=1, log=0)
            reset = restarted.read_app_events(
                AppEventsRequest(job_id="app-one", cursor=foreign), "owner"
            )
            assert reset["gap"] is True and len(reset["entries"]) == len(kinds)
            ahead = AppEventCursor(epoch=cursor.epoch, control=999, log=0)
            with pytest.raises(BrokerError, match="^cursor_ahead$"):
                restarted.read_app_events(AppEventsRequest(job_id="app-one", cursor=ahead), "owner")
            assert restarted.backend.calls == 0
        finally:
            await restarted.close()

    asyncio.run(scenario())


def test_app_events_tee_redacts_logs_and_events(tmp_path):
    store = JobLogStore(tmp_path)
    events = AppEvents("owner", 262144)
    events.logs.redactions = (PRIVATE,)
    events.attach_durable(store.begin("app-two", "owner", "app_session"))
    events.log(f"I (1) app: token={SECRET} at {PRIVATE}")
    events.emit("ack", command_id=2, result="ok", note=f"bearer {SECRET} {PRIVATE}")
    events.durable.close()
    raw = (tmp_path / "app-two.jsonl").read_text("utf-8")
    assert SECRET not in raw and PRIVATE not in raw
    report = store.app_events_read("app-two", "owner", AppEventsRequest(job_id="app-two"))
    assert report is not None
    assert [entry["kind"] for entry in report["entries"]] == ["ack"]
    assert report["logs"][0]["stream"] == "observed_during_run"
    assert "[REDACTED]" in report["logs"][0]["text"]
    assert store.app_events_read("app-two", "other", AppEventsRequest(job_id="app-two")) is None


def test_records_are_redacted_skipped_by_stream_readers_and_reserved(tmp_path, monkeypatch):
    monkeypatch.setattr(job_logs, "MAX_JOB_BYTES", 400)
    store = JobLogStore(tmp_path)
    log = store.begin("job-r", "alice", "firmware_flash")
    log.redactions = (PRIVATE,)
    log.append("flash", "-- flash_write")
    log.record("flash_outcome", {"reason_code": "x", "note": f"{PRIVATE} token={SECRET}"})
    for _ in range(20):
        log.append("flash", "y" * 40)
    assert log.truncated
    # Ordinary records are dropped after the cap; a reserved terminal one is kept.
    log.record("app_event", {"sequence": 1, "kind": "late"})
    log.record("flash_outcome", {"reason_code": "final"}, reserve=True)
    assert not log.unavailable
    # An unknown record kind is refused like an unknown stream: never raises.
    log.record("not_a_record", {"x": 1})
    assert log.unavailable
    log.close()
    raw = (tmp_path / "job-r.jsonl").read_text("utf-8")
    assert SECRET not in raw and PRIVATE not in raw
    outcomes = store.records("job-r", "alice", "flash_outcome")
    assert [item["data"]["reason_code"] for item in outcomes] == ["x", "final"]
    assert store.records("job-r", "alice", "app_event") == []
    assert store.records("job-r", "bob", "flash_outcome") is None
    stream = store.read("job-r", "alice", "flash")
    assert all("record" not in entry for entry in stream["entries"])
    assert stream["entries"][0]["text"] == "-- flash_write"


def test_prune_evicts_ordinary_succeeded_logs_before_protected_ones(tmp_path, monkeypatch):
    store = JobLogStore(tmp_path)
    order = ["review-old", "flash-old", "ok-old", "failed-mid", "ok-new"]
    for index, job_id in enumerate(order):
        store.begin(job_id, "alice", "kind").close()
        stamp = 1_000_000_000 + index
        os.utime(tmp_path / f"{job_id}.jsonl", (stamp, stamp))
    monkeypatch.setattr(job_logs, "MAX_JOB_FILES", 4)
    tiers = {"review-old": 2, "flash-old": 2, "failed-mid": 1}
    store.begin("next", "alice", "kind", tiers=tiers).close()
    remaining = sorted(path.stem for path in tmp_path.glob("*.jsonl"))
    # Two evictions: the oldest succeeded ordinary logs go first, protected ones stay.
    assert remaining == ["failed-mid", "flash-old", "next", "review-old"]


def test_pre_record_v1_job_log_still_reads(tmp_path):
    # Exact layout written before records and the `app` stream existed.
    lines = [
        {
            "schema_version": 1,
            "job_id": "old-job",
            "principal_id": "alice",
            "kind": "firmware_flash",
            "created": "2026-01-01T00:00:00+00:00",
        },
        {"seq": 1, "t": "2026-01-01T00:00:01+00:00", "stream": "flash", "level": "info",
         "text": "-- flash_write"},
    ]  # fmt: skip
    (tmp_path / "old-job.jsonl").write_bytes(
        b"".join(json.dumps(line, separators=(",", ":")).encode() + b"\n" for line in lines)
    )
    store = JobLogStore(tmp_path)
    assert store.records("old-job", "alice", "flash_outcome") == []
    assert store.app_events_read("old-job", "alice", AppEventsRequest(job_id="old-job")) is None
    report = store.build_read("old-job", "alice", "flash", BuildLogRequest(job_id="old-job"))
    assert [entry["text"] for entry in report["entries"]] == ["-- flash_write"]
    # Reopening appends records after the old entries, continuing the sequence.
    log = store.begin("old-job", "alice", "firmware_flash")
    log.record("flash_outcome", {"state": "failed"}, reserve=True)
    log.close()
    assert store.records("old-job", "alice", "flash_outcome")[0]["seq"] == 2


async def _flash(env, job, **references):
    prepared = env.prepare(job, **(references or {"build_job_id": "build-one"}))
    env.execute(prepared)
    await env.broker.queue.join()
    return prepared


def _outcome(env, job):
    env.broker.firmware_flash.logs.clear()
    env.broker.firmware_flash.results.clear()
    result = env.broker.firmware_flash.read_logs(BuildLogRequest(job_id=job), "alice")
    denied = env.broker.firmware_flash.read_logs(BuildLogRequest(job_id=job), "bob")
    assert "flash_outcome" not in denied
    return result["flash_outcome"]


async def test_failed_flash_outcome_is_persisted_and_readable_after_ram_loss(env):
    original = env.driver.write

    def corrupting_write(segments):
        original(segments)
        offset, _ = segments[-1]
        env.driver.data[offset] ^= 0xFF

    env.driver.write = corrupting_write
    prepared = await _flash(env, "readback")
    job = env.broker.job("readback")
    assert job["state"] == "needs_review"
    outcome = _outcome(env, "readback")
    assert outcome["state"] == "needs_review"
    assert outcome["reason_code"] == "flash_readback_mismatch_needs_review"
    assert outcome["phase_reached"] == "flash_readback_verify"
    assert "flash_write" in [phase["name"] for phase in outcome["phases"]]
    assert outcome["write_started"] is True
    assert outcome["plan_hash"] == prepared["plan_hash"]
    assert outcome["device"]["device_id"] == "fixture"
    assert outcome["device"]["device_fingerprint"] == prepared["plan"]["device_fingerprint"]
    assert outcome["references"]["build_job_id"] == "build-one"
    assert outcome["lkg_selected"] is False
    assert outcome["timings"]["recorded"] and outcome["timings"]["first_phase"]
    text = json.dumps(outcome)
    assert str(env.broker.settings.state_dir) not in text


async def test_prewrite_failure_outcome_says_no_write_and_success_names_lkg(env):
    metadata_root = env.broker.settings.flash_devices["fixture"].backup_directory / "lkg"
    from cardputer_mcp.flash_lkg_store import FirmwareSnapshots

    metadata, _ = FirmwareSnapshots(metadata_root, windows_private_acl_confirmed=True).load(
        env.selected
    )
    app = metadata["bundle"]["segments"][-1]
    env.driver.data[app["offset"] + 3] ^= 1
    await _flash(env, "prewrite", build_job_id="build-one", lkg_receipt=env.selected)
    failed = _outcome(env, "prewrite")
    assert failed["state"] == "failed"
    assert failed["reason_code"] == "lkg_current_firmware_changed_needs_review"
    assert failed["write_started"] is False and failed["close_confirmed"] is True
    assert failed["references"]["lkg_receipt"] == env.selected
    env.driver.data[app["offset"] + 3] ^= 1
    await _flash(env, "control")
    succeeded = _outcome(env, "control")
    assert succeeded["state"] == "succeeded" and succeeded["write_started"] is True
    assert succeeded["lkg_selected"] is True
    assert succeeded["staged_lkg_receipt"] == env.store.firmware_current("fixture")["receipt"]
    assert succeeded["verification"]["readback_verified"] is True


async def test_flash_outcome_is_on_disk_for_a_fresh_reader(env):
    await _flash(env, "durable-flash")
    # A new store instance over the same directory models a restarted broker.
    store = JobLogStore(env.broker.job_logs.root)
    outcomes = store.records("durable-flash", "alice", "flash_outcome")
    assert outcomes and outcomes[-1]["data"]["state"] == "succeeded"
    assert store.records("durable-flash", "bob", "flash_outcome") is None
