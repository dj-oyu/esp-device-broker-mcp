"""Real serial adapter path with synthetic bytes only; never opens a device."""

import asyncio

import pytest

from cardputer_mcp.app_models import AppCommandRequest, AppSessionOptions
from cardputer_mcp.app_sessions import AppFailure, AppSessions, MockLink
from cardputer_mcp.app_wire import Op, Request
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend


class FramedSyntheticSerial:
    def __init__(self, *, close_failure=False, stays_open=False, disconnect_on_tap=False):
        self.device = MockLink(lambda _: None)
        self.incoming = bytearray()
        self.requests = []
        self.open_count = self.close_count = 0
        self.is_open = False
        self.port = None
        self.dtr = self.rts = True
        self.close_failure = close_failure
        self.stays_open = stays_open
        self.disconnect_on_tap = disconnect_on_tap
        self.disconnected = False

    def open(self):
        assert not self.is_open
        assert self.dtr is self.rts is False
        assert self.port == "SYNTHETIC_PORT"
        self.open_count += 1
        self.is_open = True

    def close(self):
        self.close_count += 1
        if self.close_failure:
            raise OSError("synthetic close failure; must not escape")
        if not self.stays_open:
            self.is_open = False

    def reset_input_buffer(self):
        raise AssertionError("app sessions must not reset or discard serial input")

    def read(self, size=1):
        if self.disconnected:
            raise OSError("synthetic disconnected read; must not escape")
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def write(self, data):
        if self.disconnected:
            raise OSError("synthetic disconnected write; must not escape")
        request = Request.decode(data)
        self.requests.append(request)
        if request.op == Op.CATALOG and request.seq != 65535:
            reply = self.device.catalog()[request.seq]
        else:
            reply = self.device.send(
                request.op,
                request.session,
                request.command_id,
                boot=request.boot,
                run=request.run,
                seq=request.seq,
                fence=request.fence,
                ttl=request.ttl,
                app_id=request.app_id,
                arg=request.arg,
            )
        if self.disconnect_on_tap and request.op == Op.TAP:
            # The device consumed it, but its response is lost. No host replay.
            self.disconnected = True
            raise OSError("synthetic consumed-before-disconnect; must not escape")
        self.incoming.extend(reply.encode())
        return len(data)


def backend_for(port):
    inventory = [
        PortInfo(port="SYNTHETIC_PORT", vid=1, pid=2, serial_number="SYNTHETIC_DEVICE")
    ]
    config = SerialSettings(
        selector=DeviceSelector(vid=1, pid=2, serial_number="SYNTHETIC_DEVICE")
    )
    return SerialBackend(config, inventory=lambda: inventory, factory=lambda: port)


async def start(manager, backend, *, live=lambda: True):
    options = AppSessionOptions(app_id="local.hello", duration_ms=1000)
    plan = {"job_id": "synthetic-job", **manager.prepare(backend, options)}
    task = asyncio.create_task(manager.execute(backend, plan, "owner", lambda _: None, live))
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        run = manager.jobs.get(plan["job_id"])
        if run is not None and run.state == "running":
            return run, task
        if task.done():
            await task
            raise AssertionError("worker finished before started")
        await asyncio.sleep(0.005)
    raise AssertionError("synthetic app start deadline")


def command(run, command_id, operation, action=None):
    return AppCommandRequest(
        session="a" * 64,
        job_id="synthetic-job",
        boot_id=run.boot,
        run_id=run.run,
        control_session=run.session,
        command_id=command_id,
        seq=command_id,
        operation=operation,
        action=action,
    )


async def ack(run, command_id):
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        entry = run.commands.get(command_id)
        if entry is not None and entry[1]["state"] not in ("queued", "sent", "accepted"):
            return entry[1]
        await asyncio.sleep(0.005)
    raise AssertionError("synthetic ACK deadline")


def test_serial_framed_lifecycle_preserves_instance_and_single_port_owner():
    async def scenario():
        port = FramedSyntheticSerial()
        backend, manager = backend_for(port), AppSessions()
        run, task = await start(manager, backend)
        original = run.run
        run.command(command(run, 2, "action.tap", "accept"))
        assert (await ack(run, 2))["state"] == "consumed"
        await asyncio.sleep(0.11)
        run.command(command(run, 3, "suspend"))
        assert (await ack(run, 3))["state"] == "ok"
        assert run.state == "suspended" and run.run == original
        await asyncio.sleep(0.11)
        run.command(command(run, 4, "resume"))
        assert (await ack(run, 4))["state"] == "ok"
        assert run.state == "running" and run.run == original
        run.command(command(run, 5, "stop"))
        assert await asyncio.wait_for(task, 2) == "app_session_stopped"
        operations = [r.op for r in port.requests]
        assert operations == [
            Op.CATALOG,
            Op.CATALOG,
            Op.CATALOG,
            Op.CATALOG,
            Op.OPEN,
            Op.START,
            Op.TAP,
            Op.SUSPEND,
            Op.START,
            Op.STOP,
            Op.CLOSE,
        ]
        assert [r.arg for r in port.requests if r.op == Op.START] == [0, 1]
        assert port.open_count == port.close_count == 1 and not port.is_open
        assert backend.calls == backend.max_active == 1 and backend.active == 0
        assert not backend.ownership_uncertain and not backend.hardware_connected
        assert manager.catalog_cache["source"] == "device"
        assert manager.catalog_cache["firmware_verification"] == "device_reported_unattested"
        assert run.state == "stopped" and not run.running

    asyncio.run(scenario())


@pytest.mark.parametrize("raises", [True, False])
def test_unconfirmed_serial_close_retains_ownership_and_port_reference(raises):
    async def scenario():
        port = FramedSyntheticSerial(close_failure=raises, stays_open=not raises)
        backend, manager = backend_for(port), AppSessions()
        run, task = await start(manager, backend)
        run.cancelled.set()
        with pytest.raises(AppFailure) as failure:
            await asyncio.wait_for(task, 2)
        assert failure.value.code == "serial_close_unconfirmed"
        assert backend.ownership_uncertain and backend.hardware_connected
        assert backend.active == 1 and backend._unclosed_port is port
        assert port.open_count == port.close_count == 1 and port.is_open
        assert not any(e["kind"] == "port_closed" for e in run.events.control)
        assert any(
            e["kind"] == "needs_review" and e["reason"] == "serial_close_unconfirmed"
            for e in run.events.control
        )

    asyncio.run(scenario())


def test_serial_firmware_identity_mismatch_fails_before_open_or_start_commands():
    async def scenario():
        port = FramedSyntheticSerial()
        backend, manager = backend_for(port), AppSessions()
        backend.config = backend.config.model_copy(update={"expected_firmware_sha256": "11" * 32})
        options = AppSessionOptions(app_id="local.hello", duration_ms=1000)
        plan = {"job_id": "synthetic-job", **manager.prepare(backend, options)}
        with pytest.raises(AppFailure) as failure:
            await manager.execute(backend, plan, "owner", lambda _: None, lambda: True)
        assert failure.value.code == "catalog_changed"
        assert all(request.op == Op.CATALOG for request in port.requests)
        assert port.open_count == port.close_count == 1 and not port.is_open
        assert backend.active == 0 and not backend.hardware_connected

    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["cancelled", "authority", "timeout"])
def test_serial_cancel_authority_and_timeout_allow_only_bounded_cleanup(reason):
    async def scenario():
        port = FramedSyntheticSerial()
        backend, manager = backend_for(port), AppSessions()
        authorized = [True]
        run, task = await start(manager, backend, live=lambda: authorized[0])
        if reason == "cancelled":
            run.cancelled.set()
            with pytest.raises(ValueError, match="app_input_stopped"):
                run.command(command(run, 2, "action.tap", "accept"))
        elif reason == "authority":
            authorized[0] = False
            # Admission may race revocation; the worker must recheck before I/O.
            run.command(command(run, 2, "action.tap", "accept"))
        if reason == "authority":
            with pytest.raises(AppFailure) as failure:
                await asyncio.wait_for(task, 2)
            assert failure.value.code == "app_authority_lost"
        else:
            assert await asyncio.wait_for(task, 2) == "app_session_" + reason
        operations = [r.op for r in port.requests]
        assert Op.TAP not in operations
        assert operations[-2:] == [Op.STOP, Op.CLOSE]
        assert operations.count(Op.STOP) == operations.count(Op.CLOSE) == 1
        assert port.open_count == port.close_count == 1 and not port.is_open
        assert backend.active == 0 and not backend.hardware_connected
        assert run.state == "stopped" and not run.running

    asyncio.run(scenario())


def test_serial_disconnect_after_consumption_never_replays_input_or_reopens():
    async def scenario():
        port = FramedSyntheticSerial(disconnect_on_tap=True)
        backend, manager = backend_for(port), AppSessions()
        run, task = await start(manager, backend)
        run.command(command(run, 2, "action.tap", "accept"))
        with pytest.raises(AppFailure) as failure:
            await asyncio.wait_for(task, 2)
        assert failure.value.code == "outcome_unknown"
        assert [r.op for r in port.requests].count(Op.TAP) == 1
        assert [r.op for r in port.requests].count(Op.START) == 1
        assert run.commands[2][1]["state"] == "outcome_unknown"
        assert port.open_count == port.close_count == 1 and not port.is_open
        assert backend.calls == 1 and backend.active == 0 and not backend.hardware_connected
        assert any(e["kind"] == "needs_review" for e in run.events.control)
        assert not any(
            e["kind"] == "finished" and e["teardown_confirmed"] for e in run.events.control
        )
        assert AppSessions().jobs == {}
        assert port.open_count == 1

    asyncio.run(scenario())
