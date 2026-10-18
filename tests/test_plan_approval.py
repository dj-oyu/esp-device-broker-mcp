"""Immutable bounded preflight remains enforced without local approval or USB opens."""

import json
from contextlib import asynccontextmanager

import pytest

from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def running(tmp_path, *, backend=None):
    clock = Clock()
    settings = {"backend": "serial", "serial": backend.config} if backend else {}
    broker = Broker(
        Settings(state_dir=tmp_path / "state", mock_stage_seconds=0.001, **settings),
        backend=backend,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        serial_enabled=backend is not None,
        principal_valid=lambda *args: True,
    )
    await broker.start()
    broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="synthetic-client")
    try:
        yield broker, PlanApprovals(broker), clock
    finally:
        broker.closing = False
        await broker.close()


def prepare(broker, test=Action.DIAGNOSTICS):
    return broker.prepare(
        PrepareRequest(session=broker.session.capability, job_id="test-job", test=test)
    )["plan_hash"]


def execute(broker, digest):
    return broker.execute(ExecuteRequest(session=broker.session.capability, plan_hash=digest))


async def test_preflight_is_read_only_and_approval_surface_is_retired(tmp_path):
    async with running(tmp_path) as (broker, validator, _):
        digest = prepare(broker)
        before = "\n".join(broker._store().db.iterdump())
        assert validator.validate_current(digest, broker.session) == broker._store().plan(digest)
        assert validator.list_pending() == {"plans": [], "retired": True}
        with pytest.raises(BrokerError, match="^approval_interface_retired$"):
            validator.approve(digest, "a" * 64, "APPROVE")
        assert "\n".join(broker._store().db.iterdump()) == before
        assert broker.backend.calls == 0


@pytest.mark.parametrize("mutation", ["extra", "stage", "test", "version", "oversize", "duplicate"])
async def test_changed_or_malformed_plan_cannot_enqueue(tmp_path, mutation):
    async with running(tmp_path) as (broker, _, _):
        digest = prepare(broker)
        payload = broker._store().plan(digest)
        if mutation == "extra":
            payload["command"] = "arbitrary-command"
        elif mutation == "stage":
            payload["stages"] = ["unexpected"]
        elif mutation == "test":
            payload["test"] = "shell"
        elif mutation == "version":
            payload["version"] = True
        elif mutation == "oversize":
            payload["extra"] = "x" * 17000
        raw = json.dumps(payload)
        changed_digest = digest_plan(payload)
        if mutation == "duplicate":
            raw = raw[:-1] + ', "version": 1}'
        db = broker._store().db
        db.execute("UPDATE plans SET hash=?, payload=? WHERE hash=?", (changed_digest, raw, digest))
        db.commit()
        with pytest.raises(BrokerError):
            execute(broker, changed_digest)
        assert broker.queue.empty()
        assert broker._store().record_count("jobs") == 0
        assert broker.backend.calls == 0


async def test_current_backend_preflight_is_rechecked_without_reflecting_exceptions(tmp_path):
    async with running(tmp_path) as (broker, _, _):
        digest = prepare(broker)

        def fail(_):
            raise RuntimeError("SYNTHETIC_SECRET")

        broker.backend.prepare = fail
        with pytest.raises(BrokerError, match="^backend_preflight_failed$"):
            execute(broker, digest)
        assert broker.queue.empty() and broker.backend.calls == 0


def serial_backend():
    config = SerialSettings(
        selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number="SYNTHETIC-ONLY"),
        expected_firmware_sha256="a" * 64,
    )
    inventory = [PortInfo(port="COM42", vid=0xCAFE, pid=1, serial_number="SYNTHETIC-ONLY")]

    def no_open():
        pytest.fail("preflight must never construct or open a serial port")

    return SerialBackend(config, inventory=lambda: inventory, factory=no_open), inventory


@pytest.mark.parametrize("change", ["port", "firmware", "inventory", "exception", "deadline"])
async def test_serial_device_firmware_and_deadline_binding_rechecked_before_enqueue(
    tmp_path, change
):
    backend, inventory = serial_backend()
    async with running(tmp_path, backend=backend) as (broker, _, clock):
        digest = prepare(broker, Action.HELLO)
        if change == "port":
            inventory[0] = inventory[0].model_copy(update={"port": "COM99"})
        elif change == "firmware":
            updated = backend.config.model_copy(update={"expected_firmware_sha256": "b" * 64})
            backend.config = updated
            broker.settings = broker.settings.model_copy(update={"serial": updated})
        elif change == "inventory":
            inventory.clear()
        elif change == "exception":

            def fail():
                raise RuntimeError("SYNTHETIC_SECRET")

            backend.inventory = fail
        else:

            def expire():
                clock.now += 30
                return inventory

            backend.inventory = expire
        with pytest.raises(BrokerError) as caught:
            execute(broker, digest)
        assert "SYNTHETIC_SECRET" not in str(caught.value)
        assert broker.queue.empty() and broker.backend.calls == 0
        assert broker._store().record_count("jobs") == 0
