"""Characterization: surface reason codes that no other test pins (mock backend, localhost only).

Each case pins what the six tools return today, so collapsing the legacy @op layer cannot change
it silently. Known gaps are xfail(strict) tests stating the desired behaviour instead.
"""

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from surface_harness import REMOTE, lease, raw, serve, ticket

from cardputer_mcp import continuation
from cardputer_mcp.client import call, connect

TOKEN = "c" * 64
NO_PLAN = "f" * 64
APP_TAP = {
    "op": "app_input",
    "job_id": "app-job",
    "boot_id": 1,
    "run_id": 1,
    "control_session": 1,
    "operation": "action.tap",
}
INVALID_ARGUMENTS = {
    "ok": False,
    "error": "invalid_tool_request",
    "reason": "invalid_arguments",
    "fields": [],
}


@pytest.fixture(scope="module")
def event_loop_policy():
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.mark.parametrize(
    "tool,request_body,is_error,expected",
    [
        # argument_invalid: a value the schema allows but the surface/legacy model refuses.
        (
            "session",
            {"action": "request", "resume_token": TOKEN, "wait_seconds": 0},
            False,
            {"ok": False, "error": "argument_invalid", "field": "wait_seconds"},
        ),
        # A model-level validator error has no loc, so no field is named.
        (
            "execute",
            {**APP_TAP, "session": TOKEN},
            False,
            {"ok": False, "error": "argument_invalid"},
        ),
        # A free-form cursor is validated late; the first missing cursor field is named.
        (
            "status",
            {"view": "logs", "stream": "broker", "cursor": {}},
            False,
            {"ok": False, "error": "argument_invalid", "field": "epoch"},
        ),
        # Per-stream rules for stream=app.
        (
            "status",
            {"view": "logs", "stream": "app"},
            False,
            {"ok": False, "error": "argument_required", "field": "job_id"},
        ),
        (
            "status",
            {"view": "logs", "stream": "app", "job_id": "j", "levels": ["error"]},
            False,
            {"ok": False, "error": "argument_not_allowed", "field": "levels"},
        ),
        (
            "status",
            {"view": "logs", "stream": "app", "job_id": "unknown-job"},
            False,
            {"ok": False, "error": "app_events_not_available"},
        ),
        # job_plan_mismatch is decided from the store before any session/ownership check,
        # including for a job that does not exist (DESIGN-REDESIGN §2.2-5).
        (
            "cancel",
            {"target": "job", "job_id": "no-such-job", "session": TOKEN, "plan_hash": NO_PLAN},
            False,
            {"ok": False, "error": "job_plan_mismatch", "field": "plan_hash"},
        ),
        # Explicit null: on an optional field it counts as present (rule refusal) ...
        (
            "status",
            {"view": "identity", "job_id": None},
            False,
            {"ok": False, "error": "argument_not_allowed", "field": "job_id"},
        ),
        # ... on a required nullable field it passes the rules and trips an internal assert,
        # which surfaces as the generic invalid_arguments (current behaviour, needs asserts on).
        ("status", {"view": "source", "project_id": None}, True, INVALID_ARGUMENTS),
        ("execute", {"op": "run", "plan_hash": None}, True, INVALID_ARGUMENTS),
        (
            "execute",
            {"op": "home_recovery", "job_id": None, "source_job_id": "s"},
            True,
            INVALID_ARGUMENTS,
        ),
        (
            "cancel",
            {"target": "request", "resume_token": None, "request_id": "r"},
            True,
            INVALID_ARGUMENTS,
        ),
        ("session", {"action": "renew", "session": None}, True, INVALID_ARGUMENTS),
        # ... while a non-nullable field is caught by pre-validation with its name.
        (
            "flash",
            {"plan_hash": None},
            True,
            {
                "ok": False,
                "error": "invalid_tool_request",
                "reason": "invalid_field_type",
                "fields": ["plan_hash"],
            },
        ),
    ],
)
async def test_reason_codes_without_device_state(
    tmp_path: Path, tool: str, request_body: dict, is_error: bool, expected: dict
) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            error, body, _ = await raw(client, tool, request_body)
    assert (error, body) == (is_error, expected)


async def test_status_apps_on_an_unobserved_mock(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            body = await call(client, "status", {"view": "apps"})
    assert body == {
        "ok": True,
        "apps": [],
        "source": "unobserved",
        "fresh": False,
        "observed_at": None,
        "firmware_verification": "unverified",
        "protocol": "app-control-v1",
    }


async def test_job_plan_mismatch_for_an_existing_job(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            session = await lease(client)
            plan = await call(
                client, "submit", {"kind": "diagnostics", "session": session, "job_id": "done"}
            )
            queued = await call(
                client, "execute", {"op": "run", "plan_hash": plan["plan_hash"], "session": session}
            )
            assert queued["ok"], queued
            refused = await call(
                client,
                "cancel",
                {"target": "job", "job_id": "done", "session": TOKEN, "plan_hash": NO_PLAN},
            )
    assert refused == {"ok": False, "error": "job_plan_mismatch", "field": "plan_hash"}


async def test_continuation_error_keeps_the_queued_job_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            session = await lease(client)
            plan = await call(
                client, "submit", {"kind": "diagnostics", "session": session, "job_id": "one"}
            )
            monkeypatch.setattr(continuation, "MAX_ISSUED_TICKETS", 0)
            queued = await call(
                client,
                "execute",
                {
                    "op": "run",
                    "plan_hash": plan["plan_hash"],
                    "session": session,
                    "continues": True,
                },
            )
    # The job is queued; only the ticket failed, reported beside ok:true.
    assert queued["ok"] is True and queued["id"] == "one"
    assert queued["continuation_error"] == "ticket_limit"
    assert "continuation_ticket" not in queued


async def deferred_plans(client: Any, session: str) -> dict[str, str]:
    """Plans requested ahead of time carry principal_id, so the wrong-tool guard can see them."""
    parent = await ticket(client, session, "parent")
    plans = {}
    for kind, extra in (("restore", {"device_id": "synthetic-device"}), ("diagnostics", {})):
        prepared = await call(
            client, "submit", {"kind": kind, "job_id": f"later-{kind}", "after": parent, **extra}
        )
        assert prepared["ok"], prepared
        assert prepared["execute_tool"] == ("flash" if kind == "restore" else "execute")
        plans[kind] = prepared["plan_hash"]
    return plans


async def test_wrong_tool_guard_with_auth_required(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            plans = await deferred_plans(client, await lease(client))
            via_execute = await call(
                client, "execute", {"op": "run", "plan_hash": plans["restore"]}
            )
            via_flash = await call(client, "flash", {"plan_hash": plans["diagnostics"]})
        # Refused before any state change: neither plan became a waiting job.
        assert surface.resident.deferred.get("later-restore") is None
        assert surface.resident.deferred.get("later-diagnostics") is None
    assert via_execute == {"ok": False, "error": "wrong_execute_tool", "use": "flash"}
    assert via_flash == {"ok": False, "error": "wrong_flash_tool", "use": "execute"}


GAP = (
    "DESIGN-REDESIGN §2.2-4: the wrong-tool guard runs only when auth is required and only "
    "for plans that carry a top-level principal key"
)


async def _wrong_tool_attempt(tmp_path: Path, require_auth: bool, case: str) -> dict:
    async with serve(tmp_path, require_auth=require_auth) as surface:
        async with connect(surface.url, REMOTE) as client:
            session = await lease(client)
            if case == "restore_via_execute":
                plans = await deferred_plans(client, session)
                return await call(client, "execute", {"op": "run", "plan_hash": plans["restore"]})
            # An ordinary leased device plan carries no top-level principal key.
            plan = await call(
                client, "submit", {"kind": "diagnostics", "session": session, "job_id": "plain"}
            )
            return await call(client, "flash", {"plan_hash": plan["plan_hash"], "session": session})


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=GAP)
@pytest.mark.parametrize(
    "require_auth,case,expected",
    [
        (
            False,
            "restore_via_execute",
            {"ok": False, "error": "wrong_execute_tool", "use": "flash"},
        ),
        (False, "plain_via_flash", {"ok": False, "error": "wrong_flash_tool", "use": "execute"}),
        (True, "plain_via_flash", {"ok": False, "error": "wrong_flash_tool", "use": "execute"}),
    ],
)
async def test_wrong_tool_guard_desired(
    tmp_path: Path, require_auth: bool, case: str, expected: dict
) -> None:
    # Today each of these is accepted and queued (waiting_parent / queued).
    assert await _wrong_tool_attempt(tmp_path, require_auth, case) == expected
