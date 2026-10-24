"""Historical outcomes cannot authorize replay or impersonate live resources."""

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from test_broker import broker as broker
from test_broker import plan
from test_profile_jobs import register

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.models import (
    AcquireRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Action


def history(broker):
    store = broker._store()
    # An unknown old plan must remain diagnostic too; no classification bypass.
    store.add_job("old-capture", "a" * 64)
    store.set_stage("old-capture", "serial_close")
    store.set_job("old-capture", "needs_review", "serial_capture_timeout")
    store.add_job("old-flash", "b" * 64)
    store.set_stage("old-flash", "flash_readback_verify")
    store.set_job("old-flash", "needs_review", "flash_authority_changed_needs_review")
    store.put_request("old-owner", None, 30, state="needs_review")
    return [store.job("old-capture"), store.job("old-flash"), store.request("old-owner")]


@pytest.mark.parametrize("boundary", ["acquire", "execute", "running"])
async def test_new_explicit_job_runs_without_changing_or_replaying_history(broker, boundary):
    before = None
    if boundary == "acquire":
        before = history(broker)
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    digest = plan(broker, token, "new-job")
    assert broker.backend.calls == 0
    if boundary == "execute":
        before = history(broker)
    if boundary == "running":
        broker.backend.gate = asyncio.Event()
    execute = ExecuteRequest(session=token, plan_hash=digest)
    broker.execute(execute)
    if boundary == "running":
        await asyncio.sleep(0)
        assert broker.active_job == "new-job"
        before = history(broker)
        broker.backend.gate.set()
    await asyncio.wait_for(broker.queue.join(), 2)
    assert broker.job("new-job")["state"] == "succeeded"
    assert broker.execute(execute)["deduplicated"]
    assert broker.backend.calls == 1
    assert before == [
        broker.job("old-capture"),
        broker.job("old-flash"),
        broker._store().request("old-owner"),
    ]
    assert broker.status()["needs_review"] is True
    assert broker.status()["execution_uncertain"] is False
    broker.release(LeaseRequest(session=token))


@pytest.mark.parametrize(
    "resource,attribute,value",
    [
        ("backend", "active", 1),
        ("process", "active", True),
        ("build", "output_uncertain", True),
        ("broker", "execution_uncertain", True),
    ],
)
async def test_current_resource_hazards_still_prevent_new_ownership(
    broker, monkeypatch, resource, attribute, value
):
    before = history(broker)
    target = {
        "backend": broker.backend,
        "process": broker.firmware_build.process,
        "build": broker.firmware_build,
        "broker": broker,
    }[resource]
    with monkeypatch.context() as change:
        change.setattr(target, attribute, value, raising=False)
        with pytest.raises(BrokerError, match="device_busy|recovery_review_required"):
            broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")
        assert broker.session is None and broker.backend.calls == 0
    assert broker.job("old-capture") == before[0]


async def test_current_uncertainty_after_preparation_or_enqueue_blocks_io(broker, monkeypatch):
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    digest = plan(broker, token, "not-started")
    with monkeypatch.context() as change:
        change.setattr(broker.firmware_build.process, "uncertain", True)
        with pytest.raises(BrokerError, match="recovery_review_required"):
            broker.prepare(PrepareRequest(session=token, job_id="not-prepared"))
        with pytest.raises(BrokerError, match="recovery_review_required"):
            broker.execute(ExecuteRequest(session=token, plan_hash=digest))
        assert broker._store().job("not-started") is None
    broker.execute(ExecuteRequest(session=token, plan_hash=digest))
    with monkeypatch.context() as change:
        change.setattr(broker.backend, "ownership_uncertain", True, raising=False)
        await asyncio.wait_for(broker.queue.join(), 2)
    assert broker.job("not-started")["state"] == "cancelled"
    assert broker.backend.calls == 0


@pytest.mark.parametrize("hazard", ["process", "state"])
async def test_profile_current_uncertainty_remains_blocking(broker, tmp_path, monkeypatch, hazard):
    register(SimpleNamespace(resident=broker), tmp_path)
    candidate = PrepareRequest(job_id="new-build", test=Action.BUILD, project_id="fixture")
    prepared = broker.profile_jobs.prepare(candidate, "owner", Origin.REMOTE.value)
    target, attribute = {
        "process": (broker.firmware_build.process, "uncertain"),
        "state": (broker.profile_jobs, "state_uncertain"),
    }[hazard]
    with monkeypatch.context() as change:
        change.setattr(target, attribute, True)
        with pytest.raises(BrokerError, match="recovery_review_required"):
            broker.profile_jobs.prepare(candidate, "owner", Origin.REMOTE.value)
        with pytest.raises(BrokerError, match="recovery_review_required"):
            broker.profile_jobs.execute(
                ExecuteRequest(plan_hash=prepared["plan_hash"]), "owner", Origin.REMOTE.value
            )
        assert broker._store().job("new-build") is None
        assert broker.profile_jobs.queue.empty()


async def test_worker_state_failure_latches_current_uncertainty(broker, monkeypatch):
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    digest = plan(broker, token, "state-failure")
    broker.execute(ExecuteRequest(session=token, plan_hash=digest))

    def unreadable(_):
        raise sqlite3.DatabaseError("synthetic private error")

    with monkeypatch.context() as change:
        change.setattr(broker._store(), "plan", unreadable)
        await asyncio.wait_for(broker.queue.join(), 2)
    assert broker.execution_uncertain is True
    assert broker.backend.calls == 0
    with pytest.raises(BrokerError, match="recovery_review_required"):
        broker.prepare(PrepareRequest(session=token, job_id="cannot-follow"))


async def test_profile_commit_failure_latches_current_uncertainty(broker, tmp_path, monkeypatch):
    register(SimpleNamespace(resident=broker), tmp_path)

    async def build(prepared, stage, *, defer_publish):
        return "build_completed_no_device_write"

    def failed_commit(prepared, live):
        raise sqlite3.DatabaseError("synthetic private commit error")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    monkeypatch.setattr(broker.firmware_build, "commit_deferred", failed_commit)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="commit-failure", test=Action.BUILD, project_id="fixture"),
        "owner",
        Origin.REMOTE.value,
    )
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), "owner", Origin.REMOTE.value
    )
    await asyncio.wait_for(broker.profile_jobs.queue.join(), 2)
    assert broker.job("commit-failure")["state"] == "needs_review"
    assert broker.profile_jobs.state_uncertain is True
    with pytest.raises(BrokerError, match="recovery_review_required"):
        broker.profile_jobs.prepare(
            PrepareRequest(job_id="cannot-follow", test=Action.BUILD, project_id="fixture"),
            "owner",
            Origin.REMOTE.value,
        )


async def test_profile_publication_rechecks_uncertainty_after_auth_callback(
    broker, tmp_path, monkeypatch
):
    register(SimpleNamespace(resident=broker), tmp_path)
    published = []

    async def build(prepared, stage, *, defer_publish):
        return "build_completed_no_device_write"

    def commit(prepared, live):
        def changed_resource(*_):
            broker.firmware_build.process.uncertain = True
            return True

        monkeypatch.setattr(broker, "principal_valid", changed_resource)
        if not live():
            raise ValueError("build_final_commit_not_authorized")
        published.append(prepared["job_id"])

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    monkeypatch.setattr(broker.firmware_build, "commit_deferred", commit)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="publication", test=Action.BUILD, project_id="fixture"),
        "owner",
        Origin.REMOTE.value,
    )
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), "owner", Origin.REMOTE.value
    )
    try:
        await asyncio.wait_for(broker.profile_jobs.queue.join(), 2)
        assert broker.job("publication")["state"] == "needs_review"
        assert broker.profile_jobs.state_uncertain
        assert published == []
    finally:
        broker.firmware_build.process.uncertain = False
