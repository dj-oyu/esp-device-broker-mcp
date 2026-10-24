"""Bounded private fan-out keeps control evidence separate from console flood."""

from unittest.mock import patch

import pytest

from cardputer_mcp.app_events import AppEvents
from cardputer_mcp.app_models import AppEventCursor, AppEventsRequest


def test_log_flood_does_not_evict_ack_and_redacts_before_private_delivery():
    events = AppEvents("owner", 2048)
    events.emit("ack", command_id=2, result="consumed")
    for i in range(500):
        events.log(f"line {i}: password=synthetic-secret " + "x" * 900)
    request = AppEventsRequest(job_id="job", limit=100)
    with patch("time.monotonic", return_value=100):
        first = events.read("owner", request)
        second = events.read("owner", request)
    assert first == second
    assert first["entries"] == [dict(sequence=1, kind="ack", command_id=2, result="consumed")]
    assert first["gap"] and first["dropped_count"]["log"] > 0
    assert first["dropped_count"]["control"] == 0
    assert events.logs.bytes <= 2048
    assert len(events.logs.entries) <= 256
    assert all("synthetic-secret" not in entry["text"] for entry in first["logs"])
    assert all("[REDACTED]" in entry["text"] for entry in first["logs"])
    assert first["log_attribution"] == "observed_during_run"
    assert all(entry["stream"] == "observed_during_run" for entry in first["logs"])


def test_principal_expiry_foreign_epoch_and_future_cursors():
    events = AppEvents("owner", 4096)
    events.emit("started", run_id=1)
    events.log("test output")
    with pytest.raises(ValueError, match="app_events_not_available"):
        events.read("someone-else", AppEventsRequest(job_id="job"))
    reset = events.read(
        "owner",
        AppEventsRequest(job_id="job", cursor=AppEventCursor(epoch="0" * 32, control=10, log=10)),
    )
    assert reset["gap"]
    assert reset["entries"][0]["kind"] == "started"
    for cursor in (
        AppEventCursor(epoch=events.epoch, control=2),
        AppEventCursor(epoch=events.epoch, log=2),
    ):
        with pytest.raises(ValueError, match="cursor_ahead"):
            events.read("owner", AppEventsRequest(job_id="job", cursor=cursor))
    events.deadline = events.logs.deadline = 0
    expired = events.read("owner", AppEventsRequest(job_id="job"))
    assert expired["expired"] and expired["gap"]
    assert expired["expiry_seconds"] == 0
    assert not expired["entries"] and not expired["logs"]
    events.expire()
    assert not events.control and not events.logs.entries and events.logs.bytes == 0


def test_partial_credentials_at_transport_and_output_boundaries_are_redacted():
    from cardputer_mcp.app_wire import Decoder

    token = "cpk_" + "a" * 32 + "." + "b" * 64
    events = AppEvents("owner", 262144)
    events.log("x" * 1000 + token)
    decoder = Decoder()
    _, lines = decoder.feed(("y" * 980 + token + "\n").encode())
    for line in lines:
        events.log(line)
    report = events.read("owner", AppEventsRequest(job_id="redaction"))
    assert len(report["logs"]) == 2
    assert all("cpk_" not in entry["text"] for entry in report["logs"])
    assert all(len(entry["text"].encode()) <= 1024 for entry in report["logs"])
