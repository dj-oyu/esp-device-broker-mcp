"""Characterization: per-path auth/scope gate, body and text encoding of the six tools.

Pins, for one fixed sequence of stateless or self-contained requests, what each tool
returns under five caller setups: an unauthenticated mock with and without a bearer, an
auth-required mock, and registry keys holding only `read` or only `control`. The gate a
path applies (always authenticate vs. skip when auth is not required) is visible as the
difference between the setups, so folding the internal operation layer into the surface
cannot move a principal/scope check silently.

Success bodies are pinned by their key set (they carry fresh capabilities and times);
refusals are pinned exactly. Each row also records how the first text content encodes
the body. Regenerate after an intended change with UPDATE_SURFACE_GATE_MATRIX=1 and
review the fixture diff. Mock backend and localhost only; synthetic tokens.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from surface_harness import REMOTE, serve
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import connect

FIXTURE = Path(__file__).parent / "fixtures" / "surface_gate_matrix.json"
TOKEN = "c" * 64
NO_PLAN = "f" * 64
TICKET = "ABCDEFGH"
TAP = {
    "op": "app_input",
    "session": TOKEN,
    "job_id": "app-job",
    "boot_id": 1,
    "run_id": 1,
    "control_session": 1,
    "operation": "action.tap",
    "action": "accept",
    "command_id": 1,
    "seq": 1,
}

# Order matters: the session rows near the end take the device lease.
REQUESTS: list[tuple[str, str, dict[str, Any]]] = [
    ("status.identity", "status", {"view": "identity"}),
    ("status.targets", "status", {"view": "targets"}),
    ("status.source", "status", {"view": "source", "project_id": "synthetic"}),
    ("status.device", "status", {"view": "device"}),
    ("status.session", "status", {"view": "session", "resume_token": TOKEN}),
    ("status.job", "status", {"view": "job", "job_id": "no-such-job"}),
    ("status.logs.broker", "status", {"view": "logs", "stream": "broker"}),
    (
        "status.logs.broker.cursor",
        "status",
        {"view": "logs", "stream": "broker", "cursor": {"epoch": "x", "sequence": 0}},
    ),
    ("status.logs.build", "status", {"view": "logs", "stream": "build", "job_id": "no-such"}),
    ("status.logs.device", "status", {"view": "logs", "stream": "device", "job_id": "no-such"}),
    ("status.logs.app", "status", {"view": "logs", "stream": "app", "job_id": "no-such"}),
    ("status.capture", "status", {"view": "capture", "job_id": "no-such-job"}),
    ("status.apps", "status", {"view": "apps"}),
    ("status.jobs", "status", {"view": "jobs"}),
    ("status.jobs.cursor", "status", {"view": "jobs", "cursor": {"before": 0}}),
    ("status.recovery", "status", {"view": "recovery"}),
    (
        "submit.fetch",
        "submit",
        {"kind": "fetch", "job_id": "f1", "project_id": "synthetic", "source_commit": "a" * 40},
    ),
    ("submit.build", "submit", {"kind": "build", "job_id": "b1", "project_id": "synthetic"}),
    ("submit.hello", "submit", {"kind": "hello", "job_id": "h1", "session": TOKEN}),
    ("submit.capture", "submit", {"kind": "capture", "job_id": "c1", "session": TOKEN}),
    (
        "submit.backup",
        "submit",
        {"kind": "backup", "job_id": "k1", "session": TOKEN, "device_id": "dev"},
    ),
    (
        "submit.flash",
        "submit",
        {
            "kind": "flash",
            "job_id": "x1",
            "session": TOKEN,
            "device_id": "dev",
            "build_job_id": "b1",
        },
    ),
    (
        "submit.flash.null_session",
        "submit",
        {"kind": "flash", "job_id": "x2", "session": None, "device_id": "dev", "build_job_id": "b"},
    ),
    ("submit.after", "submit", {"kind": "diagnostics", "job_id": "d1", "after": TICKET}),
    ("submit.after.backup", "submit", {"kind": "backup", "job_id": "k2", "after": TICKET}),
    ("execute.run", "execute", {"op": "run", "plan_hash": NO_PLAN}),
    ("execute.run.session", "execute", {"op": "run", "plan_hash": NO_PLAN, "session": TOKEN}),
    (
        "execute.run.hint",
        "execute",
        {"op": "run", "plan_hash": NO_PLAN, "chain_total_hint": 2},
    ),
    ("execute.run.continues", "execute", {"op": "run", "plan_hash": NO_PLAN, "continues": True}),
    ("execute.app_input", "execute", TAP),
    (
        "execute.home_recovery",
        "execute",
        {"op": "home_recovery", "job_id": "r", "source_job_id": "s"},
    ),
    ("flash", "flash", {"plan_hash": NO_PLAN}),
    ("flash.session", "flash", {"plan_hash": NO_PLAN, "session": TOKEN}),
    ("cancel.job", "cancel", {"target": "job", "job_id": "no-such-job"}),
    ("cancel.job.session", "cancel", {"target": "job", "job_id": "no-such-job", "session": TOKEN}),
    (
        "cancel.request",
        "cancel",
        {"target": "request", "resume_token": TOKEN, "request_id": "r1"},
    ),
    ("cancel.ticket", "cancel", {"target": "ticket", "ticket": TICKET}),
    ("session.renew", "session", {"action": "renew", "session": TOKEN}),
    ("session.release", "session", {"action": "release", "session": TOKEN}),
    ("session.close", "session", {"action": "close", "resume_token": TOKEN}),
    ("session.request", "session", {"action": "request", "resume_token": TOKEN}),
    ("session.open.other_owner", "session", {"action": "open", "owner": "local_cli"}),
    (
        "session.open.wait_without_lease",
        "session",
        {"action": "open", "owner": "remote_mcp", "wait_seconds": 5},
    ),
    (
        "session.open.lease_with_ttl",
        "session",
        {"action": "open", "owner": "remote_mcp", "lease_seconds": 30, "ttl_seconds": 60},
    ),
    ("session.open", "session", {"action": "open", "owner": "remote_mcp"}),
    (
        "session.open.lease",
        "session",
        {"action": "open", "owner": "remote_mcp", "lease_seconds": 30},
    ),
    (
        "session.open.lease.wait",
        "session",
        {"action": "open", "owner": "remote_mcp", "lease_seconds": 30, "wait_seconds": 5},
    ),
]


@pytest.fixture(scope="module")
def event_loop_policy():
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


def _shape(body: Any) -> Any:
    """Refusals exactly; successes by key set (nested composite parts likewise)."""
    if not isinstance(body, dict) or body.get("ok") is not True:
        return body
    shaped: dict[str, Any] = {"ok": True, "keys": sorted(body)}
    if isinstance(body.get("device_request"), dict):
        shaped["device_request"] = _shape(body["device_request"])
    return shaped


def _encoding(body: Any, text: str) -> str:
    if text == json.dumps(body, sort_keys=True):
        return "sorted"
    if text == json.dumps(body, indent=2):
        return "indented"
    if text == json.dumps(body):
        return "plain"
    return "other"


async def _run(url: str, token: str | None) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    async with connect(url, token) as client:
        for label, tool, request in REQUESTS:
            result = await client.call_tool(tool, {"request": request})
            body = result.structured_content
            text = result.content[0].text if result.content else ""
            rows[label] = {
                "is_error": bool(result.is_error),
                "body": _shape(body),
                "encoding": _encoding(body, text),
                "content": [content.type for content in result.content],
            }
    return rows


def _compare(scenario: str, rows: dict[str, Any]) -> None:
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8")) if FIXTURE.exists() else {}
    if os.environ.get("UPDATE_SURFACE_GATE_MATRIX") == "1":
        recorded[scenario] = rows
        FIXTURE.write_text(
            json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    expected = recorded[scenario]
    drifted = {
        label: {"expected": expected.get(label), "actual": row}
        for label, row in rows.items()
        if expected.get(label) != row
    }
    assert not drifted, json.dumps(drifted, indent=2, sort_keys=True)
    assert sorted(expected) == sorted(rows)


@pytest.mark.parametrize(
    "scenario,require_auth,token",
    [
        ("mock_anonymous", False, None),
        ("mock_bearer", False, REMOTE),
        ("auth_required", True, REMOTE),
    ],
)
async def test_gate_matrix_on_the_mock(
    tmp_path: Path, scenario: str, require_auth: bool, token: str | None
) -> None:
    async with serve(tmp_path, require_auth=require_auth) as surface:
        rows = await _run(surface.url, token)
    _compare(scenario, rows)


async def test_gate_matrix_with_single_scope_keys(registry_service) -> None:
    service = registry_service
    read_rows = await _run(service.url, service.keys["reader"].token)
    control_rows = await _run(service.url, service.keys["first"].token)
    _compare("registry_read_only", read_rows)
    _compare("registry_control_only", control_rows)
