"""Production serial dispatch with synthetic bounded firmware; host only."""

import threading

import pytest

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.native_diagnostic import request_identity
from cardputer_mcp.native_diagnostic_protocol import ZERO_BOOT, ZERO_ID
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend, SerialFailure

BOOT = "b" * 16


def frame(request_id, state, flags, boot=BOOT, enabled=1):
    return f"\x1dND1 {request_id} {state} {flags} {boot} {int(enabled)}\n".encode()


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class Port:
    def __init__(self, clock, cancel, scenario="complete"):
        self.clock, self.cancel, self.scenario = clock, cancel, scenario
        self.is_open = False
        self.dtr = self.rts = True
        self.port = None
        self.writes = []
        self.incoming = []
        self.request_id = None
        self.cancelled = False
        self.closed = threading.Event()

    def open(self):
        self.is_open = True

    def close(self):
        self.closed.set()
        if self.scenario == "close_failure":
            raise OSError("PRIVATE path")
        self.is_open = False

    def reset_input_buffer(self):
        pytest.fail("native diagnostic must not discard or reopen")

    def write(self, data):
        self.writes.append(data)
        prefix, operation, request_id, boot = data.decode().removesuffix("\n").split(" ")
        assert prefix == "\x1dD1"
        if request_id == ZERO_ID:
            assert operation == "STATUS" and boot == ZERO_BOOT
            self.incoming.append(
                frame(ZERO_ID, "REFUSED", "00", enabled=self.scenario != "disabled")
            )
        elif operation == "REQUEST":
            self.request_id = request_id
            if self.scenario == "partial_write":
                return len(data) - 1
            if self.scenario == "wrong_boot":
                self.incoming.append(frame(request_id, "RUNNING", "00", boot="c" * 16))
            elif self.scenario == "wrong_id":
                self.incoming.append(frame("c" * 32, "RUNNING", "00"))
            elif self.scenario == "invalid":
                self.incoming.append(frame(request_id, "COMPLETED", "01"))
            elif self.scenario in ("quarantined", "unknown"):
                self.incoming.append(
                    frame(
                        request_id,
                        "QUARANTINED" if self.scenario == "quarantined" else "REFUSED",
                        "00",
                    )
                )
            elif self.scenario == "refused":
                self.incoming.append(frame(request_id, "REFUSED", "20"))
            else:
                self.incoming.append(frame(request_id, "RUNNING", "00"))
                if self.scenario.startswith("cancel"):
                    self.cancel.set()
        elif operation == "CANCEL":
            assert not self.cancelled
            self.cancelled = True
            self.incoming.append(frame(request_id, "CANCEL_REQUESTED", "00"))
        elif operation == "STATUS" and self.scenario != "timeout":
            if self.scenario == "external_cancel" and not self.cancelled:
                return len(data)
            if self.scenario.startswith("cancel") or self.cancelled:
                self.incoming.append(
                    frame(
                        request_id,
                        "CANCELLED",
                        "3e" if self.scenario == "cancel_before_work" else "1f",
                    )
                )
            else:
                self.incoming.append(frame(request_id, "COMPLETED", "1f"))
        return len(data)

    def read(self, size=1):
        self.clock.now += 0.1
        if not self.incoming:
            return b""
        raw = self.incoming[0]
        chunk, rest = raw[:size], raw[size:]
        if rest:
            self.incoming[0] = rest
        else:
            self.incoming.pop(0)
        return chunk


def fixture(scenario="complete"):
    clock = Clock()
    inventory = [PortInfo(port="COM42", vid=0xCAFE, pid=1, serial_number="SYNTHETIC")]
    config = SerialSettings(
        selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number="SYNTHETIC"),
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(config, inventory=lambda: inventory, clock=clock)
    port = Port(clock, backend.native_cancel, scenario)
    backend.factory = lambda: port
    plan = backend.prepare(Action.NATIVE_DIAGNOSTIC) | {
        "test": Action.NATIVE_DIAGNOSTIC.value,
        "job_id": "native-one",
        "broker_epoch": "e" * 32,
        "session_id": "d" * 32,
    }
    return backend, port, plan


@pytest.mark.parametrize(
    "scenario,code",
    [
        ("complete", "native_diagnostic_completed"),
        ("cancel", "native_diagnostic_cancelled"),
        ("cancel_before_work", "native_diagnostic_cancelled"),
        ("refused", "native_diagnostic_refused"),
    ],
)
async def test_native_serial_requires_terminal_proof_and_confirmed_close(scenario, code):
    backend, port, plan = fixture(scenario)
    stages = []
    result = await backend.execute_native(plan, stages.append, lambda: True)
    assert result.code == code
    assert result.native_diagnostic.request_id == request_identity(plan)
    assert port.closed.is_set() and not port.is_open
    assert backend.active == 0 and not backend.ownership_uncertain
    assert stages.index("native_diagnostic_terminal") < stages.index("serial_close")
    assert sum(b" REQUEST " in value for value in port.writes) == 1
    assert sum(b" CANCEL " in value for value in port.writes) == scenario.startswith("cancel")
    assert all(value.startswith(b"\x1dD1 ") and len(value) <= 64 for value in port.writes)


@pytest.mark.parametrize(
    "scenario,code,uncertain",
    [
        ("disabled", "native_diagnostic_not_enabled", False),
        ("timeout", "native_diagnostic_timeout", True),
        ("wrong_boot", "native_diagnostic_boot_changed", True),
        ("wrong_id", "native_diagnostic_protocol_invalid", True),
        ("invalid", "native_diagnostic_protocol_invalid", True),
        ("quarantined", "native_diagnostic_quarantined", True),
        ("unknown", "native_diagnostic_state_unknown", True),
        ("partial_write", "incomplete_serial_write", True),
        ("close_failure", "serial_close_unconfirmed", True),
    ],
)
async def test_uncertain_native_work_or_close_cannot_release_ownership(scenario, code, uncertain):
    backend, port, plan = fixture(scenario)
    with pytest.raises(SerialFailure, match="^" + code + "$"):
        await backend.execute_native(plan, lambda _: None, lambda: True)
    assert port.closed.is_set()
    assert backend.ownership_uncertain is uncertain
    assert backend.active == int(uncertain)
    assert sum(b" REQUEST " in value for value in port.writes) <= 1
    assert (
        not any(b" REQUEST " in value for value in port.writes) if scenario == "disabled" else True
    )


async def test_authority_loss_blocks_cancel_and_status_writes_but_allows_close():
    backend, port, plan = fixture("cancel")

    def live():
        return port.request_id is None

    with pytest.raises(SerialFailure, match="operation_interrupted_review_required"):
        await backend.execute_native(plan, lambda _: None, live)
    assert port.closed.is_set()
    assert backend.ownership_uncertain and backend.active == 1
    assert not any(b" CANCEL " in value for value in port.writes)
    assert sum(b" STATUS " in value for value in port.writes) == 1


def test_native_operation_is_serial_only_and_legacy_mock_diagnostics_stays_mock():
    with pytest.raises(ValueError, match="test_not_available_for_backend"):
        MockBackend().prepare(Action.NATIVE_DIAGNOSTIC)
    assert MockBackend().prepare(Action.DIAGNOSTICS) == {"stages": ["mock_diagnostics"]}


async def test_cancel_during_bootstrap_never_launches_new_firmware_work():
    backend, port, plan = fixture()
    original = port.read

    def read(size):
        backend.native_cancel.set()
        return original(size)

    port.read = read
    result = await backend.execute_native(plan, lambda _: None, lambda: True)
    assert result.code == "native_diagnostic_cancelled_before_request"
    assert result.native_diagnostic is None
    assert len(port.writes) == 1 and b" STATUS " in port.writes[0]
    assert backend.active == 0 and not backend.ownership_uncertain
    assert not port.is_open


async def broker_run(tmp_path, scenario="complete"):
    from cardputer_mcp.broker import Broker
    from cardputer_mcp.config import Settings
    from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest

    backend, port, _ = fixture(scenario)
    broker = Broker(
        Settings(state_dir=tmp_path / "state", backend="serial", serial=backend.config),
        backend=backend,
        serial_enabled=True,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda *args: True,
    )
    await broker.start()
    session = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="alice")["session"]
    prepared = broker.prepare(
        PrepareRequest(session=session, job_id="native", test=Action.NATIVE_DIAGNOSTIC)
    )
    broker.execute(ExecuteRequest(session=session, plan_hash=prepared["plan_hash"]))
    return broker, backend, port, session


async def terminal(broker):
    import asyncio

    for _ in range(1000):
        row = broker.job("native")
        if row["state"] not in ("queued", "running"):
            return row
        await asyncio.sleep(0.002)
    pytest.fail("synthetic native job did not reach terminal state")


@pytest.mark.parametrize(
    "scenario,state",
    [
        ("complete", "succeeded"),
        ("cancel", "cancelled"),
        ("cancel_before_work", "cancelled"),
        ("refused", "failed"),
    ],
)
async def test_broker_native_terminal_commit_persists_separate_phase_evidence(
    tmp_path, scenario, state
):
    broker, backend, port, _ = await broker_run(tmp_path, scenario)
    try:
        row = await terminal(broker)
        assert row["state"] == state
        proof = row["diagnostic"]["native_diagnostic"]
        assert proof["serial_close_confirmed"] is True
        if state != "failed":
            assert proof["home_restored"] and proof["output_drained"]
            assert proof["borrows_released"] and proof["owner_released"]
        else:
            assert proof["work_not_started"] and not proof["work_finished"]
        assert backend.active == 0 and not port.is_open
    finally:
        await broker.close()


@pytest.mark.parametrize("scenario", ["timeout", "quarantined"])
async def test_unknown_native_end_keeps_lease_and_blocks_queued_successor(tmp_path, scenario):
    from cardputer_mcp.broker import BrokerError
    from cardputer_mcp.models import ExecuteRequest, LeaseRequest, PrepareRequest

    broker, backend, port, session = await broker_run(tmp_path, scenario)
    try:
        successor = broker.prepare(
            PrepareRequest(session=session, job_id="next", test=Action.NATIVE_DIAGNOSTIC)
        )
        broker.execute(ExecuteRequest(session=session, plan_hash=successor["plan_hash"]))
        row = await terminal(broker)
        assert row["state"] == "needs_review"
        assert backend.active == 1 and backend.ownership_uncertain
        assert not port.is_open
        assert broker.job("next")["state"] == "cancelled"
        with pytest.raises(BrokerError, match="active_job_cannot_release"):
            broker.release(LeaseRequest(session=session))
        assert broker.session is not None
        assert sum(b" REQUEST " in value for value in port.writes) == 1
    finally:
        with pytest.raises(BrokerError, match="device_close_unconfirmed"):
            await broker.close()


@pytest.mark.parametrize("mutation", ["authority", "closed_plan"])
async def test_native_final_commit_refuses_changed_authority_or_close_binding(tmp_path, mutation):
    broker, backend, _, _ = await broker_run(tmp_path)
    original = broker.finish

    def finish(run, result, failure):
        if mutation == "authority":
            broker.principal_valid = lambda *args: False
        else:
            backend.last_closed_plan = {"test": "other"}
        return original(run, result, failure)

    broker.finish = finish
    try:
        row = await terminal(broker)
        assert row["state"] == "needs_review"
        assert row["result"] != "native_diagnostic_completed"
    finally:
        await broker.close()


async def test_broker_cancel_delivery_waits_for_firmware_end_and_serial_close(tmp_path):
    import asyncio

    from cardputer_mcp.broker import BrokerError
    from cardputer_mcp.models import CancelRequest, LeaseRequest

    broker, backend, port, session = await broker_run(tmp_path, "external_cancel")
    closing, release = threading.Event(), threading.Event()
    original = port.close

    def close():
        closing.set()
        assert release.wait(3)
        original()

    port.close = close
    try:
        for _ in range(1000):
            if port.request_id is not None:
                break
            await asyncio.sleep(0.001)
        response = broker.cancel(CancelRequest(session=session, job_id="native"))
        assert response["stop_requested"] is True and response["state"] == "running"
        for _ in range(1000):
            if closing.is_set():
                break
            await asyncio.sleep(0.001)
        assert closing.is_set() and port.cancelled
        assert broker.job("native")["state"] == "running" and backend.active == 1
        with pytest.raises(BrokerError, match="active_job_cannot_release"):
            broker.release(LeaseRequest(session=session))
        release.set()
        assert (await terminal(broker))["state"] == "cancelled"
        assert sum(b" CANCEL " in value for value in port.writes) == 1
    finally:
        release.set()
        await broker.close()


async def test_cancel_inside_last_authority_callback_wins_request_admission_cutoff():
    backend, port, plan = fixture()
    bootstrap_received = False
    original = port.read

    def read(size):
        nonlocal bootstrap_received
        raw = original(size)
        bootstrap_received = True
        return raw

    port.read = read

    def live():
        # SmokePort checks once after bootstrap read, and once immediately before
        # REQUEST. Set cancel in the latter callback, after run's earlier check.
        if bootstrap_received:
            live.calls += 1
            if live.calls == 2:
                backend.request_native_cancel()
        return True

    live.calls = 0
    result = await backend.execute_native(plan, lambda _: None, live)
    assert result.code == "native_diagnostic_cancelled_before_request"
    assert not backend.native_request_admitted
    assert len(port.writes) == 1 and b" STATUS " in port.writes[0]
    assert backend.active == 0 and not backend.ownership_uncertain


async def test_request_admission_winning_cutoff_requires_firmware_cancel_terminal():
    backend, port, plan = fixture("external_cancel")
    original = port.write

    def write(raw):
        if b" REQUEST " in raw:
            assert backend.native_request_admitted
            backend.request_native_cancel()
        return original(raw)

    port.write = write
    result = await backend.execute_native(plan, lambda _: None, lambda: True)
    assert result.code == "native_diagnostic_cancelled"
    assert backend.native_request_admitted
    assert result.native_diagnostic.cancelled
    assert sum(b" REQUEST " in raw for raw in port.writes) == 1
    assert sum(b" CANCEL " in raw for raw in port.writes) == 1
    assert not backend.ownership_uncertain and backend.active == 0


async def test_broker_local_pre_request_cancel_does_not_claim_home_or_firmware_release(tmp_path):
    broker, backend, port, _ = await broker_run(tmp_path)
    original = port.read

    def read(size):
        backend.request_native_cancel()
        return original(size)

    port.read = read
    try:
        row = await terminal(broker)
        assert row["state"] == "cancelled"
        proof = row["diagnostic"]["native_diagnostic"]
        assert proof["state"] == "NOT_REQUESTED" and proof["work_not_started"]
        assert proof["serial_close_confirmed"]
        assert not any(
            proof[key]
            for key in (
                "home_restored",
                "output_drained",
                "borrows_released",
                "owner_released",
                "work_finished",
            )
        )
        assert len(port.writes) == 1
    finally:
        await broker.close()
