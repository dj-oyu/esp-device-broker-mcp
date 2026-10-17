"""Firmware observation GUI and timeline metadata tests; no hardware or network."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from typing import Any

import pytest
from test_gui_timeline import (
    FakeAPI,
    card,
    loaded,
    panel_fixture,
    refresh,
    snapshot,
)
from test_operator_timeline import prepare, running
from test_serial_backend import backend

from cardputer_mcp.broker import RetainedDeviceObservation
from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.gui_approvals import PlanReview, review_warning
from cardputer_mcp.gui_timeline import (
    EXPIRED_OBSERVATION,
    INVALID_TIMELINE,
    TimelineOperation,
    TimelineSnapshot,
    TimelineWorker,
    firmware_observation_text,
    perform_timeline,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_timeline import observation_summary

PRINCIPAL = "key:" + "a" * 32
STARTUP = (
    b"I (1) app_init: Application information:\n"
    b"I (1) app_init: Project name:     demo\n"
    b"I (1) app_init: App version:      1.2.3\n"
    b"I (1) app_init: ELF file SHA256:  0123456789abcdef...\n"
    b"I (1) app_init: ESP-IDF:          v6.0.1\n"
)


def digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def firmware_card() -> dict[str, Any]:
    result = card(backend="serial", test="firmware_observe")
    plan = result["review"]["plan"]
    plan["observe_seconds"] = 30
    plan["expected_firmware_sha256"] = "b" * 64
    plan["log_delivery"] = {
        "principal_id": PRINCIPAL,
        "origin": "remote_mcp",
        "retention_seconds": 300,
        "max_received_bytes": 65536,
        "max_lines": 256,
        "max_line_bytes": 1024,
        "max_rendered_bytes": 32768,
    }
    result["plan_hash"] = result["review"]["plan_hash"] = digest(plan)
    return result


def observed_row(data: bytes = STARTUP) -> dict[str, Any]:
    collector = DeviceLogCollector()
    collector.feed(data)
    observation = collector.finish()
    return {
        "job_id": "job-a",
        "sha256": observation.sha256,
        "recipient_principal": PRINCIPAL,
        "remaining_seconds": 200,
        "metadata": observation.metadata(),
    }


def completed_firmware(data: bytes = STARTUP) -> dict[str, Any]:
    return firmware_card() | {
        "state": "succeeded",
        "review": None,
        "stage": "serial_close",
        "result": "firmware_observation_completed_no_commands",
        "device_observation": observed_row(data),
    }


def test_same_approval_visibly_authorizes_named_recipient_and_never_sends_itself(monkeypatch: Any):
    panel, api, clock, _ = panel_fixture(monkeypatch, firmware_card())
    view = panel.views["job-a"]
    assert view.expanded and not view.details_expanded
    text = view.summary_var.get()
    for required in (
        "30 seconds",
        "No writes",
        "reset command",
        "firmware flash",
        "automatic reconnect",
        "OS/driver",
        "private data",
        "credential patterns",
        "not guaranteed",
        PRINCIPAL,
        "No additional send approval",
        "five minutes",
        "not evidence",
    ):
        assert required in text
    assert "Approve observation and delivery" == view.approve_button.config["text"]
    assert not hasattr(view, "attestation")  # The current UI displays a fixed phrase, not an entry.
    assert view.attestation_var.get() == "" and view.home_line_opt_in.hidden
    assert view.phrase.config["text"] == "Approve this plan only"
    assert view.image_panel.hidden and view.image_actions.hidden
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
    "key,value",
    [
        ("principal_id", "bad\nrecipient"),
        ("principal_id", None),
        ("principal_id", "a" * 129),
        ("origin", "unknown"),
        ("retention_seconds", 301),
        ("max_received_bytes", 65537),
        ("max_lines", True),
        ("max_line_bytes", 2048),
        ("max_rendered_bytes", 65536),
    ],
)
def test_review_rejects_unbounded_or_untrusted_delivery(key: str, value: Any):
    data = firmware_card()["review"]
    data["plan"]["log_delivery"][key] = value
    data["plan_hash"] = digest(data["plan"])
    with pytest.raises(ValueError, match="invalid_plan_response"):
        PlanReview.parse(data | {"ok": True}, 1000)


@pytest.mark.parametrize("duration", [True, 29, 31, "30", None])
def test_review_rejects_nonfixed_duration(duration: Any):
    data = firmware_card()["review"]
    data["plan"]["observe_seconds"] = duration
    data["plan_hash"] = digest(data["plan"])
    with pytest.raises(ValueError, match="invalid_plan_response"):
        PlanReview.parse(data | {"ok": True}, 1000)


def test_standalone_review_has_same_delivery_warning():
    review = PlanReview.parse(firmware_card()["review"] | {"ok": True}, 1000)
    assert PRINCIPAL in review_warning(review)
    assert "This click permits report delivery" in review_warning(review)


def test_changed_recipient_invalidates_armed_approval(monkeypatch: Any):
    panel, api, clock, _ = panel_fixture(monkeypatch, firmware_card())
    view = panel.views["job-a"]
    view._arm_plan()
    row = api.data["cards"][0]
    row["review"]["plan"]["log_delivery"]["principal_id"] = "key:" + "c" * 32
    row["plan_hash"] = row["review"]["plan_hash"] = digest(row["review"]["plan"])
    refresh(panel.controller, api, clock)
    panel._render()
    view._approve()
    assert panel.worker.commands.empty()
    assert "key:" + "c" * 32 in view.summary_var.get()


@pytest.mark.parametrize(
    "state,stage,started",
    [
        ("queued", None, False),
        ("running", "serial_open", False),
        ("running", "firmware_observe", True),
        ("running", "serial_close", False),
    ],
)
def test_receive_status_follows_actual_stage(
    monkeypatch: Any, state: str, stage: str, started: bool
):
    row = firmware_card() | {"review": None, "state": state, "stage": stage}
    panel, _, _, _ = panel_fixture(monkeypatch, row)
    view = panel.views["job-a"]
    assert ("Serial receive started" in view.next_var.get()) is started
    assert ("Serial receive started" in view.summary_var.get()) is started


@pytest.mark.parametrize(
    "raw,status",
    [
        (STARTUP, "observed"),
        (STARTUP.split(b"ELF file")[0], "partial"),
        (b"running quietly\n", "unavailable"),
        (STARTUP + STARTUP, "unavailable (ambiguous"),
    ],
)
def test_finished_identity_is_observed_partial_or_unavailable(
    monkeypatch: Any, raw: bytes, status: str
):
    panel, _, _, _ = panel_fixture(monkeypatch, completed_firmware(raw))
    view = panel.views["job-a"]
    assert "Firmware " + status in view.next_var.get()
    view._toggle_card()
    text = view.summary_var.get()
    assert "Firmware identity:" in text and "bytes inspected" in text and "lines retained" in text
    assert "Retrieval by the client is not confirmed" in text
    assert "b" * 64 not in text
    if status == "observed":
        assert "ELF SHA-256 prefix (not a .bin hash): 0123456789abcdef" in text
        assert "unverified" in text
    assert view.image_panel.hidden and view.image_actions.hidden


def test_expired_and_missing_report_preserve_completed_outcome(monkeypatch: Any):
    panel, api, clock, _ = panel_fixture(monkeypatch, completed_firmware())
    clock.value += 200
    panel._render()
    view = panel.views["job-a"]
    assert view.summary_var.get() == EXPIRED_OBSERVATION
    assert "Observation completed" in view.next_var.get()
    assert "ELF SHA-256 prefix" not in view.summary_var.get()
    api.data["cards"][0].pop("device_observation")
    refresh(panel.controller, api, clock)
    panel._render()
    assert view.summary_var.get() == EXPIRED_OBSERVATION
    assert panel.controller.card("job-a").state == "succeeded"


def test_refresh_cannot_extend_same_observation_expiry():
    model, api, clock = loaded(completed_firmware())
    first = model.card("job-a").device_observation.expires_at
    clock.value += 100
    refresh(model, api, clock)
    assert model.card("job-a").device_observation.expires_at == first
    clock.value = first
    assert firmware_observation_text(model.card("job-a"), clock()) == EXPIRED_OBSERVATION


def test_stale_timeline_does_not_claim_current_metadata_available(monkeypatch: Any):
    panel, api, clock, _ = panel_fixture(monkeypatch, completed_firmware())
    api.fail.add("timeline")
    refresh(panel.controller, api, clock)
    panel._render()
    text = panel.views["job-a"].summary_var.get()
    assert "availability is unconfirmed" in text
    assert "Metadata available" not in text and "0123456789abcdef" not in text


def test_projection_never_copies_logs_or_arbitrary_extension_strings():
    data = observed_row()
    data["logs"] = ["PRIVATE_RAW_LOG"]
    data["metadata"]["logs"] = ["PRIVATE_RAW_LOG"]
    data["metadata"]["redaction_limitations"] = "PRIVATE_RAW_LOG"
    data["metadata"]["firmware"]["unexpected"] = "PRIVATE_RAW_LOG"
    data["metadata"]["firmware"]["limitations"] = "PRIVATE_RAW_LOG"
    clean = observation_summary(data, "job-a")
    assert "PRIVATE_RAW_LOG" not in json.dumps(clean)
    parsed = TimelineSnapshot.parse(
        snapshot(completed_firmware() | {"device_observation": data}), 0
    )
    assert "PRIVATE_RAW_LOG" not in json.dumps(parsed.cards[0].device_observation.metadata)


@pytest.mark.parametrize(
    "path,value",
    [
        ("job_id", "other"),
        ("remaining_seconds", 301),
        ("remaining_seconds", True),
        ("recipient_principal", "bad\nrecipient"),
        ("metadata.bytes_received", 65537),
        ("metadata.retained_lines", 257),
        ("metadata.rendered_bytes", True),
        ("metadata.partial", "yes"),
        ("metadata.stop_reason", "untrusted"),
        ("metadata.firmware.project_name", "bad\nproject"),
        ("metadata.firmware.elf_sha256_prefix", "a" * 64),
        ("metadata.firmware.source_verified", True),
        ("metadata.firmware.status", "proven"),
    ],
)
def test_invalid_metadata_disables_timeline(path: str, value: Any):
    row = completed_firmware()
    target = row["device_observation"]
    parts = path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(row), 0)


@pytest.mark.parametrize(
    "change", [{"state": "running"}, {"test": "capture_home"}, {"backend": "mock"}]
)
def test_metadata_cannot_attach_to_different_kind_of_card(change: dict[str, Any]):
    with pytest.raises(ValueError, match=INVALID_TIMELINE):
        TimelineSnapshot.parse(snapshot(completed_firmware() | change), 0)


def test_metadata_request_runs_only_on_worker_thread():
    api = FakeAPI(completed_firmware())
    worker = TimelineWorker(api)
    worker.commands.put(TimelineOperation(1, 0, "refresh", time.monotonic()))
    try:
        outcome = worker.results.get(timeout=2)
        assert outcome.snapshot.cards[0].device_observation is not None
        assert api.calls == [("timeline",)]
        assert api.threads == [worker.thread.ident]
        assert worker.thread.ident != threading.get_ident()
    finally:
        worker.stop()
        worker.thread.join(timeout=2)


async def test_operator_timeline_metadata_is_optional_authorized_and_never_contains_logs(
    tmp_path: Any,
):
    device, port = backend()
    async with running(tmp_path, backend=device) as (broker, _, timeline, clock):
        plan = prepare(broker, test=Action.FIRMWARE)
        collector = DeviceLogCollector()
        collector.feed(STARTUP + b"PRIVATE_RAW_LOG\n")
        observation = collector.finish()
        store = broker._store()
        store.add_job("one", plan["plan_hash"])
        store.set_job("one", "succeeded", "firmware_observation_completed_no_commands")
        broker.device_observations["one"] = RetainedDeviceObservation(
            "synthetic-owner", "local_cli", observation, clock.now + 100
        )
        result = timeline.snapshot()
        row = result["cards"][0]
        assert row["device_observation"]["metadata"]["retained_lines"] == 6
        assert "PRIVATE_RAW_LOG" not in json.dumps(result)
        assert "logs" not in row["device_observation"]["metadata"]
        assert port.opens == 0 and not port.writes
        broker.principal_valid = lambda *args: False
        row = timeline.snapshot()["cards"][0]
        assert row["state"] == "succeeded" and "device_observation" not in row
        assert not broker.device_observations


async def test_operator_timeline_rejects_recipient_substitution_and_unsafe_metadata(tmp_path: Any):
    device, _ = backend()
    async with running(tmp_path, backend=device) as (broker, _, timeline, _):
        plan = prepare(broker, test=Action.FIRMWARE)
        store = broker._store()
        store.add_job("one", plan["plan_hash"])
        store.set_job("one", "succeeded", "firmware_observation_completed_no_commands")
        data = observed_row() | {"job_id": "one"}
        broker.operator_device_observation_metadata = lambda job: copy.deepcopy(data)
        assert "device_observation" not in timeline.snapshot()["cards"][0]
        data["recipient_principal"] = "synthetic-owner"
        data["metadata"]["firmware"]["project_name"] = "unsafe\nproject"
        row = timeline.snapshot()["cards"][0]
        assert row["state"] == "succeeded" and "device_observation" not in row
