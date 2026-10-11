"""One exact restart approval and bounded metadata display, entirely synthetic."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from test_gui_device_observation import PRINCIPAL, STARTUP, digest, firmware_card, observed_row
from test_gui_timeline import loaded, panel_fixture, refresh, snapshot
from test_operator_timeline import prepare, running

from cardputer_mcp.broker import RetainedDeviceObservation
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.gui_approvals import PlanReview, review_warning
from cardputer_mcp.gui_timeline import (
    EXPIRED_OBSERVATION,
    INVALID_TIMELINE,
    TimelineSnapshot,
    firmware_observation_text,
    perform_timeline,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_timeline import observation_summary
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend


def restart_card() -> dict[str, Any]:
    row = firmware_card()
    row["test"] = "restart_and_observe"
    plan = row["review"]["plan"]
    plan.update(
        test="restart_and_observe",
        protocol="esp-idf-startup-log-v1",
        firmware_verification="unverified_device_report",
        restart={
            "method": "esp32s3_usb_serial_jtag_rts_v1",
            "attempts": 1,
            "rts_hold_milliseconds": 100,
            "reconnect_seconds": 8,
            "max_reopens": 1,
            "stable_usb_fingerprint": "f" * 64,
            "buffer_policy": "discard_before_reset",
        },
    )
    row["plan_hash"] = row["review"]["plan_hash"] = digest(plan)
    return row


def completed_restart(data: bytes = STARTUP, *, capture_gap: bool = False) -> dict[str, Any]:
    observation = observed_row(data)
    observation["metadata"].update(reset_sequence_issued=True, capture_gap=capture_gap)
    return restart_card() | {
        "state": "succeeded",
        "review": None,
        "stage": "serial_close",
        "result": "restart_observation_completed_unverified",
        "device_observation": observation,
    }


def test_one_click_discloses_reset_and_exact_delivery_without_another_dialog(monkeypatch: Any):
    panel, api, clock, _ = panel_fixture(monkeypatch, restart_card())
    view = panel.views["job-a"]
    assert view.expanded and not view.details_expanded
    for required in (
        "ESP32-S3 native USB Serial/JTAG ONLY",
        "VID 303A / PID 1001",
        "shared with other chips",
        "nonempty USB serial",
        "lose unsaved state",
        "100 ms RTS pulse",
        "no reset retry",
        "Discards buffered pre-reset serial input",
        "30 seconds and 64 KiB",
        "at most once within 8 seconds",
        "same USB identity",
        "may lose early boot logs",
        "No serial command bytes, bootloader commands or firmware flash",
        "private data",
        "not guaranteed complete",
        "does not verify hardware reset or application boot",
        "ROM download-mode",
        "identity remains unknown",
        "No automatic policy approval",
        PRINCIPAL,
        "No additional send approval",
        "five minutes",
    ):
        assert required in view.summary_var.get()
    assert view.approve_button.config["text"] == "Approve reset, observation and delivery"
    assert view.attestation.hidden and view.home_line_opt_in.hidden
    assert view.image_panel.hidden and view.image_actions.hidden
    assert api.calls == [("timeline",)]
    assert panel.worker.commands.empty()
    view._arm_plan()
    view._approve()
    view._approve()
    assert panel.worker.commands.qsize() == 1
    operation = panel.worker.commands.get_nowait()
    assert panel.controller.accept(perform_timeline(api, operation, clock))
    assert api.calls == [("timeline",), ("approve", operation.review.plan_hash, "d" * 64, "")]
    assert panel.controller.next_preview() is None
    assert panel.controller.next_home_line() is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("method", "generic_dtr_reset"),
        ("method", None),
        ("attempts", 2),
        ("attempts", True),
        ("rts_hold_milliseconds", 101),
        ("rts_hold_milliseconds", 100.0),
        ("reconnect_seconds", 9),
        ("reconnect_seconds", "8"),
        ("max_reopens", 2),
        ("max_reopens", True),
        ("stable_usb_fingerprint", "f" * 63),
        ("stable_usb_fingerprint", "F" * 64),
        ("stable_usb_fingerprint", None),
        ("buffer_policy", "keep"),
        ("extra", "command"),
    ],
)
def test_review_rejects_changed_reset_method_or_limits(field: str, value: Any):
    data = restart_card()["review"]
    data["plan"]["restart"][field] = value
    data["plan_hash"] = digest(data["plan"])
    with pytest.raises(ValueError, match="invalid_plan_response"):
        PlanReview.parse(data | {"ok": True}, 1000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("restart", None),
        ("restart", {}),
        ("protocol", "pocketjs-hello-v1"),
        ("firmware_verification", "verified"),
        ("observe_seconds", 31),
        ("observe_seconds", 30.0),
        ("backend", "mock"),
    ],
)
def test_review_rejects_wrong_restart_protocol_or_unbounded_observation(field: str, value: Any):
    data = restart_card()["review"]
    data["plan"][field] = value
    data["plan_hash"] = digest(data["plan"])
    with pytest.raises(ValueError, match="invalid_plan_response"):
        PlanReview.parse(data | {"ok": True}, 1000)


@pytest.mark.parametrize(
    "field,value",
    [("principal_id", "invalid\nrecipient"), ("max_received_bytes", 65537), ("origin", "unknown")],
)
def test_restart_review_keeps_exact_recipient_and_bounds(field: str, value: Any):
    data = restart_card()["review"]
    data["plan"]["log_delivery"][field] = value
    data["plan_hash"] = digest(data["plan"])
    with pytest.raises(ValueError, match="invalid_plan_response"):
        PlanReview.parse(data | {"ok": True}, 1000)


def test_standalone_review_requires_same_reset_disclosure():
    review = PlanReview.parse(restart_card()["review"] | {"ok": True}, 1000)
    assert review.attestation == "none"
    text = review_warning(review)
    assert "This exact approval click permits the reset and report delivery" in text
    assert "No automatic policy approval" in text and PRINCIPAL in text


@pytest.mark.parametrize("part", ["restart", "log_delivery"])
def test_usb_identity_or_recipient_change_clears_armed_approval(monkeypatch: Any, part: str):
    panel, api, clock, _ = panel_fixture(monkeypatch, restart_card())
    view = panel.views["job-a"]
    view._arm_plan()
    row = api.data["cards"][0]
    plan = row["review"]["plan"]
    if part == "restart":
        plan[part]["stable_usb_fingerprint"] = "a" * 64
    else:
        plan[part]["principal_id"] = "key:" + "c" * 32
    row["plan_hash"] = row["review"]["plan_hash"] = digest(plan)
    refresh(panel.controller, api, clock)
    panel._render()
    view._approve()
    assert panel.worker.commands.empty()
    assert view.plan_activation is None


@pytest.mark.parametrize("invalidated", ["expired", "stale", "closed"])
def test_restart_approval_requires_fresh_visible_review(monkeypatch: Any, invalidated: str):
    panel, api, clock, _ = panel_fixture(monkeypatch, restart_card())
    view = panel.views["job-a"]
    view._arm_plan()
    if invalidated == "expired":
        clock.value += 31
    elif invalidated == "stale":
        api.fail.add("timeline")
        refresh(panel.controller, api, clock)
    else:
        view._toggle_card()
    panel._render()
    view._approve()
    assert panel.worker.commands.empty()
    assert all(call[0] != "approve" for call in api.calls)


@pytest.mark.parametrize(
    "stage,expected,receiving",
    [
        ("serial_open", "Receive is not active", False),
        ("restart_and_observe", "Preparing the one-shot reset", False),
        ("serial_restart_requested", "Reset control sequence requested", False),
        ("serial_restart_complete", "Reset controls completed", False),
        ("serial_reconnect_wait", "Waiting for the same USB identity", False),
        ("serial_reopened", "Same USB identity reopened once", False),
        ("firmware_observe", "Reset controls completed", True),
        ("serial_close", "Receive is not active", False),
    ],
)
def test_stage_labels_distinguish_reset_controls_from_receiving(
    monkeypatch: Any, stage: str, expected: str, receiving: bool
):
    row = restart_card() | {"review": None, "state": "running", "stage": stage}
    panel, _, _, _ = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert expected in view.next_var.get() and expected in view.summary_var.get()
    assert ("Serial receive started" in view.next_var.get()) is receiving
    assert ("Serial receive started" in view.summary_var.get()) is receiving
    assert "No commands sent" not in view.summary_var.get()


@pytest.mark.parametrize("raw,expected", [(STARTUP, "observed"), (b"quiet\n", "unknown")])
@pytest.mark.parametrize("capture_gap", [False, True])
def test_completed_restart_does_not_attest_reset_or_boot(
    monkeypatch: Any, raw: bytes, expected: str, capture_gap: bool
):
    panel, _, _, _ = panel_fixture(monkeypatch, completed_restart(raw, capture_gap=capture_gap))
    view = panel.views["job-a"]
    view._toggle_card()
    assert (
        "Boot unverified" in view.next_var.get() and "Firmware " + expected in view.next_var.get()
    )
    text = view.summary_var.get()
    assert "Reset control sequence issued: yes" in text
    assert "Hardware reset and application boot remain unverified" in text
    assert "ROM download-mode" in text
    assert "Firmware identity: " + expected in text
    assert "Retrieval by the client is not confirmed" in text
    assert ("Capture gap: USB was reopened" in text) is capture_gap
    if not capture_gap:
        assert "does not guarantee complete boot logs" in text
    assert view.image_panel.hidden and view.image_actions.hidden


def test_failed_restart_keeps_review_required_then_reviewed(monkeypatch: Any):
    row = restart_card() | {"review": None, "state": "needs_review", "reviewed": False}
    panel, api, clock, _ = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert view.next_var.get() == "Review device state before continuing"
    assert "reset and device state are unknown" in view.summary_var.get()
    api.data["cards"][0]["reviewed"] = True
    refresh(panel.controller, api, clock)
    panel._render()
    assert "Failure reviewed" in view.next_var.get()
    assert panel.worker.commands.empty()


def test_expired_restart_report_does_not_extend_on_refresh_or_erase_completion():
    model, api, clock = loaded(completed_restart())
    expires = model.card("job-a").device_observation.expires_at
    clock.value += 100
    refresh(model, api, clock)
    assert model.card("job-a").device_observation.expires_at == expires
    clock.value = expires
    assert firmware_observation_text(model.card("job-a"), clock()) == EXPIRED_OBSERVATION
    assert model.card("job-a").state == "succeeded"


def test_stale_restart_metadata_hides_identity_and_current_availability(monkeypatch: Any):
    panel, api, clock, _ = panel_fixture(monkeypatch, completed_restart())
    api.fail.add("timeline")
    refresh(panel.controller, api, clock)
    panel._render()
    text = panel.views["job-a"].summary_var.get()
    assert "availability is unconfirmed" in text and "0123456789abcdef" not in text


@pytest.mark.parametrize("key", ["reset_sequence_issued", "capture_gap"])
@pytest.mark.parametrize("value", ["yes", 1, None])
def test_restart_metadata_flags_are_strict_booleans(key: str, value: Any):
    row = completed_restart()
    row["device_observation"]["metadata"][key] = value
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(row), 0)


def test_projection_adds_backward_compatible_flags_without_device_text():
    data = observed_row()
    data["metadata"].pop("reset_sequence_issued", None)
    data["metadata"].pop("capture_gap", None)
    data["metadata"]["restart"] = "PRIVATE_RAW_LOG"
    clean = observation_summary(data, "job-a")
    assert clean["metadata"]["reset_sequence_issued"] is False
    assert clean["metadata"]["capture_gap"] is False
    assert "PRIVATE_RAW_LOG" not in json.dumps(clean)


@pytest.mark.parametrize(
    "changes",
    [
        {"test": "firmware_observe"},
        {"result": "firmware_observation_completed_no_commands"},
        {"state": "running"},
        {"backend": "mock"},
    ],
)
def test_restart_metadata_requires_its_exact_result_pair(changes: dict[str, Any]):
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(completed_restart() | changes), 0)


def synthetic_restart_backend() -> SerialBackend:
    port = PortInfo(port="COM42", vid=0x303A, pid=0x1001, serial_number="SYNTHETIC-ONLY")
    config = SerialSettings(
        selector=DeviceSelector(vid=port.vid, pid=port.pid, serial_number=port.serial_number)
    )

    def never_open() -> Any:
        raise AssertionError("GUI/timeline must not open or reset hardware")

    return SerialBackend(config, inventory=lambda: [port], factory=never_open)


async def test_operator_restart_stays_manual_and_projects_authorized_metadata(tmp_path: Any):
    device = synthetic_restart_backend()
    async with running(tmp_path, backend=device) as (broker, plans, timeline, clock):
        plan = prepare(broker, test=Action.RESTART)
        pending = timeline.snapshot()["cards"][0]
        assert pending["state"] == "approval_required" and pending["review"] is not None
        assert pending["review"]["attestation"] == "none"
        review = PlanReview.parse(pending["review"] | {"ok": True}, 0)
        assert "No automatic policy approval" in review_warning(review)
        assert device.calls == 0
        plans.approve(plan["plan_hash"], pending["review"]["review_revision"], "")
        assert timeline.snapshot()["cards"][0]["state"] == "approved"
        assert device.calls == 0
        collector = DeviceLogCollector()
        collector.feed(STARTUP + b"PRIVATE_RAW_LOG\n")
        observation = collector.finish()
        store = broker._store()
        store.add_job("one", plan["plan_hash"])
        store.set_job("one", "succeeded", "restart_observation_completed_unverified")
        broker.device_observations["one"] = RetainedDeviceObservation(
            "synthetic-owner", "local_cli", observation, clock.now + 100
        )
        result = timeline.snapshot()
        assert result["cards"][0]["device_observation"]["metadata"]["retained_lines"] == 6
        assert "PRIVATE_RAW_LOG" not in json.dumps(result)
        assert device.calls == 0
        clock.now += 100
        row = timeline.snapshot()["cards"][0]
        assert row["state"] == "succeeded" and "device_observation" not in row


async def test_operator_restart_rejects_recipient_substitution(tmp_path: Any):
    device = synthetic_restart_backend()
    async with running(tmp_path, backend=device) as (broker, _, timeline, _):
        plan = prepare(broker, test=Action.RESTART)
        store = broker._store()
        store.add_job("one", plan["plan_hash"])
        store.set_job("one", "succeeded", "restart_observation_completed_unverified")
        data = completed_restart()["device_observation"] | {"job_id": "one"}
        broker.operator_device_observation_metadata = lambda job: copy.deepcopy(data)
        row = timeline.snapshot()["cards"][0]
        assert row["state"] == "succeeded" and "device_observation" not in row
