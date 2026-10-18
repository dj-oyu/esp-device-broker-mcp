"""Saved worker callbacks must never acquire authority from the next job."""

from test_derby_http import derby_service as derby_service
from test_derby_smoke import Wire
from test_pending_execution import running
from test_serial_http import serial_service as serial_service

from cardputer_mcp.config import SerialSettings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend


async def test_saved_derby_callback_stays_bound_to_completed_job(derby_service, monkeypatch):
    broker = derby_service.resident
    wires = []

    def factory():
        wire = Wire()
        wires.append(wire)
        return wire

    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=factory,
        clock=lambda: wires[-1].now if wires else 0,
    )
    broker.backend = backend
    broker.settings = broker.settings.model_copy(
        update={"backend": "serial", "serial": config, "allow_mock_execution": False}
    )
    execute = backend.execute_smoke
    callbacks = []

    async def checked(plan, stage, live):
        assert live() is True
        for previous in callbacks:
            assert previous() is False
        callbacks.append(live)
        return await execute(plan, stage, live)

    monkeypatch.setattr(backend, "execute_smoke", checked)
    lease = broker.acquire(
        AcquireRequest(owner=Origin.REMOTE, lease_seconds=300),
        principal_id="ephemeral:remote_mcp",
    )["session"]
    for job_id in ("callback-first", "callback-second"):
        plan = broker.prepare(PrepareRequest(session=lease, job_id=job_id, test="derby_smoke"))
        broker.execute(ExecuteRequest(session=lease, plan_hash=plan["plan_hash"]))
        await broker.queue.join()
        assert broker.job(job_id)["state"] == "succeeded"
        assert all(callback() is False for callback in callbacks)
    assert len(callbacks) == len(wires) == 2
    assert all(wire.closed for wire in wires)


async def test_refresh_discards_legacy_delivery_consent_without_reviving_it(tmp_path):
    async with running(tmp_path) as (broker, _):
        broker.capture_delivery_consents["legacy-job"] = object()
        broker.status()
        assert broker.capture_delivery_consents == {}
        assert broker.backend.calls == 0
