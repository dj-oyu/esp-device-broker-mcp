"""Build-log reads report what remains and can return a raw line range."""

import pytest
from pydantic import ValidationError
from surface_harness import REMOTE, serve

from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.client import call, connect
from cardputer_mcp.job_logs import JobLogStore, attach
from cardputer_mcp.models import BuildLogRequest, LogCursor

LINES = 30  # every 10th line is an error, so the filtered read selects 3 + context


def filled(store=None):
    logs = BuildLogs("alice", job_id="job-1")
    if store is not None:
        attach(
            logs,
            lambda j, p, k: store.begin(j, p, k),
            "job-1",
            "alice",
            "firmware_build",
            "build",
        )
    for number in range(1, LINES + 1):
        logs.append(
            ("error: " if number % 10 == 0 else "note: ") + f"line {number}", console_output=False
        )
    return logs


def readers(tmp_path):
    ram = filled()
    store = JobLogStore(tmp_path)
    filled(store)
    return {
        "ram": lambda request: ram.read("alice", request),
        "durable": lambda request: store.build_read("job-1", "alice", "build", request),
    }


@pytest.fixture(params=["ram", "durable"])
def read(request, tmp_path):
    return readers(tmp_path)[request.param]


def numbers(result):
    return [entry["sequence"] for entry in result["entries"]]


def test_filtered_read_reports_last_sequence_and_remaining(read):
    first = read(BuildLogRequest(job_id="job-1", context=0, limit=2))
    assert numbers(first) == [10, 20]
    assert first["last_sequence"] == LINES
    assert first["remaining"] == 1 and first["has_more"] is True
    rest = read(
        BuildLogRequest(
            job_id="job-1", context=0, limit=2, cursor=LogCursor(**first["next_cursor"])
        )
    )
    assert numbers(rest) == [30] and rest["remaining"] == 0


def test_range_returns_raw_lines_regardless_of_level(read):
    result = read(BuildLogRequest(job_id="job-1", from_line=3, to_line=6))
    assert numbers(result) == [3, 4, 5, 6]
    assert [entry["level"] for entry in result["entries"]][0] != "error"
    assert result["remaining"] == 0 and result["has_more"] is False
    assert result["last_sequence"] == LINES


def test_range_is_paged_by_limit_and_continues_from_the_next_line(read):
    first = read(BuildLogRequest(job_id="job-1", from_line=5, to_line=25, limit=8))
    assert numbers(first) == list(range(5, 13))
    assert first["remaining"] == 13 and first["has_more"] is True
    second = read(
        BuildLogRequest(
            job_id="job-1", from_line=first["next_cursor"]["sequence"] + 1, to_line=25, limit=100
        )
    )
    assert numbers(second) == list(range(13, 26)) and second["remaining"] == 0


def test_open_ended_range_runs_to_the_last_line(read):
    result = read(BuildLogRequest(job_id="job-1", from_line=28))
    assert numbers(result) == [28, 29, 30] and result["has_more"] is False


def test_range_past_the_end_is_empty_not_an_error(read):
    result = read(BuildLogRequest(job_id="job-1", from_line=99, to_line=120))
    assert result["entries"] == [] and result["remaining"] == 0 and result["has_more"] is False
    assert result["next_cursor"]["sequence"] <= LINES


def test_range_from_a_line_already_evicted_reports_a_gap():
    logs = BuildLogs("alice", max_lines=10)
    for number in range(1, 26):
        logs.append(f"note: {number}", console_output=False)
    result = logs.read("alice", BuildLogRequest(job_id="job-1", from_line=1, to_line=30))
    assert result["gap"] is True and result["oldest_sequence"] == 16
    assert numbers(result)[0] == 16


@pytest.mark.parametrize("fields", [{"to_line": 5}, {"from_line": 9, "to_line": 4}])
def test_inverted_or_lonely_range_is_rejected(fields):
    with pytest.raises(ValidationError):
        BuildLogRequest(job_id="job-1", **fields)


@pytest.mark.parametrize(
    ("arguments", "field", "error"),
    [
        ({"from_line": 1, "cursor": {"epoch": "a" * 32, "sequence": 1}}, "cursor", "conflict"),
        ({"from_line": 1, "levels": ["error"]}, "levels", "conflict"),
        ({"from_line": 1, "context": 1}, "context", "conflict"),
        ({"to_line": 5}, "from_line", "required"),
    ],
)
async def test_surface_refuses_range_mixed_with_filters(tmp_path, arguments, field, error):
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            body = await call(
                client,
                "status",
                {"view": "logs", "stream": "build", "job_id": "build-0", **arguments},
            )
    assert body == {"ok": False, "error": f"argument_{error}", "field": field}


async def test_surface_rejects_range_for_other_streams(tmp_path):
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            body = await call(
                client, "status", {"view": "logs", "stream": "app", "job_id": "x", "from_line": 1}
            )
    assert body == {"ok": False, "error": "argument_not_allowed", "field": "from_line"}
