"""Build/source/flash output survives RAM expiry and restart in the durable job log."""

import json
import sqlite3

import pytest
import test_build_artifact_persistence as fixtures

from cardputer_mcp import job_logs
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.job_logs import JobLogStore, attach
from cardputer_mcp.models import BuildLogRequest, LogCursor

PRIVATE = "C:/synthetic/private/project"
SECRET = "SYNTHETIC_TOKEN_VALUE"


def dump(root):
    """Everything the job-log database holds as text, to check nothing sensitive was stored."""
    connection = sqlite3.connect(root / job_logs.DATABASE_NAME)
    try:
        return "\n".join(connection.iterdump())
    finally:
        connection.close()


def teed(store, stream, principal="alice", job_id="job-1"):
    logs = BuildLogs(principal, job_id=job_id)
    logs.redactions = (PRIVATE,)
    attach(
        logs, lambda j, p, k: store.begin(j, p, k), job_id, principal, "firmware_" + stream, stream
    )
    return logs


@pytest.mark.parametrize("stream", ["build", "source", "flash"])
@pytest.mark.parametrize("ram", ["expired", "cleared"])
def test_stream_readable_after_ram_loss_with_cursor_semantics(tmp_path, stream, ram):
    store = JobLogStore(tmp_path)
    logs = teed(store, stream)
    logs.append("info: start in " + PRIVATE, console_output=False)
    logs.append(f"error: failed token={SECRET}", console_output=False)
    logs.append("[1/2] progress", console_output=False)
    logs.append("warning: tail", console_output=False)
    if ram == "expired":
        logs.deadline = 0
        logs.expire()
    else:
        logs.entries.clear()
    assert logs.entries == type(logs.entries)()
    raw = dump(tmp_path)
    assert PRIVATE not in raw and SECRET not in raw

    first = store.build_read("job-1", "alice", stream, BuildLogRequest(job_id="job-1", limit=2))
    assert first["source"] == "durable_job_log" and first["expired"] is False
    assert [entry["sequence"] for entry in first["entries"]] == [1, 2]
    assert first["has_more"] is True
    cursor = LogCursor(**first["next_cursor"])
    rest = store.build_read(
        "job-1", "alice", stream, BuildLogRequest(job_id="job-1", cursor=cursor)
    )
    assert [entry["text"] for entry in rest["entries"]] == ["[1/2] progress", "warning: tail"]
    assert rest["has_more"] is False and rest["gap"] is False
    ahead = LogCursor(epoch=cursor.epoch, sequence=99)
    with pytest.raises(ValueError, match="cursor_ahead"):
        store.build_read("job-1", "alice", stream, BuildLogRequest(job_id="job-1", cursor=ahead))
    assert store.build_read("job-1", "bob", stream, BuildLogRequest(job_id="job-1")) is None
    other = "device" if stream != "device" else "build"
    assert store.build_read("job-1", "alice", other, BuildLogRequest(job_id="job-1")) is None
    empty = store.build_read(
        "job-1", "alice", other, BuildLogRequest(job_id="job-1"), allow_empty=True
    )
    assert empty["entries"] == [] and empty["next_cursor"]["sequence"] == 0
    assert (
        store.build_read("job-1", "bob", other, BuildLogRequest(job_id="job-1"), allow_empty=True)
        is None
    )
    assert (
        store.build_read("job-2", "alice", other, BuildLogRequest(job_id="job-2"), allow_empty=True)
        is None
    )


@pytest.fixture
def receipt():
    case = fixtures.DurableBuildArtifactTests()
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


@pytest.mark.parametrize("retention", ["restart", "expired"])
def test_firmware_build_read_logs_falls_back_to_durable(receipt, tmp_path, retention):
    store = JobLogStore(tmp_path / "job-logs")
    logs = teed(store, "build", job_id="build-0")
    logs.append("error: synthetic compiler failure", console_output=False)
    logs.durable.close()
    if retention == "restart":
        receipt.restart()
    else:
        logs.deadline = 0
        receipt.builder.logs["build-0"] = logs
    receipt.builder.job_logs = store
    result = receipt.builder.read_logs(BuildLogRequest(job_id="build-0"), "alice")
    assert result["source"] == "durable_job_log"
    assert [entry["text"] for entry in result["entries"]] == ["error: synthetic compiler failure"]
    if retention == "restart":
        # Another principal gets the RAM-absent answer, never the durable text.
        denied = receipt.builder.read_logs(BuildLogRequest(job_id="build-0"), "bob")
        assert denied["expired"] is True and "synthetic" not in json.dumps(denied)
