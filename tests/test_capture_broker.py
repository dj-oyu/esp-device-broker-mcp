"""Capture authorization, retention and lifecycle, without ports or hardware."""

import asyncio
import base64
import gc
import json
import weakref
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from cardputer_mcp.backend import BackendResult, MockBackend
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.capture import mock_image
from cardputer_mcp.config import Settings
from cardputer_mcp.models import (
    AcquireRequest,
    CancelRequest,
    ExecuteRequest,
    LeaseRequest,
    LogRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as DeviceTest
from cardputer_mcp.operator import approve_plan
from cardputer_mcp.store import Store

PRINCIPAL = "synthetic-capture-owner"


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


@asynccontextmanager
async def running(
    path: Path,
    *,
    backend: MockBackend | None = None,
    clock: Clock | None = None,
    allow_mock_execution: bool = True,
    max_queue: int = 32,
) -> AsyncIterator[Broker]:
    instance = Broker(
        Settings(
            state_dir=path / "state",
            allow_mock_execution=allow_mock_execution,
            max_queue=max_queue,
        ),
        backend=backend or MockBackend(0.001),
        clock=clock or Clock(),
        lock_path=path / "device.lockfile",
    )
    await instance.start()
    try:
        yield instance
    finally:
        if instance.backend.gate is not None:
            instance.backend.gate.set()
        await instance.close()


def acquire(broker: Broker, principal: str | None = PRINCIPAL) -> str:
    return broker.acquire(
        AcquireRequest(owner=Origin.REMOTE, lease_seconds=300), principal_id=principal
    )["session"]


def prepare(broker: Broker, session: str, job_id: str) -> dict[str, Any]:
    return broker.prepare(PrepareRequest(session=session, job_id=job_id, test=DeviceTest.CAPTURE))


def enqueue(broker: Broker, session: str, job_id: str) -> ExecuteRequest:
    plan = prepare(broker, session, job_id)
    request = ExecuteRequest(session=session, plan_hash=plan["plan_hash"])
    broker.execute(request)
    return request


def approve_image(broker: Broker, job_id: str) -> None:
    """Simulate a local operator reviewing this exact in-memory image."""
    with pytest.raises(BrokerError, match="^capture_delivery_approval_required$"):
        broker.read_capture(job_id, PRINCIPAL)
    preview = broker.operator_image(job_id)
    assert preview["delivery_state"] == "pending"
    assert broker.operator_decide_image(
        job_id, preview["sha256"], preview["delivery_revision"], "approve"
    ) == {"job_id": job_id, "delivery_state": "approved"}


def unavailable(broker: Broker, job_id: str, principal: str = PRINCIPAL) -> None:
    with pytest.raises(BrokerError, match="^capture_not_available$"):
        broker.read_capture(job_id, principal)


async def test_capture_requires_principal_bound_lease_before_preparing(tmp_path: Path) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker, None)
        with pytest.raises(BrokerError, match="^authentication_required$"):
            prepare(broker, session, "anonymous")
        assert broker._store().record_count("plans") == 0
        assert broker.backend.calls == 0
        unavailable(broker, "anonymous")


async def test_capture_exact_approval_consumed_once_and_execute_deduplicated(
    tmp_path: Path,
) -> None:
    async with running(tmp_path, allow_mock_execution=False) as broker:
        session = acquire(broker)
        first = prepare(broker, session, "approved")
        other = prepare(broker, session, "unapproved")
        request = ExecuteRequest(session=session, plan_hash=first["plan_hash"])
        assert first["approval_required"]
        with pytest.raises(BrokerError, match="^operator_approval_required$"):
            broker.execute(request)
        with pytest.raises(ValueError, match="^approval_not_granted$"):
            approve_plan(broker.settings.state_dir, first["plan_hash"], lambda _: "yes")
        approve_plan(
            broker.settings.state_dir,
            first["plan_hash"],
            lambda _: f"APPROVE {first['plan_hash']}",
        )
        with pytest.raises(BrokerError, match="^operator_approval_required$"):
            broker.execute(ExecuteRequest(session=session, plan_hash=other["plan_hash"]))
        assert not broker.execute(request)["deduplicated"]
        assert broker.execute(request)["deduplicated"]
        await broker.queue.join()
        assert broker.execute(request)["deduplicated"]
        assert broker.backend.calls == 1
        assert broker._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        assert broker.job("approved")["result"] == "synthetic_capture_not_device"
        approve_image(broker, "approved")
        assert broker.read_capture("approved", PRINCIPAL).png == mock_image().png
        unavailable(broker, "unapproved")


async def test_capture_principal_isolation_release_and_repeated_reads_do_not_recapture(
    tmp_path: Path,
) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker)
        enqueue(broker, session, "private")
        await broker.queue.join()
        approve_image(broker, "private")
        image = broker.read_capture("private", PRINCIPAL)
        for principal in ("different-principal", "", "ephemeral:remote_mcp"):
            unavailable(broker, "private", principal)
        broker.release(LeaseRequest(session=session))
        acquire(broker, "different-principal")
        for _ in range(3):
            assert broker.read_capture("private", PRINCIPAL) == image
            unavailable(broker, "missing")
        assert broker.backend.calls == 1


@pytest.mark.parametrize("prune", ["read", "maintenance"])
async def test_capture_expires_at_five_minutes_without_read_refresh_or_recapture(
    tmp_path: Path, prune: str
) -> None:
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        session = acquire(broker)
        enqueue(broker, session, "expires")
        await broker.queue.join()
        broker.release(LeaseRequest(session=session))
        approve_image(broker, "expires")
        clock.now = 399.999
        assert broker.read_capture("expires", PRINCIPAL).png
        clock.now = 400.0
        if prune == "maintenance":
            broker.maintenance_tick()
            assert not broker.captures
        unavailable(broker, "expires")
        assert not broker.captures
        assert broker.job("expires")["state"] == "succeeded"
        assert broker.backend.calls == 1


async def test_capture_memory_cache_evicts_oldest_completion_at_eight(tmp_path: Path) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker)
        for number in range(9):
            enqueue(broker, session, f"capture-{number}")
            await broker.queue.join()
            assert len(broker.captures) <= 8
        unavailable(broker, "capture-0")
        for number in range(1, 9):
            approve_image(broker, f"capture-{number}")
            assert broker.read_capture(f"capture-{number}", PRINCIPAL).png
        assert broker.job("capture-0")["state"] == "succeeded"
        assert broker.backend.calls == 9


async def test_expired_capture_releases_worker_reference_and_pixel_memory(tmp_path: Path) -> None:
    clock = Clock()
    async with running(tmp_path, clock=clock) as broker:
        session = acquire(broker)
        enqueue(broker, session, "discard-pixels")
        await broker.queue.join()
        approve_image(broker, "discard-pixels")
        image = broker.read_capture("discard-pixels", PRINCIPAL)
        retained = weakref.ref(image)
        del image
        assert retained() is not None
        clock.now = 400.0
        broker.maintenance_tick()
        gc.collect()
        assert not broker.captures
        assert retained() is None
        unavailable(broker, "discard-pixels")
        assert broker.backend.calls == 1


class ClosingCapture(MockBackend):
    """Synthetic physical close gate: pixels exist before transport is closed."""

    def __init__(self) -> None:
        super().__init__(0.001)
        self.pixels_ready = asyncio.Event()
        self.close_allowed = asyncio.Event()

    async def execute(self, plan: dict[str, Any], stage: Callable[[str], None]) -> BackendResult:
        self.active = 1
        self.calls += 1
        self.hardware_connected = True
        image = mock_image()
        self.pixels_ready.set()
        await self.close_allowed.wait()
        self.hardware_connected = False
        self.active = 0
        return BackendResult("synthetic_capture_not_device", image)


async def test_capture_pixels_unavailable_and_owner_held_until_close_completes(
    tmp_path: Path,
) -> None:
    backend = ClosingCapture()
    async with running(tmp_path, backend=backend) as broker:
        session = acquire(broker)
        enqueue(broker, session, "closing")
        try:
            await asyncio.wait_for(backend.pixels_ready.wait(), timeout=2)
            assert broker.job("closing")["state"] == "running"
            unavailable(broker, "closing")
            assert not broker.captures
            with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                broker.release(LeaseRequest(session=session))
            with pytest.raises(BrokerError, match="^device_busy$"):
                acquire(broker, "other")
            with pytest.raises(BrokerError, match="^only_queued_jobs_can_be_cancelled$"):
                broker.cancel(CancelRequest(session=session, job_id="closing"))
        finally:
            backend.close_allowed.set()
        await broker.queue.join()
        assert not backend.hardware_connected and backend.active == 0
        assert broker.job("closing")["state"] == "succeeded"
        approve_image(broker, "closing")
        assert broker.read_capture("closing", PRINCIPAL).png


async def test_capture_unconfirmed_close_quarantines_pixels_and_preserves_device_lock(
    tmp_path: Path,
) -> None:
    class UnclosedCapture(MockBackend):
        ownership_uncertain = False

        async def execute(
            self, plan: dict[str, Any], stage: Callable[[str], None]
        ) -> BackendResult:
            result = await super().execute(plan, stage)
            assert isinstance(result, BackendResult)
            # Model a driver returning pixels without establishing physical closure.
            self.active = 1
            self.hardware_connected = True
            self.ownership_uncertain = True
            return result

    backend = UnclosedCapture(0.001)
    async with running(tmp_path, backend=backend) as broker:
        session = acquire(broker)
        enqueue(broker, session, "unclosed")
        enqueue(broker, session, "must-not-follow")
        try:
            await asyncio.wait_for(broker.queue.join(), timeout=2)
            assert broker.job("unclosed")["state"] == "needs_review"
            assert broker.job("must-not-follow")["state"] == "cancelled"
            unavailable(broker, "unclosed")
            assert not broker.captures
            assert backend.calls == 1
            with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                broker.release(LeaseRequest(session=session))
            with pytest.raises(BrokerError, match="^device_close_unconfirmed$"):
                await broker.close()
            assert broker._device_lock.is_locked
            assert broker.store is not None
        finally:
            # These flags belong to this synthetic driver; there is no real port.
            backend.active = 0
            backend.hardware_connected = False
            backend.ownership_uncertain = False
            broker.active_job = None


async def test_capture_shutdown_waits_for_active_close_and_cancels_queued_work(
    tmp_path: Path,
) -> None:
    backend = ClosingCapture()
    async with running(tmp_path, backend=backend) as broker:
        session = acquire(broker)
        backend.close_allowed.set()
        enqueue(broker, session, "completed")
        await broker.queue.join()
        approve_image(broker, "completed")
        image = broker.read_capture("completed", PRINCIPAL)
        retained = weakref.ref(image)
        del image
        backend.close_allowed.clear()
        backend.pixels_ready.clear()
        enqueue(broker, session, "active")
        await asyncio.wait_for(backend.pixels_ready.wait(), timeout=2)
        enqueue(broker, session, "queued")
        closing = asyncio.create_task(broker.close())
        try:
            async with asyncio.timeout(2):
                while broker.job("queued")["state"] != "cancelled":
                    await asyncio.sleep(0)
            assert not closing.done()
            assert broker._device_lock.is_locked
            assert backend.active == 1
            assert broker.job("queued")["state"] == "cancelled"
            unavailable(broker, "active")
            unavailable(broker, "queued")
            unavailable(broker, "completed")
            gc.collect()
            assert retained() is None
        finally:
            backend.close_allowed.set()
            await asyncio.wait_for(closing, timeout=2)
        assert not broker.captures
        assert not broker._device_lock.is_locked
        assert backend.calls == 2


async def test_capture_cancelled_queue_never_runs_or_replays(tmp_path: Path) -> None:
    backend = ClosingCapture()
    async with running(tmp_path, backend=backend, max_queue=1) as broker:
        session = acquire(broker)
        enqueue(broker, session, "active")
        await asyncio.wait_for(backend.pixels_ready.wait(), timeout=2)
        queued = enqueue(broker, session, "cancelled")
        try:
            overflow = prepare(broker, session, "overflow")
            with pytest.raises(BrokerError, match="^queue_full$"):
                broker.execute(ExecuteRequest(session=session, plan_hash=overflow["plan_hash"]))
            assert broker.cancel(CancelRequest(session=session, job_id="cancelled"))["state"] == (
                "cancelled"
            )
            assert broker.execute(queued)["deduplicated"]
            unavailable(broker, "cancelled")
        finally:
            backend.close_allowed.set()
        await broker.queue.join()
        assert broker.execute(queued)["state"] == "cancelled"
        assert broker.backend.calls == 1
        unavailable(broker, "cancelled")
        unavailable(broker, "overflow")


async def test_capture_restart_retains_history_but_never_pixels_or_pending_replay(
    tmp_path: Path,
) -> None:
    backend = MockBackend(0.001)
    restart_path = tmp_path / "restarted"
    (restart_path / "state").mkdir(parents=True)
    async with running(tmp_path / "original", backend=backend) as broker:
        session = acquire(broker)
        enqueue(broker, session, "completed")
        await broker.queue.join()
        backend.gate = asyncio.Event()
        enqueue(broker, session, "running")
        await asyncio.sleep(0)
        assert broker.job("running")["state"] == "running"
        enqueue(broker, session, "queued")
        snapshot = Store(restart_path / "state" / "history.sqlite3")
        try:
            broker._store().db.backup(snapshot.db)
        finally:
            snapshot.close()
    async with running(restart_path) as restarted:
        assert restarted.job("completed")["state"] == "succeeded"
        for job_id in ("running", "queued"):
            assert restarted.job(job_id)["state"] == "needs_review"
            assert restarted.job(job_id)["result"] == "broker_restart_no_replay"
        for job_id in ("completed", "running", "queued"):
            unavailable(restarted, job_id)
        await asyncio.sleep(0)
        assert restarted.backend.calls == 0
        assert restarted.queue.empty()


async def test_capture_pixels_never_enter_history_logs_or_files(tmp_path: Path) -> None:
    async with running(tmp_path) as broker:
        session = acquire(broker)
        enqueue(broker, session, "private-image")
        await broker.queue.join()
        approve_image(broker, "private-image")
        image = broker.read_capture("private-image", PRINCIPAL)
        visible = json.dumps(
            [broker.status(), broker.job("private-image"), broker.read_logs(LogRequest())]
        )
        history = "\n".join(broker._store().db.iterdump())
        for forbidden in (
            base64.b64encode(image.png).decode("ascii"),
            image.png.hex(),
            session,
            PRINCIPAL,
            "CAPTURE_BEGIN",
            "PIX 0 ",
        ):
            assert forbidden not in visible
            assert forbidden not in history
        files = [path for path in broker.settings.state_dir.rglob("*") if path.is_file()]
        assert {path.suffix for path in files} <= {
            ".sqlite3",
            ".sqlite3-wal",
            ".sqlite3-shm",
            ".lockfile",
        }
        # Windows prevents reading an actively byte-locked lockfile. Its tiny
        # coordination record cannot contain the complete image either.
        assert all(
            path.stat().st_size <= 1 if path.suffix == ".lockfile"
            else image.png not in path.read_bytes()
            for path in files
        )
        assert image.png.hex() not in repr(broker.captures)
