"""Dedicated HOME recovery with fake USB, clock and authentication; never real hardware."""

import asyncio
import threading
from collections import deque
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.home_recovery import (
    CancelHomeRecovery,
    HomeRecovery,
    RequestHomeRecovery,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend
from cardputer_mcp.store import Store


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class Port:
    def __init__(self, clock):
        self.clock = clock
        self.is_open = False
        self.dtr = self.rts = False
        self.port = None
        self.queue = deque()
        self.reply = [b"APP_STOPPED\nHOME_READY\n"]
        self.writes = []
        self.opens = self.closes = 0
        self.close_fails = False
        self.on_open = lambda: None
        self.on_read = lambda: None
        self.on_close = lambda: None

    def open(self):
        self.opens += 1
        self.is_open = True
        self.on_open()

    def read(self, size=4096):
        self.clock.now += 0.05
        self.on_read()
        data = self.queue.popleft() if self.queue else b""
        if len(data) > size:
            self.queue.appendleft(data[size:])
        return data[:size]

    def write(self, data):
        assert data == b"q"
        self.writes.append(data)
        self.queue.extend(self.reply)
        return len(data)

    def close(self):
        self.closes += 1
        self.on_close()
        if self.close_fails:
            raise OSError("synthetic private error")
        self.is_open = False

    def reset_input_buffer(self):
        raise AssertionError("Do not erase stale receive evidence")


@asynccontextmanager
async def running(tmp_path):
    clock = Clock()
    port = Port(clock)
    identity = PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")
    inventory = [identity]
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(config, inventory=lambda: inventory, factory=lambda: port, clock=clock)
    grants = {"client": True}
    broker = Broker(
        Settings(state_dir=tmp_path / "state", backend="serial", serial=config),
        backend=backend,
        clock=clock,
        lock_path=tmp_path / "device.lock",
        serial_enabled=True,
        principal_valid=lambda principal, *_: grants.get(principal, False),
    )
    await broker.start()
    plan = dict(
        version=1,
        broker_epoch=broker.epoch,
        session_id="d" * 32,
        backend="serial",
        device="configured-cardputer",
        job_id="failed-derby",
        test="derby_smoke",
        **backend.prepare(Action.DERBY),
    )
    store = broker._store()
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    store.add_job(plan["job_id"], digest)
    store.db.execute("UPDATE jobs SET stage='serial_close' WHERE id=?", (plan["job_id"],))
    store.db.commit()
    store.set_job(
        plan["job_id"],
        "needs_review",
        "serial_capture_invalid",
        {
            "expected_marker": "CAPTURE_END",
            "receive_summary": {"last_command": "capture_frame", "command_write_completed": True},
        },
    )
    backend.last_closed_plan = deepcopy(plan)
    manager = broker.home_recovery
    manager.record_closed(plan, "client", "remote_mcp")
    env = SimpleNamespace(
        b=broker,
        m=manager,
        port=port,
        clock=clock,
        backend=backend,
        grants=grants,
        source=plan,
        inventory=inventory,
    )
    try:
        yield env
    finally:
        port.close_fails = False
        for e in manager.entries.values():
            e.stop.set()
            if e.task:
                await e.task
        # Fake teardown only; never alter a production uncertainty hold.
        broker.active_job = None
        backend.active = 0
        backend.hardware_connected = False
        backend.ownership_uncertain = False
        await broker.close()


def request(e, job="recover-one"):
    return e.m.request(
        RequestHomeRecovery(job_id=job, source_job_id=e.source["job_id"]), "client", "remote_mcp"
    )


async def finish(e, prepared):
    await e.m.entries[prepared["plan_hash"]].task
    return prepared, e.b.job(prepared["plan"]["job_id"])


async def test_exact_recovery_records_atomic_resolution_and_cannot_repeat(tmp_path):
    async with running(tmp_path) as e:
        p = request(e)
        assert not e.port.opens and e.b._store().needs_review()
        assert p["state"] == "running" and p["job_id"] == "recover-one"
        assert "attestation" not in p and "review_revision" not in p
        assert e.b._store().home_source(e.source["job_id"])["attempted"] == 1
        receipt = e.b._store().db.execute("SELECT * FROM home_recovery_executions").fetchone()
        assert receipt["principal_id"] == "client"
        assert receipt["source_hash"] == digest_plan(e.source)
        assert receipt["recovery_hash"] == p["plan_hash"]
        assert e.b._store().db.execute("SELECT count(*) FROM home_recovery_approvals").fetchone()[0] == 0
        _, job = await finish(e, p)
        assert job["state"] == "succeeded", job
        assert e.port.writes == [b"q"] and e.port.opens == e.port.closes == 1
        source = e.b.job(e.source["job_id"])
        assert source["state"] == "needs_review" and source["result"] == "serial_capture_invalid"
        assert source["reviewed"] == 1 and not e.b._store().needs_review()
        assert e.b.active_job is None
        with pytest.raises(BrokerError):
            request(e, "again")
        assert not hasattr(e.m, "approve") and not hasattr(e.m, "review")


@pytest.mark.parametrize(
    "before,reply",
    [
        ([b"APP_STOPPED\nHOME_READY\n"], []),
        ([], [b"HOME_READY\n"]),
        ([], [b"HOME_READY\nAPP_STOPPED\n"]),
        ([], [b"APP_STOPPED\nHOME_READY\nHOME_READY\n"]),
        ([], [b"APP_ID local.other\nAPP_STOPPED\nHOME_READY\n"]),
        ([b"HOME_RE"], [b"APP_STOPPED\nHOME_READY\n"]),
        ([b"ESP-ROM:synthetic\n"], [b"APP_STOPPED\nHOME_READY\n"]),
        ([b"APP_ID local.other\n"], [b"APP_STOPPED\nHOME_READY\n"]),
    ],
)
async def test_old_incomplete_or_wrong_home_is_not_fresh_success(tmp_path, before, reply):
    async with running(tmp_path) as e:
        e.port.queue.extend(before)
        e.port.reply = reply
        _, job = await finish(e, request(e))
        assert job["state"] == "needs_review"
        assert e.b.job(e.source["job_id"])["reviewed"] == 0
        assert len(e.port.writes) <= 1 and e.port.opens == e.port.closes == 1


async def test_home_followed_by_close_failure_retains_owner_and_review(tmp_path):
    async with running(tmp_path) as e:
        e.port.close_fails = True
        _, job = await finish(e, request(e))
        assert job["state"] == "needs_review"
        assert e.backend.ownership_uncertain and e.backend.hardware_connected
        assert e.backend.active == 1 and e.b.active_job == job["id"]
        assert e.b.job(e.source["job_id"])["reviewed"] == 0


@pytest.mark.parametrize(
    "change", ["expired", "revoked", "epoch", "target", "firmware", "cancelled"]
)
async def test_explicit_execution_cannot_revive_changed_authority(tmp_path, change):
    async with running(tmp_path) as e:
        p = request(e)
        if change == "expired":
            e.clock.now += 30
        elif change == "revoked":
            e.grants["client"] = False
        elif change == "epoch":
            e.b.epoch = "9" * 32
        elif change == "target":
            e.inventory[:] = []
        elif change == "firmware":
            e.backend.config = e.backend.config.model_copy(
                update={"expected_firmware_sha256": "9" * 64}
            )
        else:
            e.m.cancel(
                CancelHomeRecovery(job_id=p["plan"]["job_id"], plan_hash=p["plan_hash"]), "client"
            )
        _, job = await finish(e, p)
        assert job["state"] == "needs_review"
        assert not e.port.opens and not e.port.writes


@pytest.mark.parametrize(
    "change", ["review", "active", "uncertain", "unknown_close", "source_wrong", "flash"]
)
async def test_other_reviews_and_unknown_or_busy_ownership_are_refused(tmp_path, change):
    async with running(tmp_path) as e:
        if change in ("review", "flash"):
            e.b._store().add_job("other-job", "c" * 64)
            e.b._store().set_job(
                "other-job",
                "needs_review",
                "flash_readback_failed" if change == "flash" else "other_failure",
            )
        elif change == "active":
            e.b.active_job = "other-job"
        elif change == "uncertain":
            e.backend.ownership_uncertain = True
        elif change == "unknown_close":
            e.backend.hardware_connected = None
        else:
            e.b._store().db.execute("DELETE FROM home_closed_sources")
            e.b._store().db.commit()
        with pytest.raises(BrokerError):
            request(e)
        assert not e.port.opens


@pytest.mark.parametrize("change", ["revoked", "expired"])
async def test_final_audit_callback_cannot_resolve_stale_authority(tmp_path, monkeypatch, change):
    async with running(tmp_path) as e:
        store = e.b._store()
        audit = store.home_audit

        def changed(source, recovery, event):
            audit(source, recovery, event)
            if event == "fresh_home_and_close_confirmed":
                if change == "revoked":
                    e.grants["client"] = False
                else:
                    e.clock.now += 16

        monkeypatch.setattr(store, "home_audit", changed)
        _, job = await finish(e, request(e))
        assert job["state"] == "needs_review"
        assert e.b.job(e.source["job_id"])["reviewed"] == 0
        assert e.port.writes == [b"q"]
        assert not store.db.execute(
            "SELECT 1 FROM home_recovery_audit WHERE event='fresh_home_and_close_confirmed'"
        ).fetchone()


@pytest.mark.parametrize("field", ["source_hash", "recovery_hash", "principal_id", "proof"])
async def test_final_commit_requires_exact_execution_receipt(tmp_path, monkeypatch, field):
    async with running(tmp_path) as e:
        store = e.b._store()
        audit = store.home_audit

        def changed(source, recovery, event):
            audit(source, recovery, event)
            if event == "fresh_home_and_close_confirmed":
                value = "{}" if field == "proof" else "different"
                store.db.execute(
                    f"UPDATE home_recovery_executions SET {field}=? WHERE recovery_job=?",
                    (value, recovery),
                )

        monkeypatch.setattr(store, "home_audit", changed)
        _, job = await finish(e, request(e))
        assert job["state"] == "needs_review"
        assert e.b.job(e.source["job_id"])["reviewed"] == 0
        assert e.port.writes == [b"q"]


@pytest.mark.parametrize("principal,origin", [("other-client", "remote_mcp"), ("client", "local_cli")])
async def test_direct_request_cannot_take_another_clients_source(tmp_path, principal, origin):
    async with running(tmp_path) as e:
        with pytest.raises(BrokerError, match="home_recovery_source_not_eligible"):
            e.m.request(
                RequestHomeRecovery(job_id="foreign", source_job_id=e.source["job_id"]),
                principal,
                origin,
            )
        assert not e.port.opens and e.b._store().job("foreign") is None


async def test_direct_request_still_requires_live_read_and_control(tmp_path):
    async with running(tmp_path) as e:
        scopes_seen = []

        def only_read(principal, origin, scopes):
            scopes_seen.append(scopes)
            return scopes <= {"read"}

        e.b.principal_valid = only_read
        with pytest.raises(BrokerError, match="home_recovery_unavailable"):
            request(e)
        assert frozenset({"read", "control"}) in scopes_seen
        assert not e.port.opens and not e.port.writes


@pytest.mark.parametrize("when", ["open", "read", "after_home"])
async def test_cancellation_or_loss_during_io_never_reinputs_or_commits(tmp_path, when):
    async with running(tmp_path) as e:
        p = request(e)
        entry = e.m.entries[p["plan_hash"]]
        if when == "open":
            e.port.on_open = entry.stop.set
        elif when == "read":
            e.port.on_read = lambda: entry.stop.set() if e.port.writes else None
        else:
            e.port.on_close = entry.stop.set
        _, job = await finish(e, p)
        assert job["state"] == "needs_review"
        assert e.b.job(e.source["job_id"])["reviewed"] == 0
        assert len(e.port.writes) <= 1 and e.port.opens == e.port.closes == 1
        with pytest.raises(BrokerError):
            request(e, "retry")


@pytest.mark.parametrize("change", ["revoked", "expired"])
async def test_final_close_callback_cannot_commit_stale_authority(tmp_path, change):
    async with running(tmp_path) as e:
        p = request(e)

        def changed():
            if change == "revoked":
                e.grants["client"] = False
            else:
                e.clock.now += 30

        e.port.on_close = changed
        _, job = await finish(e, p)
        assert job["state"] == "needs_review"
        assert e.b.job(e.source["job_id"])["reviewed"] == 0
        assert e.port.writes == [b"q"] and e.port.closes == 1


@pytest.mark.parametrize("event", ["serial_open", "serial_close", "fresh_home_and_close_confirmed"])
async def test_resolution_audit_failure_rolls_back_the_source_review(tmp_path, monkeypatch, event):
    async with running(tmp_path) as e:
        p = request(e)
        store = e.b._store()
        audit = store.home_audit

        def failed(source, recovery, actual):
            if actual == event:
                raise OSError("synthetic audit failure")
            audit(source, recovery, actual)

        monkeypatch.setattr(store, "home_audit", failed)
        _, job = await finish(e, p)
        assert job["state"] == "needs_review"
        assert e.b.job(e.source["job_id"])["reviewed"] == 0
        assert store.home_source(e.source["job_id"])["attempted"] == 1


async def test_close_in_progress_retains_reservation_even_after_cancel(tmp_path):
    async with running(tmp_path) as e:
        p = request(e)
        entered, release = threading.Event(), threading.Event()

        def blocked_close():
            entered.set()
            assert release.wait(5)

        e.port.on_close = blocked_close
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            e.m.cancel(
                CancelHomeRecovery(job_id=p["plan"]["job_id"], plan_hash=p["plan_hash"]), "client"
            )
            assert e.b.active_job == p["plan"]["job_id"] and e.backend.active == 1
            assert e.backend.hardware_connected is True
            assert e.b.job(e.source["job_id"])["reviewed"] == 0
        finally:
            release.set()
            await e.m.entries[p["plan_hash"]].task
        assert e.b.job(p["plan"]["job_id"])["state"] == "needs_review"
        assert e.port.writes == [b"q"] and e.port.opens == 1


async def test_receive_budget_stops_before_input_without_discard_or_retry(tmp_path):
    async with running(tmp_path) as e:
        e.port.queue.extend([b"x\n" * 2000] * 6)
        p = request(e)
        _, job = await finish(e, p)
        assert job["state"] == "needs_review"
        assert not e.port.writes and e.port.opens == e.port.closes == 1
        assert e.b.job(e.source["job_id"])["reviewed"] == 0


async def test_enumeration_callback_revocation_cannot_reach_open(tmp_path):
    async with running(tmp_path) as e:
        p = request(e)

        def revoked_inventory():
            e.grants["client"] = False
            return e.inventory

        e.backend.inventory = revoked_inventory
        _, job = await finish(e, p)
        assert job["state"] == "needs_review"
        assert not e.port.opens and not e.port.writes


async def test_nonfinite_deadline_never_grants(tmp_path):
    async with running(tmp_path) as e:
        p = request(e)
        e.m.entries[p["plan_hash"]].deadline = float("nan")
        _, job = await finish(e, p)
        assert job["state"] == "needs_review"
        assert not e.port.opens


@pytest.mark.parametrize("change", ["success", "failure", "cancel"])
async def test_consumption_survives_fresh_store_and_manager(tmp_path, change):
    async with running(tmp_path) as e:
        p = request(e)
        if change == "failure":
            e.port.reply = [b"HOME_READY\n"]
        elif change == "cancel":
            e.port.on_open = e.m.entries[p["plan_hash"]].stop.set
        await finish(e, p)
        reopened = Store(e.b.settings.state_dir / "history.sqlite3")
        try:
            assert reopened.home_source(e.source["job_id"])["attempted"] == 1
        finally:
            reopened.db.close()
        manager = HomeRecovery(e.b)
        with pytest.raises(BrokerError):
            manager.request(
                RequestHomeRecovery(job_id="retry-after-restart", source_job_id=e.source["job_id"]),
                "client",
                "remote_mcp",
            )
        assert e.port.opens == 1 and len(e.port.writes) <= 1


@pytest.mark.parametrize("change", ["test", "stage", "write", "close"])
async def test_close_receipt_is_not_created_for_ineligible_source(tmp_path, change):
    async with running(tmp_path) as e:
        store = e.b._store()
        store.db.execute("DELETE FROM home_closed_sources")
        store.db.commit()
        plan = deepcopy(e.source)
        if change == "test":
            plan["test"] = "capture_home"
        elif change == "stage":
            store.set_stage(plan["job_id"], "serial_open")
        elif change == "write":
            store.set_job(
                plan["job_id"],
                "needs_review",
                "serial_capture_invalid",
                {
                    "expected_marker": "CAPTURE_END",
                    "receive_summary": {
                        "last_command": "capture_frame",
                        "command_write_completed": False,
                    },
                },
            )
        else:
            e.backend.last_closed_plan = None
        e.m.record_closed(plan, "client", "remote_mcp")
        assert store.home_source(plan["job_id"]) is None
        with pytest.raises(BrokerError):
            request(e)
        assert not e.port.opens
