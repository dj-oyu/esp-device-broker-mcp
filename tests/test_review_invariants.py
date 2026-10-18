"""Independent safety/recovery regressions. All devices and state are synthetic."""

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.models import (
    AcquireRequest,
    ExecuteRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import (
    TestName as CardputerTestName,
)
from cardputer_mcp.store import Store

# A bounded harness guard for real SQLite/OS-lock IO, not a broker lease or
# latency assertion. Authorization expiry is controlled by Clock below.
COMPLETION_TIMEOUT = 5


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class ObservedBackend(MockBackend):
    def __init__(self, stage_seconds: float) -> None:
        super().__init__(stage_seconds)
        self.started = asyncio.Event()

    async def run(self, test: CardputerTestName, stage: Callable[[str], None]) -> None:
        self.started.set()
        await super().run(test, stage)


async def wait_active(broker: Broker) -> None:
    assert isinstance(broker.backend, ObservedBackend)
    await asyncio.wait_for(broker.backend.started.wait(), timeout=COMPLETION_TIMEOUT)
    assert broker.active_job is not None


def prepare(broker: Broker, token: str, job_id: str) -> ExecuteRequest:
    result = broker.prepare(
        PrepareRequest(
            session=token,
            job_id=job_id,
            test=CardputerTestName.WORKFLOW,
        )
    )
    return ExecuteRequest(session=token, plan_hash=result["plan_hash"])


@pytest.mark.asyncio
async def test_review_expired_lease_does_not_eat_shutdown_sentinel(tmp_path: Path) -> None:
    clock = Clock()
    backend = ObservedBackend(0.001)
    backend.gate = asyncio.Event()
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        backend=backend,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    token = broker.acquire(
        AcquireRequest(owner=Origin.LOCAL, lease_seconds=1), principal_id="synthetic-client"
    )["session"]
    broker.execute(prepare(broker, token, "shutdown"))
    await wait_active(broker)
    close_task = asyncio.create_task(broker.close())
    await asyncio.sleep(0)
    clock.now += 2
    backend.gate.set()
    try:
        await asyncio.wait_for(asyncio.shield(close_task), timeout=COMPLETION_TIMEOUT)
        assert clock.now == 102
        assert broker.worker is not None and broker.worker.done()
        assert broker.queue.empty() and broker.active_job is None
        assert not broker._state_lock.is_locked and not broker._device_lock.is_locked
        assert backend.calls == backend.max_active == 1
    finally:
        # Repair only the synthetic test's teardown if the sentinel was lost.
        if not close_task.done():
            broker.queue.put_nowait(None)
            await asyncio.wait_for(close_task, timeout=COMPLETION_TIMEOUT)


@pytest.mark.asyncio
async def test_review_expiry_cancels_pending_but_pins_active_owner(tmp_path: Path) -> None:
    clock = Clock()
    backend = ObservedBackend(0.001)
    backend.gate = asyncio.Event()
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        backend=backend,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    try:
        token = broker.acquire(
            AcquireRequest(owner=Origin.LOCAL, lease_seconds=1), principal_id="synthetic-client"
        )["session"]
        broker.execute(prepare(broker, token, "active"))
        await wait_active(broker)
        broker.execute(prepare(broker, token, "pending"))
        clock.now += 2
        assert broker.status()["active_job"] == "active"
        assert broker.job("pending")["state"] == "cancelled"
        with pytest.raises(BrokerError, match="device_busy"):
            broker.acquire(AcquireRequest(owner=Origin.REMOTE))
        backend.gate.set()
        await asyncio.wait_for(broker.queue.join(), timeout=COMPLETION_TIMEOUT)
        assert backend.calls == 1
        assert backend.max_active == 1
        assert broker.status()["owner"] is None
        assert broker.job("active")["state"] == "succeeded"
    finally:
        backend.gate.set()
        await broker.close()


@pytest.mark.asyncio
async def test_review_explicit_retries_do_not_duplicate_work(tmp_path: Path) -> None:
    backend = ObservedBackend(0.001)
    broker = Broker(
        Settings(state_dir=tmp_path / "state"),
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    try:
        token = broker.acquire(
            AcquireRequest(owner=Origin.LOCAL), principal_id="synthetic-client"
        )["session"]
        request = prepare(broker, token, "approved")
        assert broker.store is not None
        first = broker.execute(request)
        second = broker.execute(request)
        assert first["deduplicated"] is False
        assert second["deduplicated"] is True
        assert broker.store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
        await asyncio.wait_for(broker.queue.join(), timeout=COMPLETION_TIMEOUT)
        assert backend.calls == 1
        assert broker.queue.empty() and broker.active_job is None
        assert broker.job("approved")["state"] == "succeeded"
        assert broker.execute(request)["deduplicated"] is True
        assert backend.calls == 1
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_review_recovery_never_replays_and_blocks_acquire(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "history.sqlite3")
    store.add_job("queued", "a" * 64)
    store.add_job("running", "b" * 64)
    store.set_job("running", "running")
    store.close()
    backend = ObservedBackend(0.001)
    broker = Broker(
        Settings(state_dir=state, allow_mock_execution=True),
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    try:
        assert broker.job("queued")["state"] == "cancelled"
        assert broker.job("running")["state"] == "needs_review"
        assert broker.status()["needs_review"] is True
        with pytest.raises(BrokerError, match="recovery_review_required"):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="synthetic-client")
        await asyncio.sleep(0)
        assert backend.calls == 0
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_review_different_state_dirs_share_device_lock(tmp_path: Path) -> None:
    lock = tmp_path / "device.lockfile"
    first = Broker(Settings(state_dir=tmp_path / "one"), lock_path=lock)
    second = Broker(Settings(state_dir=tmp_path / "two"), lock_path=lock)
    await first.start()
    try:
        with pytest.raises(BrokerError, match="broker_already_running"):
            await second.start()
        assert first.status()["backend"] == "mock"
    finally:
        await first.close()
    await second.start()
    await second.close()


@pytest.mark.asyncio
async def test_review_mutated_plan_is_rejected(tmp_path: Path) -> None:
    broker = Broker(
        Settings(state_dir=tmp_path / "state", allow_mock_execution=True),
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    try:
        token = broker.acquire(
            AcquireRequest(owner=Origin.LOCAL), principal_id="synthetic-client"
        )["session"]
        request = prepare(broker, token, "integrity")
        assert broker.store is not None
        payload = broker.store.plan(request.plan_hash)
        assert payload is not None
        payload["test"] = "diagnostics"
        broker.store.db.execute(
            "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(payload), request.plan_hash)
        )
        broker.store.db.commit()
        with pytest.raises(BrokerError, match="plan_not_found_or_changed"):
            broker.execute(request)
        assert broker.queue.empty()
    finally:
        await broker.close()


@pytest.mark.asyncio
async def test_review_same_os_user_with_different_home_keeps_one_device_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HOME is process configuration, not an OS-user identity. Keep all lockfiles
    # inside the test directory while exercising the real default lock resolver.
    monkeypatch.setattr("cardputer_mcp.broker.tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home_one"))
    first = Broker(Settings(state_dir=tmp_path / "one"))
    monkeypatch.setenv("HOME", str(tmp_path / "home_two"))
    second = Broker(Settings(state_dir=tmp_path / "two"))
    await first.start()
    try:
        with pytest.raises(BrokerError, match="broker_already_running"):
            await second.start()
    finally:
        await second.close()
        await first.close()
