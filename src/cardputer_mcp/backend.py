"""Backend interface and synthetic adapter. Hardware is isolated in serial_backend."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .capture import CapturedImage, mock_image
from .derby_observation import DerbyObservation
from .device_observation import DeviceObservation
from .models import TestName
from .serial_diagnostics import sanitize_receive_summary


class DeviceDisconnected(Exception):
    """The device state can no longer be established safely."""


class DeviceOperationFailure(Exception):
    """Public diagnostics contain allowlisted categories and bounded counts only."""

    CODES = frozenset(
        {
            "serial_operation_uncertain",
            "serial_deadline",
            "serial_capture_limit",
            "serial_fault_marker",
            "serial_line_limit",
            "serial_marker_limit",
            "command_not_allowlisted",
            "serial_not_quiet_before_command",
            "incomplete_serial_write",
            "serial_marker_timeout",
            "wrong_category",
            "wrong_app_navigation",
            "hello_not_selected",
            "wrong_app_identity",
            "hello_not_ready_or_out_of_order",
            "teardown_sequence_missing",
            "teardown_sequence_invalid",
            "teardown_memory_invalid",
            "incomplete_final_line",
            "unexpected_app_launch",
            "multiple_device_owners",
            "operation_interrupted_review_required",
            "device_or_plan_changed",
            "device_identity_changed",
            "serial_close_unconfirmed",
            "serial_open_failed",
            "serial_read_failed",
            "serial_write_failed",
            "serial_capture_invalid",
            "serial_capture_timeout",
            "serial_capture_boundary",
            "serial_capture_unexpected_app",
            "serial_restart_unsupported",
            "serial_restart_buffer_discard_failed",
            "serial_restart_control_failed",
            "serial_reconnect_timeout",
        }
    )
    STEPS = frozenset(
        {
            "operation",
            "open_serial",
            "validate_identity",
            "initial_home",
            "select_apps",
            "select_hello",
            "launch_hello",
            "count_hello",
            "stop_hello",
            "settle",
            "passive_observe",
            "firmware_observe",
            "restart_buffer_discard",
            "restart_control",
            "restart_observe",
            "reconnect_serial",
            "command",
            "close_serial",
            "capture_home",
        }
    )
    MARKERS = frozenset(
        {
            "HOME_READY",
            "CATEGORY",
            "APP_ROW",
            "FRAME_PRESENTED",
            "HELLO_COUNT_ONE",
            "TEARDOWN",
            "CAPTURE_END",
        }
    )

    def __init__(
        self,
        code: str,
        *,
        step: str = "operation",
        expected_marker: str | None = None,
        received_bytes: int = 0,
        received_lines: int = 0,
        recognized_markers: int = 0,
        receive_summary: object = None,
    ) -> None:
        self.code = code if code in self.CODES else "serial_operation_uncertain"
        self.diagnostic: dict[str, Any] = {
            "code": self.code,
            "step": step if step in self.STEPS else "operation",
            "expected_marker": expected_marker if expected_marker in self.MARKERS else None,
            "received_bytes": max(0, min(received_bytes, 1048576)),
            "received_lines": max(0, min(received_lines, 65536)),
            "recognized_markers": max(0, min(recognized_markers, 65536)),
        }
        if summary := sanitize_receive_summary(receive_summary):
            self.diagnostic["receive_summary"] = summary
        super().__init__(self.code)


@dataclass(frozen=True)
class BackendResult:
    code: str
    image: CapturedImage | None = field(default=None, repr=False)
    observation: DeviceObservation | None = field(default=None, repr=False)
    derby_observation: DerbyObservation | None = field(default=None, repr=False)


class Backend(Protocol):
    active: int
    max_active: int

    name: str
    device: str
    hardware_connected: bool

    def prepare(self, test: TestName) -> dict[str, Any]: ...
    async def execute(
        self, plan: dict[str, Any], stage: Callable[[str], None]
    ) -> str | BackendResult: ...


def stages(test: TestName) -> tuple[str, ...]:
    if test == TestName.CAPTURE:
        return ("mock_capture",)
    if test == TestName.WORKFLOW:
        return ("mock_flash", "mock_boot", "mock_test")
    return ("mock_diagnostics",)


class MockBackend:
    name = "mock"
    device = "mock-cardputer"
    hardware_connected = False

    def prepare(self, test: TestName) -> dict[str, Any]:
        if test not in (TestName.DIAGNOSTICS, TestName.WORKFLOW, TestName.CAPTURE):
            raise ValueError("test_not_available_for_backend")
        details: dict[str, Any] = {"stages": list(stages(test))}
        if test == TestName.CAPTURE:
            details["effects"] = ["Returns a synthetic TEST / NOT DEVICE image; no hardware"]
        return details

    async def execute(
        self, plan: dict[str, Any], stage: Callable[[str], None]
    ) -> str | BackendResult:
        await self.run(TestName(plan["test"]), stage)
        if TestName(plan["test"]) == TestName.CAPTURE:
            return BackendResult("synthetic_capture_not_device", mock_image())
        return "mock_pass"

    def __init__(self, stage_seconds: float = 0.15) -> None:
        self.stage_seconds = stage_seconds
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.disconnect_next = False  # Test-only injection; never an MCP tool.
        self.gate: asyncio.Event | None = None  # Deterministic concurrency tests.

    async def run(self, test: TestName, stage: Callable[[str], None]) -> None:
        if self.active:
            raise RuntimeError("multiple_device_owners")
        self.active += 1
        self.calls += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for name in stages(test):
                stage(name)
                if self.gate is not None:
                    await self.gate.wait()
                await asyncio.sleep(self.stage_seconds)
                if self.disconnect_next:
                    self.disconnect_next = False
                    raise DeviceDisconnected()
        finally:
            # Broker does not hand over ownership until this block has run.
            self.active -= 1
