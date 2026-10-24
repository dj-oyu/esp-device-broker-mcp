"""Durable per-job log store and the passive serial tee; synthetic bytes only."""

import json
import os

import pytest

from cardputer_mcp import job_logs
from cardputer_mcp.job_logs import (
    TRUNCATION_MARKER,
    DeviceLogTap,
    JobLogError,
    JobLogStore,
)
from cardputer_mcp.serial_backend import Reader, SerialFailure

OWNER, OTHER = "key:owner", "key:other"
SECRET = "SYNTHETIC_PASSWORD_VALUE"


def lines(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_header_entries_and_write_time_redaction(tmp_path):
    store = JobLogStore(tmp_path / "job-logs")
    log = store.begin("job-1", OWNER, "firmware_observe")
    log.redactions = ("C:/private/project",)
    log.append("device", "built in C:/private/project")
    log.append("build", f"password={SECRET}")
    log.close()
    header, *entries = lines(tmp_path / "job-logs" / "job-1.jsonl")
    assert header["schema_version"] == 1
    assert {header["job_id"], header["principal_id"], header["kind"]} == {
        "job-1",
        OWNER,
        "firmware_observe",
    }
    assert [entry["seq"] for entry in entries] == [1, 2]
    assert set(entries[0]) == {"seq", "t", "stream", "level", "text"}
    raw = (tmp_path / "job-logs" / "job-1.jsonl").read_text("utf-8")
    assert SECRET not in raw and "C:/private/project" not in raw


@pytest.mark.parametrize("job_id", ["../escape", "a/b", "", "x" * 65, "bad id"])
def test_refuses_non_identifier_job_ids(tmp_path, job_id):
    store = JobLogStore(tmp_path / "job-logs")
    with pytest.raises(JobLogError):
        store.begin(job_id, OWNER, "observe")
    assert store.read(job_id, OWNER, "device") is None


def test_refuses_symlinked_log_file(tmp_path):
    root = tmp_path / "job-logs"
    root.mkdir()
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    try:
        os.symlink(target, root / "job-1.jsonl")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted")
    with pytest.raises(JobLogError):
        JobLogStore(root).begin("job-1", OWNER, "observe")
    assert target.read_text() == ""


def test_per_job_cap_writes_one_marker_then_drops(tmp_path, monkeypatch):
    monkeypatch.setattr(job_logs, "MAX_JOB_BYTES", 600)
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    for index in range(50):
        log.append("device", f"line {index}")
    log.close()
    texts = [entry["text"] for entry in lines(tmp_path / "job-1.jsonl")[1:]]
    assert texts[-1] == TRUNCATION_MARKER and texts.count(TRUNCATION_MARKER) == 1
    assert log.truncated and store.device_report("job-1", OWNER)["size_limit_reached"]


def test_prune_oldest_first_never_touches_kept_jobs(tmp_path, monkeypatch):
    root = tmp_path / "job-logs"
    store = JobLogStore(root)
    for index in range(4):
        store.begin(f"job-{index}", OWNER, "observe").close()
        os.utime(root / f"job-{index}.jsonl", ns=(index * 10**9, index * 10**9))
    (root / "unrelated.txt").write_text("keep")
    monkeypatch.setattr(job_logs, "MAX_JOB_FILES", 2)
    store.begin("job-new", OWNER, "observe", keep={"job-0"}).close()
    remaining = sorted(path.name for path in root.iterdir())
    # job-0 is still queued/running, so the newest others fill the cap.
    assert remaining == ["job-0.jsonl", "job-3.jsonl", "job-new.jsonl", "unrelated.txt"]


def test_entries_written_before_an_exception_survive(tmp_path):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    with pytest.raises(RuntimeError):
        log.append("device", "before crash")
        raise RuntimeError("synthetic crash")
    # No close/fsync: a fresh reader still sees the flushed line.
    assert store.device_report("job-1", OWNER)["logs"] == ["before crash"]
    log.close()


def test_owner_only_read_and_append_never_raises(tmp_path):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    log.append("device", "hello")
    log.append("not-a-stream", "dropped")
    assert log.unavailable
    log.close()
    log.append("device", "after close is ignored")
    assert store.device_report("job-1", OTHER) is None
    assert store.device_report("job-1", OWNER)["logs"] == ["hello"]
    with pytest.raises(JobLogError, match="owner_mismatch"):
        store.begin("job-1", OTHER, "observe")


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (b"I (1) app: plain\r\n", ["I (1) app: plain"]),
        (b"pass", []),  # unterminated until finish
        (b"password=" + SECRET.encode() + b"\n", ["[REDACTED: credential-like content]"]),
        (
            b"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\nafter\n",
            ["[REDACTED: credential-like content]"] * 3 + ["after"],
        ),
        (b"PIX 0 0 ffff\nCAPTURE_BEGIN\nok\n", ["ok"]),
        (b"\x1b[0;31mE (5) x: red\x1b[0m\n", ["E (5) x: red"]),
    ],
)
def test_device_tap_redacts_like_the_collector(tmp_path, wire, expected):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "observe")
    tap = DeviceLogTap(log)
    for index in range(len(wire)):
        tap.feed(wire[index : index + 1])  # arbitrary chunking
    if expected:
        assert store.device_report("job-1", OWNER)["logs"] == expected
    tap.finish()
    log.close()
    report = store.device_report("job-1", OWNER)
    if wire == b"pass":
        assert report["logs"] == ["pass"]
    assert SECRET not in (tmp_path / "job-1.jsonl").read_text("utf-8")


class _Port:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def read(self, size=1):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        return chunk


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        ([b"boot line\nGuru Meditation Error: Core 0 panic'ed\n"], "serial_fault_marker"),
        ([b"boot line\n", OSError("synthetic")], "serial_read_failed"),
        ([b"boot line\n", RuntimeError("synthetic")], "serial_operation_uncertain"),
    ],
)
def test_reader_tee_keeps_text_on_failure(tmp_path, chunks, code):
    store = JobLogStore(tmp_path)
    log = store.begin("job-1", OWNER, "hello_cycle")
    tap = DeviceLogTap(log)
    reader = Reader(_Port(chunks), lambda _: None, lambda: 0.0, 10.0)
    reader.device_tap = tap
    with pytest.raises(SerialFailure) as failure:
        for _ in range(5):
            reader.read_once()
    assert failure.value.code == code
    tap.finish()
    log.close()
    assert store.device_report("job-1", OWNER)["logs"][0] == "boot line"
