"""Synthetic read errors identify a boundary, never a guessed root cause."""

import json
import threading
from copy import deepcopy

import pytest
import serial
from test_serial_backend import Clock, FakePort, backend

from cardputer_mcp.models import TestName as Action
from cardputer_mcp.serial_backend import Reader, SerialFailure
from cardputer_mcp.smoke_port import SmokePort


@pytest.mark.parametrize(
    "code", ["operation_interrupted_review_required", "device_identity_changed", "serial_deadline"]
)
def test_reader_preserves_known_failure_identity_code_and_existing_diagnostics(code):
    clock = Clock()
    port = FakePort(clock)
    failure = SerialFailure(
        code, step="passive_observe", received_bytes=17, received_lines=2, recognized_markers=1
    )
    failure.diagnostic["failure_origin"] = "authority_or_stop_check"
    diagnostic = deepcopy(failure.diagnostic)

    def fail(size):
        raise failure

    port.read = fail
    reader = Reader(port, lambda _: None, clock, 10)
    with pytest.raises(SerialFailure) as caught:
        reader.read_once()
    assert caught.value is failure
    assert caught.value.diagnostic == diagnostic
    assert caught.value.code == code
    assert port.writes == []


@pytest.mark.parametrize(
    "error,code,origin",
    [
        (OSError, "serial_read_failed", "port_read_os_error"),
        (serial.SerialException, "serial_read_failed", "port_read_os_error"),
        (RuntimeError, "serial_operation_uncertain", "port_read_error"),
    ],
)
async def test_read_errors_are_fixed_safe_and_close_without_retry(error, code, origin):
    device, port = backend()

    def fail(size):
        raise error("SYNTHETIC_PRIVATE_DRIVER_ERROR token=SYNTHETIC_SECRET")

    port.read = fail
    plan = {"test": Action.HELLO.value, **device.prepare(Action.HELLO)}
    with pytest.raises(SerialFailure) as caught:
        await device.execute(plan, lambda _: None)
    assert caught.value.code == code
    assert caught.value.diagnostic["failure_origin"] == origin
    assert "SYNTHETIC_PRIVATE" not in json.dumps(caught.value.diagnostic)
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert caught.value.__cause__ is caught.value.__context__ is None
    assert port.opens == port.closes == 1 and port.writes == []
    assert device.active == 0 and not device.hardware_connected and not device.ownership_uncertain


@pytest.mark.parametrize("source", ["authority", "stop", "lookup_os_error", "lookup_non_os_error"])
def test_smoke_guard_origin_does_not_guess_the_reason_or_read_the_inner_port(source):
    clock = Clock()
    inner = FakePort(clock)
    stop = threading.Event()

    def live():
        if source == "lookup_os_error":
            raise TimeoutError("SYNTHETIC_PRIVATE_AUTHORITY_ERROR")
        if source == "lookup_non_os_error":
            raise RuntimeError("SYNTHETIC_PRIVATE_AUTHORITY_ERROR")
        return source != "authority"

    if source == "stop":
        stop.set()
    reader = Reader(SmokePort(inner, live, stop), lambda _: None, clock, 10)
    with pytest.raises(SerialFailure) as caught:
        reader.read_once()
    assert caught.value.code == "operation_interrupted_review_required"
    assert caught.value.diagnostic["failure_origin"] == "authority_or_stop_check"
    assert clock.value == 0 and inner.writes == []
    assert "SYNTHETIC_PRIVATE" not in json.dumps(caught.value.diagnostic)


def test_smoke_guard_preserves_a_known_lookup_failure_and_existing_diagnostic_fields():
    failure = SerialFailure("device_identity_changed", step="validate_identity", received_bytes=13)
    diagnostic = deepcopy(failure.diagnostic)

    def live():
        raise failure

    port = SmokePort(FakePort(Clock()), live, threading.Event())
    with pytest.raises(SerialFailure) as caught:
        port.read(4096)
    assert caught.value is failure
    assert caught.value.diagnostic == {**diagnostic, "failure_origin": "authority_or_stop_check"}
