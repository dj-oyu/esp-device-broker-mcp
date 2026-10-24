import asyncio
import json

import pytest
from pydantic import ValidationError

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker, BrokerError
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
from cardputer_mcp.models import TestName as Workflow
from cardputer_mcp.operator import acknowledge_recovery, approve_plan
from cardputer_mcp.ports import DeviceSelector, PortInfo, resolve_port


@pytest.fixture
async def broker(tmp_path):
    instance = Broker(
        Settings(state_dir=tmp_path / "state", mock_stage_seconds=0.001),
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await instance.start()
    yield instance
    await instance.close()


def plan(broker, token, job="job-one", test=Workflow.DIAGNOSTICS):
    return broker.prepare(PrepareRequest(session=token, job_id=job, test=test))["plan_hash"]


async def test_explicit_authenticated_execute_is_atomic_and_deduplicated(broker):
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    digest = plan(broker, token)
    request = ExecuteRequest(session=token, plan_hash=digest, wait_for_approval=True)
    with pytest.raises(ValueError, match="approval_interface_retired"):
        approve_plan(broker.settings.state_dir, digest, lambda _: pytest.fail("must not prompt"))
    first = broker.execute(request)
    second = broker.execute(request)
    assert first["state"] == "queued"
    assert first["id"] == second["id"]
    assert second["deduplicated"]
    assert broker._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    await broker.queue.join()
    assert broker.job(first["id"])["state"] == "succeeded"
    assert broker.backend.calls == 1


async def test_two_clients_one_complete_workflow(broker):
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    with pytest.raises(BrokerError, match="device_busy"):
        broker.acquire(AcquireRequest(owner=Origin.REMOTE))
    digest = plan(broker, token, test=Workflow.WORKFLOW)
    broker.execute(ExecuteRequest(session=token, plan_hash=digest))
    await asyncio.sleep(0)
    with pytest.raises(BrokerError, match="active_job_cannot_release"):
        broker.release(LeaseRequest(session=token))
    with pytest.raises(BrokerError, match="only_queued"):
        broker.cancel(CancelRequest(session=token, job_id="job-one"))
    await broker.queue.join()
    assert broker.backend.max_active == 1
    first = broker.read_logs(LogRequest())
    second = broker.read_logs(LogRequest())
    assert first == second
    stages = [event["stage"] for event in first["entries"] if event["stage"]]
    assert stages == ["mock_flash", "mock_boot", "mock_test"]
    broker.release(LeaseRequest(session=token))
    other = broker.acquire(AcquireRequest(owner=Origin.REMOTE))["session"]
    assert token != other
    with pytest.raises(BrokerError, match="invalid_session"):
        broker.prepare(PrepareRequest(session=token, job_id="stale"))


async def test_cancel_only_queued_and_capacity(tmp_path):
    backend = MockBackend(0.001)
    backend.gate = asyncio.Event()
    b = Broker(
        Settings(state_dir=tmp_path, max_queue=1, allow_mock_execution=True),
        backend=backend,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await b.start()
    try:
        token = b.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
        a = plan(b, token, "a")
        b.execute(ExecuteRequest(session=token, plan_hash=a))
        await asyncio.sleep(0)
        queued = plan(b, token, "b")
        b.execute(ExecuteRequest(session=token, plan_hash=queued))
        third = plan(b, token, "c")
        with pytest.raises(BrokerError, match="queue_full"):
            b.execute(ExecuteRequest(session=token, plan_hash=third))
        b.cancel(CancelRequest(session=token, job_id="b"))
        backend.gate.set()
        await b.queue.join()
        assert b.job("b")["state"] == "cancelled"
        assert backend.calls == 1
    finally:
        backend.gate.set()
        await b.close()


async def test_disconnect_quarantines_and_offline_review(broker):
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    digest = plan(broker, token)
    broker.backend.disconnect_next = True
    broker.execute(ExecuteRequest(session=token, plan_hash=digest))
    await broker.queue.join()
    assert broker.job("job-one")["state"] == "needs_review"
    broker.prepare(PrepareRequest(session=token, job_id="fresh"))
    assert broker.job("job-one")["reviewed"] == 0
    assert broker.backend.calls == 1
    with pytest.raises(ValueError, match="stop_broker"):
        acknowledge_recovery(broker.settings.state_dir, "job-one", lambda _: "REVIEW job-one")
    await broker.close()
    result = acknowledge_recovery(broker.settings.state_dir, "job-one", lambda _: "REVIEW job-one")
    assert not result["replayed"]
    assert broker.backend.calls == 1


async def test_no_capability_or_backend_exception_in_persistent_data(broker):
    class Failing(MockBackend):
        async def run(self, test, stage):
            raise RuntimeError(
                "Authorization: Bearer SYNTHETIC_SECRET https://secret.invalid/token"
            )

    broker.backend = Failing()
    token = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="owner")["session"]
    digest = plan(broker, token)
    broker.execute(ExecuteRequest(session=token, plan_hash=digest))
    await broker.queue.join()
    visible = json.dumps([broker.status(), broker.read_logs(LogRequest()), broker.job("job-one")])
    database_dump = "\n".join(broker._store().db.iterdump())
    for secret in (token, "SYNTHETIC_SECRET", "secret.invalid"):
        assert secret not in visible
        assert secret not in database_dump


def test_usb_identity_follows_port_changes_without_opening():
    selector = DeviceSelector(vid=0xCAFE, pid=0x0001, serial_number="SYNTHETIC-001")
    first = PortInfo(port="COM10", vid=0xCAFE, pid=1, serial_number="SYNTHETIC-001")
    later = first.model_copy(update={"port": "COM21"})
    linux = first.model_copy(update={"port": "/dev/ttyACM9"})
    assert resolve_port(selector, [first]).port == "COM10"
    assert resolve_port(selector, [later]).port == "COM21"
    assert resolve_port(selector, [linux]).port == "/dev/ttyACM9"
    with pytest.raises(ValueError, match="device_missing"):
        resolve_port(selector, [])
    with pytest.raises(ValueError, match="device_ambiguous"):
        resolve_port(selector, [first, later])
    with pytest.raises(ValidationError):
        DeviceSelector(vid=1, pid=1)


@pytest.mark.parametrize(
    "changes", [{"backend": "serial"}, {"host": "0.0.0.0"}, {"port": 1}, {"unknown": True}]
)
def test_configuration_fails_closed(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)


def test_typed_tools_reject_raw_commands_and_paths():
    with pytest.raises(ValidationError):
        PrepareRequest(session="0" * 64, job_id="../private", test="diagnostics")
    with pytest.raises(ValidationError):
        PrepareRequest(session="0" * 64, job_id="safe", test="shell")
    with pytest.raises(ValidationError):
        PrepareRequest(session="0" * 64, job_id="safe", command="format")
