"""Retained GUI security behavior survives removal of the approval controls."""

from dataclasses import replace

import pytest
from test_gui_timeline import Clock, FakeAPI, card, snapshot

from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.gui_timeline import (
    TimelineController,
    TimelineSnapshot,
    perform_timeline,
)


def observed_card(*, restart=False):
    collector = DeviceLogCollector()
    collector.feed(b"I (1) app_init: Project name: demo\n")
    report = replace(collector.finish(), reset_sequence_issued=restart)
    return card(
        backend="serial",
        test="restart_and_observe" if restart else "firmware_observe",
        result=(
            "restart_observation_completed_unverified"
            if restart
            else "firmware_observation_completed_no_commands"
        ),
        device_observation={
            "job_id": "job_image_1",
            "sha256": report.sha256,
            "recipient_principal": "synthetic-client",
            "remaining_seconds": 200,
            "metadata": report.metadata(),
        },
    )


def test_same_image_refresh_never_extends_original_retention_before_preview():
    clock, api = Clock(), FakeAPI()
    model = TimelineController(clock)
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    first_expiry = model.snapshot.cards[0].image.expires_at
    clock.value += 100
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert model.snapshot.cards[0].image.expires_at == first_expiry
    clock.value = first_expiry
    assert model.load_preview("job_image_1") is None
    assert api.calls == [("timeline",), ("timeline",)]


@pytest.mark.parametrize("mutation", ["result", "reset_flag", "capture_gap"])
def test_observation_metadata_cannot_claim_a_different_operation(mutation):
    row = observed_card()
    if mutation == "result":
        row["result"] = "restart_observation_completed_unverified"
    elif mutation == "reset_flag":
        row["device_observation"]["metadata"]["reset_sequence_issued"] = True
    else:
        row["device_observation"]["metadata"]["capture_gap"] = True
    with pytest.raises(ValueError):
        TimelineSnapshot.parse(snapshot([row]), 1000)


def test_report_projection_never_copies_raw_device_text_or_unknown_extensions():
    row = observed_card()
    data = row["device_observation"]
    data["logs"] = ["SYNTHETIC_PRIVATE_DEVICE_TEXT"]
    data["metadata"]["logs"] = ["SYNTHETIC_PRIVATE_DEVICE_TEXT"]
    data["metadata"]["firmware"]["extra"] = "SYNTHETIC_PRIVATE_DEVICE_TEXT"
    parsed = TimelineSnapshot.parse(snapshot([row]), 1000)
    report = parsed.cards[0].device_observation
    assert "SYNTHETIC_PRIVATE_DEVICE_TEXT" not in str(report.metadata)
