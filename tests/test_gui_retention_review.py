"""Retained GUI security behavior survives removal of the approval controls."""

import copy
from dataclasses import replace

import pytest
from test_gui_timeline import Clock, FakeAPI, card, snapshot

from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.gui_timeline import (
    EXPIRED_OBSERVATION,
    TimelineController,
    TimelineSnapshot,
    firmware_observation_text,
    perform_timeline,
)


def observed_card(*, restart=False):
    collector = DeviceLogCollector()
    collector.feed(b"I (1) app_init: Project name: demo\n")
    report = replace(collector.finish(), reset_sequence_issued=restart)
    return card(
        backend="serial",
        test="restart_and_observe" if restart else "firmware_observe",
        result=("restart_observation_completed_unverified" if restart
                else "firmware_observation_completed_no_commands"),
        device_observation={
            "job_id": "job_image_1",
            "sha256": report.sha256,
            "recipient_principal": "synthetic-client",
            "remaining_seconds": 200,
            "metadata": report.metadata(),
        },
    )


@pytest.mark.parametrize("restart", [False, True])
def test_same_observation_refresh_never_extends_original_metadata_retention(restart):
    clock, api = Clock(), FakeAPI()
    model = TimelineController(clock)
    api.status = snapshot([observed_card(restart=restart)])
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    first_expiry = model.snapshot.cards[0].device_observation.expires_at
    clock.value += 100
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert model.snapshot.cards[0].device_observation.expires_at == first_expiry
    clock.value = first_expiry
    assert firmware_observation_text(model.snapshot.cards[0], clock()) == EXPIRED_OBSERVATION


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


@pytest.mark.parametrize("kind", ["image", "observation", "restart"])
def test_same_epoch_disappearance_cannot_refresh_a_reappearing_identity(kind):
    clock, api = Clock(), FakeAPI()
    model = TimelineController(clock)
    if kind != "image":
        api.status = snapshot([observed_card(restart=kind == "restart")])
    original = copy.deepcopy(api.status)
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    attribute = "image" if kind == "image" else "device_observation"
    first_expiry = getattr(model.card("job_image_1"), attribute).expires_at
    api.status = snapshot([])
    clock.value += 50
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert model.card("job_image_1") is None
    api.status = original
    clock.value += 50
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert getattr(model.card("job_image_1"), attribute).expires_at == first_expiry


@pytest.mark.parametrize("kind", ["image", "observation", "restart"])
def test_new_epoch_resets_retention_identity_and_clears_old_pixels(kind):
    clock, api = Clock(), FakeAPI()
    model = TimelineController(clock)
    if kind != "image":
        api.status = snapshot([observed_card(restart=kind == "restart")])
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    attribute = "image" if kind == "image" else "device_observation"
    first_expiry = getattr(model.card("job_image_1"), attribute).expires_at
    if kind == "image":
        assert model.accept(perform_timeline(api, model.load_preview("job_image_1"), clock))
        assert model.preview is not None
    api.status["broker_epoch"] = "f" * 32
    clock.value += 100
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert getattr(model.card("job_image_1"), attribute).expires_at == first_expiry + 100
    assert model.preview is None and model.generation == 1


def test_same_report_hash_cannot_refresh_changed_metadata_as_current():
    clock, api = Clock(), FakeAPI()
    model = TimelineController(clock)
    api.status = snapshot([observed_card()])
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    api.status["cards"][0]["device_observation"]["metadata"]["bytes_received"] += 1
    clock.value += 10
    model.accept(perform_timeline(api, model.refresh(), clock))
    current = model.card("job_image_1")
    assert not model.fresh or current is None or current.device_observation is None


@pytest.mark.parametrize("kind", ["image", "observation"])
def test_retention_identity_capacity_fails_closed_without_evicting_prior_deadline(monkeypatch, kind):
    monkeypatch.setattr("cardputer_mcp.gui_timeline.MAX_RETENTION_IDENTITIES", 1)
    clock, api = Clock(), FakeAPI()
    model = TimelineController(clock)
    if kind == "observation":
        api.status = snapshot([observed_card()])
    original = copy.deepcopy(api.status)
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    attribute = "image" if kind == "image" else "device_observation"
    first_expiry = getattr(model.card("job_image_1"), attribute).expires_at
    changed = copy.deepcopy(original)
    changed["cards"][0]["job_id"] = "job_image_2"
    changed["cards"][0][attribute]["job_id"] = "job_image_2"
    api.status = changed
    clock.value += 10
    model.accept(perform_timeline(api, model.refresh(), clock))
    assert not model.fresh
    api.status = original
    clock.value += 10
    assert model.accept(perform_timeline(api, model.refresh(), clock))
    assert model.fresh
    assert getattr(model.card("job_image_1"), attribute).expires_at == first_expiry


@pytest.mark.parametrize("field,value", [
    ("reset_sequence_issued", 1), ("capture_gap", "yes"),
    ("partial", None), ("bytes_received", 65537), ("retained_lines", 257),
])
def test_report_metadata_parser_still_rejects_unbounded_or_nonstrict_fields(field, value):
    row = observed_card()
    row["device_observation"]["metadata"][field] = value
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
