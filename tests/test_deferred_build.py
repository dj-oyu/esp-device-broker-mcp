"""A build that continues a source fetch waits for its parent instead of being refused."""

import asyncio

import pytest
from test_profile_jobs import register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.broker import BrokerError, digest_plan
from cardputer_mcp.models import ExecuteRequest, PrepareRequest
from cardputer_mcp.models import TestName as Action

RECEIPT = "a" * 64


async def stage(service, tmp_path, monkeypatch):
    register(service, tmp_path)
    broker = service.resident
    broker.profile_jobs.worker.cancel()
    await asyncio.sleep(0)
    monkeypatch.setattr(broker.firmware_source, "authorize_receipt", lambda *args: None)
    monkeypatch.setattr(
        broker.firmware_build,
        "prepare",
        lambda project, principal, source, mode=None, **_: {
            "project_id": project,
            "build_principal": principal,
            "source_receipt": source,
        },
    )
    monkeypatch.setattr(broker.firmware_build, "storage", lambda project: {})
    return broker, service.keys["second"].metadata


def parent_fetch(broker, owner, job_id="fetch-1", state="running"):
    plan = {
        "version": 2,
        "execution_context": "registered_project",
        "broker_epoch": broker.epoch,
        "job_id": job_id,
        "test": "firmware_fetch_source",
        "principal_id": owner.principal_id,
        "origin": owner.origin.value,
        "source_principal": owner.principal_id,
        "project_id": "fixture",
    }
    digest = digest_plan(plan)
    store = broker._store()
    store.put_plan(digest, plan)
    store.add_job(job_id, digest)
    if state != "queued":
        store.set_job(job_id, state)
    return broker.issue_ticket(job_id)


def finish_parent(broker, job_id="fetch-1", state="succeeded"):
    diagnostic = {"source": {"source_receipt": RECEIPT}} if state == "succeeded" else None
    broker._store().set_job(job_id, state, state, diagnostic)


def defer(broker, owner, ticket, job_id="build-1"):
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id=job_id, test=Action.BUILD, project_id="fixture", after=ticket),
        owner.principal_id,
        owner.origin.value,
    )
    queued = broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), owner.principal_id, owner.origin.value
    )
    return prepared, queued


async def test_build_waits_then_runs_the_exact_fetched_source(
    registry_service, tmp_path, monkeypatch
):
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)
    ticket = parent_fetch(broker, owner)
    prepared, queued = defer(broker, owner, ticket)
    assert queued["state"] == "waiting_parent" and queued["deduplicated"] is False
    assert queued["continues"] == {"parent_job_id": "fetch-1"}
    assert broker._store().job("build-1") is None  # no queue position while waiting
    assert broker.tickets._row(ticket)["state"] == "consumed"
    again = broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), owner.principal_id, owner.origin.value
    )
    assert again["deduplicated"] is True and again["state"] == "waiting_parent"
    broker.maintenance_tick()
    assert broker._store().job("build-1") is None  # parent still running
    finish_parent(broker)
    broker.maintenance_tick()
    job = broker._store().job("build-1")
    assert job is not None and job["state"] == "queued"
    resolved = broker._store().plan(job["plan_hash"])
    assert resolved["source_receipt"] == RECEIPT and resolved["principal_id"] == owner.principal_id
    assert job["plan_hash"] != prepared["plan_hash"]
    row = broker.deferred.get("build-1")
    assert row["state"] == "promoted" and row["resolved_plan_hash"] == job["plan_hash"]
    assert broker.deferred_job("build-1", owner.principal_id) is None  # the job row answers now


@pytest.mark.parametrize("state", ["failed", "cancelled"])
async def test_failed_parent_cancels_the_waiting_build(
    registry_service, tmp_path, monkeypatch, state
):
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)
    ticket = parent_fetch(broker, owner)
    defer(broker, owner, ticket)
    finish_parent(broker, state=state)
    broker.maintenance_tick()
    view = broker.deferred_job("build-1", owner.principal_id)
    assert view["state"] == "cancelled" and view["result"] == "chain_broken"
    assert broker._store().job("build-1") is None


async def test_ticket_is_single_use_and_owner_bound(registry_service, tmp_path, monkeypatch):
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)
    other = registry_service.keys["first"].metadata
    ticket = parent_fetch(broker, owner)
    with pytest.raises(BrokerError, match="ticket_invalid"):
        defer(broker, other, ticket)
    defer(broker, owner, ticket, "build-1")
    with pytest.raises(BrokerError, match="ticket_consumed"):
        defer(broker, owner, ticket, "build-2")
    assert broker.deferred_job("build-1", other.principal_id) is None
    assert broker.cancel_deferred("build-1", other.principal_id) is None


async def test_waiting_build_can_be_cancelled_and_never_queues(
    registry_service, tmp_path, monkeypatch
):
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)
    ticket = parent_fetch(broker, owner)
    defer(broker, owner, ticket)
    cancelled = broker.cancel_deferred("build-1", owner.principal_id)
    assert cancelled["state"] == "cancelled" and cancelled["result"] == "client_cancelled_waiting"
    finish_parent(broker)
    broker.maintenance_tick()
    assert broker._store().job("build-1") is None


async def test_build_may_continue_a_chain_before_its_parent_finishes(
    registry_service, tmp_path, monkeypatch
):
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)
    ticket = parent_fetch(broker, owner)
    defer(broker, owner, ticket)
    child_ticket = broker.issue_ticket("build-1")  # a waiting job can name its own child
    broker.maintenance_tick()
    assert broker.tickets._row(child_ticket)["state"] == "issued"
    progress = broker.chain_progress("build-1")
    assert progress["chain_state"] == "waiting_parent" and progress["chain_position"] == 2
    assert progress["chain_open"] is True


async def test_restart_cancels_waiting_builds(registry_service, tmp_path, monkeypatch):
    broker, owner = await stage(registry_service, tmp_path, monkeypatch)
    ticket = parent_fetch(broker, owner)
    defer(broker, owner, ticket)
    broker._store().db.execute("UPDATE deferred_jobs SET epoch='old-epoch'")
    broker.deferred.cancel_stale()
    view = broker.deferred_job("build-1", owner.principal_id)
    assert view["state"] == "cancelled" and view["result"] == "broker_restart"
