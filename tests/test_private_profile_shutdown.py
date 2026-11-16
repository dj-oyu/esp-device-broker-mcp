"""Broker close ordering fixture; native/session lifetime is tested separately."""

import asyncio

import pytest
from test_profile_jobs import prepare, register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.models import ExecuteRequest


class Adapter:
    def __init__(self, broker):
        self.broker = broker
        self.store = broker._store()
        self.events = []
        self.unknown = False

    def require_store(self, store):
        assert store is self.store

    def quiesce(self):
        self.events.append("quiesce")
        self.broker.firmware_build.request_stop()

    def verify_joined(self):
        assert self.broker.profile_jobs.worker is None
        self.store.db.execute("SELECT 1")
        self.events.append("verify")
        if self.unknown:
            raise ValueError("fixture child unknown")

    def close(self):
        self.verify_joined()
        self.events.append("close")
        self.store.close()


async def test_cancelled_close_shields_worker_then_retry_drains_queue(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    jobs = broker.profile_jobs
    adapter = Adapter(broker)
    broker.private_runtime_session = adapter
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def build(plan, stage, *, defer_publish):
        calls.append(plan["job_id"])
        started.set()
        await release.wait()
        broker.firmware_build._check_cancelled()
        pytest.fail("shutdown build succeeded")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    key = service.keys["second"].metadata
    for name in ("running", "queued"):
        plan = prepare(service, name)
        jobs.execute(
            ExecuteRequest(plan_hash=plan["plan_hash"]), key.principal_id, key.origin.value
        )
    await started.wait()
    worker = jobs.worker
    closing = asyncio.create_task(jobs.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert worker is not None and not worker.cancelled() and not worker.done()
    assert adapter.events == ["quiesce"]
    with pytest.raises(ValueError, match="broker_closing"):
        jobs.execute(ExecuteRequest(plan_hash="a" * 64), key.principal_id, key.origin.value)
    release.set()
    await jobs.close()
    assert calls == ["running"]
    assert broker.job("queued")["state"] == "cancelled"
    assert jobs.worker is None and jobs.queue.qsize() == 0
    assert "close" not in adapter.events
    adapter.store.db.execute("SELECT 1")  # Profile shutdown never closes Store.
    await broker.close()
    assert adapter.events[-1] == "close" and broker.store is None


async def test_unknown_child_retry_verifies_again_without_duplicate_sentinel(registry_service):
    broker = registry_service.resident
    adapter = Adapter(broker)
    broker.private_runtime_session = adapter
    adapter.unknown = True
    with pytest.raises(ValueError, match="child unknown"):
        await broker.close()
    task = broker.profile_jobs._close_task
    assert broker.store is adapter.store and "close" not in adapter.events
    assert broker.profile_jobs.worker is None
    adapter.unknown = False  # Synthetic reconciliation, no actual child in fixture.
    await broker.close()
    assert broker.profile_jobs._close_task is task
    assert broker.store is None and adapter.events[-1] == "close"


async def test_default_adapter_remains_none(registry_service):
    broker = registry_service.resident
    assert broker.private_runtime_session is None
    await broker.close()
    assert broker.store is None


async def test_profile_adapter_change_during_join_is_rejected(registry_service, monkeypatch):
    broker = registry_service.resident
    adapter = Adapter(broker)
    broker.private_runtime_session = adapter
    entered, release = asyncio.Event(), asyncio.Event()
    original = broker.profile_jobs.source_reads.close

    async def delayed():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(broker.profile_jobs.source_reads, "close", delayed)
    closing = asyncio.create_task(broker.profile_jobs.close())
    await entered.wait()
    broker.private_runtime_session = None
    release.set()
    with pytest.raises(ValueError, match="ownership_changed"):
        await closing
    assert "verify" not in adapter.events and "close" not in adapter.events
    adapter.store.db.execute("SELECT 1")
    with pytest.raises(ValueError, match="ownership_changed"):
        await broker.profile_jobs.close()
    broker.private_runtime_session = adapter
    # An explicit retry with the captured identity restored resumes this known fault.
    await broker.close()


async def test_broker_adapter_change_during_await_never_falls_back_to_store_close(
    registry_service, monkeypatch
):
    broker = registry_service.resident
    adapter = Adapter(broker)
    broker.private_runtime_session = adapter
    original = broker.home_recovery.close

    async def changed():
        await original()
        broker.private_runtime_session = None

    monkeypatch.setattr(broker.home_recovery, "close", changed)
    with pytest.raises(ValueError, match="ownership_changed"):
        await broker.close()
    assert broker.store is adapter.store and "close" not in adapter.events
    adapter.store.db.execute("SELECT 1")
    with pytest.raises(ValueError, match="ownership_changed"):
        await broker.close()  # Retry must not capture None and fall back to ordinary close.
    monkeypatch.setattr(broker.home_recovery, "close", original)
    broker.private_runtime_session = adapter
    await broker.close()


async def test_closed_queue_never_reads_or_cancels_replacement_store(
    registry_service, tmp_path, monkeypatch
):
    from cardputer_mcp.store import Store

    broker = registry_service.resident
    jobs = broker.profile_jobs
    original = broker._store()
    alternate = Store(tmp_path / "alternate-generated.db")
    calls = []
    monkeypatch.setattr(alternate, "job", lambda job: calls.append(job))
    jobs._closed = True
    jobs._close_identity = (None, original)
    broker.store = alternate
    try:
        jobs.queue.put_nowait("same-named-generated-job")
        await jobs.queue.join()
        assert calls == [] and jobs.uncertain
    finally:
        broker.store = original
        alternate.close()
    # No real uncertain process exists; clear only this fixture's injected mismatch.
    jobs.uncertain = False
    await broker.close()


async def test_post_sentinel_identity_fault_retry_never_enqueues_twice(
    registry_service, monkeypatch
):
    broker = registry_service.resident
    jobs = broker.profile_jobs
    adapter = Adapter(broker)
    broker.private_runtime_session = adapter
    original = jobs.queue.put
    sentinels = []

    async def put(value):
        await original(value)
        if value is None:
            sentinels.append(value)
            broker.private_runtime_session = None

    monkeypatch.setattr(jobs.queue, "put", put)
    with pytest.raises(ValueError, match="ownership_changed"):
        await jobs.close()
    assert jobs._sentinel_enqueued and sentinels == [None]
    broker.private_runtime_session = adapter
    await jobs.close()
    assert sentinels == [None] and jobs.worker is None
    await broker.close()


async def test_other_drain_failure_never_retries(registry_service, monkeypatch):
    broker = registry_service.resident
    jobs = broker.profile_jobs
    original = jobs.source_reads.close
    calls = []

    async def fail():
        calls.append(1)
        raise RuntimeError("fixture unexpected error")

    monkeypatch.setattr(jobs.source_reads, "close", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="unexpected error"):
            await jobs.close()
    assert calls == [1]
    # Dispose the synthetic failure for fixture teardown, not a production recovery API.
    monkeypatch.setattr(jobs.source_reads, "close", original)
    jobs._close_task = None
    await broker.close()


async def test_cancelled_broker_close_retains_store_and_resumes_same_drain(
    registry_service, monkeypatch
):
    broker = registry_service.resident
    adapter = Adapter(broker)
    broker.private_runtime_session = adapter
    entered, release = asyncio.Event(), asyncio.Event()
    original = broker.profile_jobs.source_reads.close

    async def delayed():
        entered.set()
        await release.wait()
        await original()

    monkeypatch.setattr(broker.profile_jobs.source_reads, "close", delayed)
    closing = asyncio.create_task(broker.close())
    await entered.wait()
    drain = broker.profile_jobs._close_task
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert broker.store is adapter.store and "close" not in adapter.events
    assert drain is not None and not drain.cancelled()
    release.set()
    await broker.close()
    assert broker.profile_jobs._close_task is drain
    assert adapter.events.count("close") == 1 and broker.store is None
