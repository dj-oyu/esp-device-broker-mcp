"""Pause real reclamation after its final resident-loop evidence read; no device I/O."""

import asyncio
import threading

import pytest
from test_flash_policy import env as env

from cardputer_mcp import source_payloads
from cardputer_mcp.flash_binding import prepare_binding
from cardputer_mcp.models import AcquireRequest, LeaseRequest, Origin, PrepareRequest
from cardputer_mcp.source_retention import retention_evidence
from cardputer_mcp.source_snapshot import SourceError


@pytest.mark.parametrize("env", ["prepare-only"], indirect=True)
async def test_flash_publication_cannot_cross_final_reclamation_snapshot(env, monkeypatch):
    broker, snapshots = env.broker, env.source.snapshots
    probe = env.prepare("valid-before-race", build_job_id="build-one")
    assert prepare_binding(broker, probe["plan"]).fetch_receipt == env.receipt
    broker.release(LeaseRequest(session=env.session))
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="alice")["session"]
    loop = asyncio.get_running_loop()
    entered, release = threading.Event(), threading.Event()
    calls = 0
    original = source_payloads._fresh

    async def evidence():
        return retention_evidence(env.store, "fixture", "alice", broker.epoch)

    snapshots.retention = lambda: asyncio.run_coroutine_threadsafe(evidence(), loop).result(5)
    source, metadata = snapshots.resolve("fixture", env.receipt, "alice")
    receipt_bytes = (source.parent / "receipt.json").read_bytes()
    writes = env.driver.writes

    def paused(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 2:
            # The second read has already returned its immutable snapshot.
            assert env.receipt not in result["protected"]
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(source_payloads, "_fresh", paused)

    def reclaim():
        with snapshots.cache_guard("fixture", write=True):
            return source_payloads._reclaim(
                snapshots, {"project_id": "fixture"}, env.receipt, metadata, lambda: True
            )

    task = asyncio.create_task(asyncio.to_thread(reclaim))
    assert await asyncio.to_thread(entered.wait, 5)
    prepared = None
    refusal = None
    try:
        try:
            prepared = broker.prepare(
                PrepareRequest(
                    session=token,
                    job_id="raced-flash",
                    test="firmware_flash",
                    device_id="fixture",
                    build_job_id="build-one",
                )
            )
        except SourceError as failure:
            refusal = failure.code
    finally:
        release.set()
        assert await asyncio.wait_for(task, 5) > 0
    assert refusal == "source_fetch_busy", (
        f"published={prepared is not None}, payload_deleted={not source.exists()}"
    )
    assert prepared is None
    assert (
        env.store.db.execute(
            "SELECT 1 FROM plans WHERE json_extract(payload,'$.job_id')='raced-flash'"
        ).fetchone()
        is None
    )
    assert not source.exists()
    assert (source.parent / "receipt.json").read_bytes() == receipt_bytes
    assert env.driver.writes == writes == 0
    assert env.store.firmware_current("fixture") is None
    # The refusal is transient: retained artifacts still prepare after cleanup,
    # preserving the established ability to prepare without resident source.
    retry = broker.prepare(
        PrepareRequest(
            session=token,
            job_id="retry-flash",
            test="firmware_flash",
            device_id="fixture",
            build_job_id="build-one",
        )
    )
    assert retry["plan"]["build_job_id"] == "build-one"


@pytest.mark.parametrize("env", ["prepare-only"], indirect=True)
async def test_flash_publication_before_reclamation_preserves_source(env):
    broker, snapshots = env.broker, env.source.snapshots
    prepared = env.prepare("published-first", build_job_id="build-one")
    assert prepare_binding(broker, prepared["plan"]).fetch_receipt == env.receipt
    source, metadata = snapshots.resolve("fixture", env.receipt, "alice")
    original = (source.parent / "receipt.json").read_bytes()
    loop = asyncio.get_running_loop()

    async def evidence():
        return retention_evidence(env.store, "fixture", "alice", broker.epoch)

    snapshots.retention = lambda: asyncio.run_coroutine_threadsafe(evidence(), loop).result(5)

    def reclaim():
        with snapshots.cache_guard("fixture", write=True):
            source_payloads._reclaim(
                snapshots, {"project_id": "fixture"}, env.receipt, metadata, lambda: True
            )

    with pytest.raises(SourceError, match="source_cache_protected"):
        await asyncio.wait_for(asyncio.to_thread(reclaim), 5)
    assert source.is_dir()
    assert (source.parent / "receipt.json").read_bytes() == original
    assert env.store.job("published-first") is None
    assert env.driver.writes == 0
