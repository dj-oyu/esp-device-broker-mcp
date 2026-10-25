"""Local operator stop of a device job owned by another principal; fakes only, no hardware."""

import asyncio
import threading
from types import SimpleNamespace

import pytest
from test_operator_review import post, principal, unused_app
from test_pending_execution import prepare, running, submit

from cardputer_mcp import cancellation, operator_stop
from cardputer_mcp.broker import BrokerError, digest_plan
from cardputer_mcp.models import FLASH_TESTS, CancelRequest, Origin
from cardputer_mcp.operator_api import OperatorAPI


def audit(broker, job_id):
    return [
        tuple(row)
        for row in broker._store().db.execute(
            "SELECT principal_id,event FROM execution_audit WHERE job_id=? ORDER BY sequence",
            (job_id,),
        )
    ]


def synthetic(broker, job_id, test, *, state="running", session_id=None):
    """A device-lane job of the current lease (another client's) in `state`."""
    store = broker._store()
    plan = {
        "version": 1,
        "job_id": job_id,
        "test": test,
        "session_id": broker.session.id if session_id is None else session_id,
    }
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    store.add_job(job_id, digest)
    if state != "queued":
        store.set_job(job_id, state)
    return digest


def stop(broker, job_id, digest, epoch=None):
    return cancellation.cancel_job(
        broker,
        cancellation.OperatorDeviceJob(job_id, digest, epoch or broker.epoch, "local-operator"),
    )


async def test_operator_cancels_another_clients_queued_job_and_audits(tmp_path):
    async with running(tmp_path) as (broker, _):
        broker.backend.gate = asyncio.Event()
        submit(broker, prepare(broker, "active"))
        await asyncio.sleep(0)
        digest = prepare(broker, "queued")
        submit(broker, digest)
        lease = broker.session
        assert operator_stop.stop_available(broker, "queued")
        result = stop(broker, "queued", digest)
        assert result == {
            "broker_epoch": broker.epoch,
            "job_id": "queued",
            "plan_hash": digest,
            "state": "cancelled",
            "stop_requested": False,
            "requires_review": False,
            "stopped_by": "local_operator",
            "audit_recorded": True,
        }
        assert broker.job("queued")["result"] == "client_cancelled_queued"
        assert audit(broker, "queued")[-1] == ("local-operator", "operator_cancelled_queued")
        # The owner's lease and the running job are untouched.
        assert broker.session is lease and broker.job("active")["state"] == "running"
        broker.backend.gate.set()
        await broker.queue.join()
        assert broker.job("active")["state"] == "succeeded"


async def test_running_app_session_is_signalled_and_ownership_kept(tmp_path):
    async with running(tmp_path) as (broker, _):
        digest = synthetic(broker, "app-run", "app_session")
        run = SimpleNamespace(
            cancelled=threading.Event(), events=SimpleNamespace(expire=lambda: None)
        )
        broker.app_sessions.jobs["app-run"] = run
        lease = broker.session
        try:
            result = stop(broker, "app-run", digest)
        finally:
            broker.app_sessions.jobs.pop("app-run", None)
        assert run.cancelled.is_set()
        assert result["state"] == "running" and result["stop_requested"] is True
        assert result["stopped_by"] == "local_operator"
        # Signalled only: the job keeps the lease until its own confirmed close.
        assert broker.session is lease and not lease.revoked
        assert audit(broker, "app-run") == [("local-operator", "operator_stop_requested")]


@pytest.mark.parametrize("test", sorted(test.value for test in FLASH_TESTS))
async def test_running_flash_restore_backup_is_never_interrupted(tmp_path, test):
    async with running(tmp_path) as (broker, _):
        digest = synthetic(broker, "write-run", test)
        calls = []
        broker.firmware_flash.request_stop = lambda *args: calls.append(args) or True
        assert not operator_stop.stop_available(broker, "write-run")
        with pytest.raises(BrokerError, match="^operator_stop_refused_device_write$"):
            stop(broker, "write-run", digest)
        assert calls == [] and broker.job("write-run")["state"] == "running"
        assert audit(broker, "write-run") == []


async def test_running_kind_the_owner_cannot_stop_stays_refused(tmp_path):
    async with running(tmp_path) as (broker, _):
        digest = synthetic(broker, "hello-run", "hello")
        with pytest.raises(BrokerError, match="^operator_stop_not_supported$"):
            stop(broker, "hello-run", digest)
        assert broker.job("hello-run")["state"] == "running"


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("epoch", "stale_operator_snapshot"),
        ("hash", "stop_target_not_found"),
        ("missing", "stop_target_not_found"),
        ("finished", "job_not_active"),
        ("other_session", "operator_stop_not_available"),
        ("closing", "broker_closing"),
    ],
)
async def test_refusals_leave_the_job_unchanged(tmp_path, case, code):
    async with running(tmp_path) as (broker, _):
        digest = synthetic(
            broker,
            "app-run",
            "app_session",
            state="succeeded" if case == "finished" else "running",
            session_id="s" * 32 if case == "other_session" else None,
        )
        broker.closing = case == "closing"
        job_id = "missing" if case == "missing" else "app-run"
        with pytest.raises(BrokerError, match=f"^{code}$"):
            stop(
                broker,
                job_id,
                "b" * 64 if case == "hash" else digest,
                "0" * 32 if case == "epoch" else None,
            )
        broker.closing = False
        assert audit(broker, "app-run") == []


async def test_mcp_cancel_for_other_principals_stays_lease_bound(tmp_path):
    async with running(tmp_path) as (broker, _):
        synthetic(broker, "app-run", "app_session")
        with pytest.raises(BrokerError, match="^invalid_session$"):
            broker.cancel(CancelRequest(session="c" * 64, job_id="app-run"))


# --- route ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor", "status", "error"),
    [
        (None, 401, "authentication_required"),
        (principal(origin=Origin.REMOTE), 403, "local_operator_required"),
        (principal(scopes=("read",)), 403, "local_operator_required"),
    ],
)
async def test_route_refuses_remote_read_only_and_anonymous(tmp_path, actor, status, error):
    async with running(tmp_path) as (broker, _):
        digest = synthetic(broker, "app-run", "app_session")
        run = SimpleNamespace(
            cancelled=threading.Event(), events=SimpleNamespace(expire=lambda: None)
        )
        broker.app_sessions.jobs["app-run"] = run
        try:
            api = OperatorAPI(unused_app, broker, lambda _header: actor)
            body = {"job_id": "app-run", "plan_hash": digest, "broker_epoch": broker.epoch}
            assert await post(api, "/operator/job-stop", body) == (
                status,
                {"ok": False, "error": error},
            )
        finally:
            broker.app_sessions.jobs.pop("app-run", None)
        assert not run.cancelled.is_set() and audit(broker, "app-run") == []


async def test_local_control_route_and_timeline_flag(tmp_path):
    async with running(tmp_path) as (broker, _):
        broker.backend.gate = asyncio.Event()
        submit(broker, prepare(broker, "active"))
        await asyncio.sleep(0)
        digest = prepare(broker, "queued")
        submit(broker, digest)
        api = OperatorAPI(unused_app, broker, lambda _header: principal())
        cards = {card["job_id"]: card for card in api._timeline(principal())["cards"]}
        assert cards["queued"]["stop_available"] is True
        assert cards["active"]["stop_available"] is False  # running hello: not stoppable
        body = {"job_id": "queued", "plan_hash": digest, "broker_epoch": broker.epoch}
        status, reply = await post(api, "/operator/job-stop", body)
        assert status == 200 and reply["ok"] and reply["state"] == "cancelled"
        assert reply["stopped_by"] == "local_operator"
        status, reply = await post(api, "/operator/job-stop", body)
        assert reply == {"ok": False, "error": "job_not_active"}
        status, reply = await post(api, "/operator/job-stop", {"job_id": "queued"})
        assert (status, reply) == (400, {"ok": False, "error": "invalid_request"})
        broker.backend.gate.set()
        await broker.queue.join()
