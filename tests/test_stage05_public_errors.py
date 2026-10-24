"""Characterize today's exception -> public error ladders before a single redaction gate.

Every assertion here is current behaviour that a PublicError/to_public gate must keep
byte-for-byte: BrokerError text passes through; SourceError uses public(); any other
ValueError is the exact allowlist or invalid_request; anything else reaches
SafeMCPServer and becomes the generic invalid_tool_request body.
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from test_registry_http import registry_service as registry_service

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.client import connect
from cardputer_mcp.continuation import TicketError
from cardputer_mcp.models import LogCursor
from cardputer_mcp.public_reasons import PUBLIC_REASON_CODES
from cardputer_mcp.server import _result
from cardputer_mcp.source_snapshot import CacheScanLimit, SourceError
from cardputer_mcp.surface import _convert

SECRET = "C:/SYNTHETIC_PRIVATE_PATH/secret.txt"
GENERIC = {
    "ok": False,
    "error": "invalid_tool_request",
    "reason": "invalid_arguments",
    "fields": [],
}
# Codes that reach clients today only because BrokerError text is passed through
# (TicketError and app-session/app-event ValueErrors re-wrapped in BrokerError).
TICKET_CODES = ("ticket_limit", "ticket_invalid", "ticket_consumed", "hold_expired", "ticket_void")
APP_COMMAND_CODES = (
    "stale_boot",
    "stale_run",
    "stale_session",
    "conflict",
    "app_input_stopped",
    "action_not_in_plan",
    "out_of_order",
    "limit",
    "queue_full",
    "rate_limit",
)
APP_EVENT_CODES = ("app_events_not_available", "cursor_ahead")
PASSTHROUGH_CODES = (*TICKET_CODES, *APP_COMMAND_CODES, *APP_EVENT_CODES)
ALLOWLISTED = "source_fetch_busy"
assert ALLOWLISTED in PUBLIC_REASON_CODES


def _raise(exc: BaseException):
    def call(*args: Any, **kwargs: Any):
        raise exc

    return call


def _async_raise(exc: BaseException):
    async def call(*args: Any, **kwargs: Any):
        raise exc

    return call


# --- lowest layer: server._result -------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        *[(BrokerError(code), {"ok": False, "error": code}) for code in PASSTHROUGH_CODES],
        (SourceError("source_fetch_busy"), {"ok": False, "error": "source_fetch_busy"}),
        (SourceError(SECRET), {"ok": False, "error": "source_fetch_failed"}),
        (
            SourceError("source_cache_protected"),
            {
                "ok": False,
                "error": "source_cache_protected",
                "explanation": SourceError("source_cache_protected").public()["explanation"],
            },
        ),
        (
            CacheScanLimit("entries", 7, 9),
            {"ok": False, **CacheScanLimit("entries", 7, 9).public()},
        ),
        (ValueError(ALLOWLISTED), {"ok": False, "error": ALLOWLISTED}),
        (ValueError(SECRET), {"ok": False, "error": "invalid_request"}),
        (ValueError("ticket_invalid"), {"ok": False, "error": "invalid_request"}),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else None,
)
def test_result_ladder_table(exc, expected):
    assert _result(_raise(exc)) == expected


def test_result_ladder_lets_unexpected_exceptions_propagate():
    with pytest.raises(RuntimeError):
        _result(_raise(RuntimeError(SECRET)))
    with pytest.raises(KeyError):
        _result(_raise(KeyError(SECRET)))


def test_cache_scan_limit_public_shape_is_bounded_counters():
    assert CacheScanLimit("entries", 7, 9).public() == {
        "error": "source_cache_scan_limit",
        "scan": CacheScanLimit("entries", 7, 9).scan,
    }
    assert set(CacheScanLimit("other", 1, 2).scan) >= {"reason"}
    assert CacheScanLimit("other", 1, 2).scan["reason"] == "deadline"


# --- surface._convert (request-model building) ------------------------------------


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (
            lambda: LogCursor.model_validate({"epoch": "zz", "sequence": 1}),
            {"ok": False, "error": "argument_invalid", "field": "epoch"},
        ),
        (
            lambda: LogCursor.model_validate({"epoch": "a" * 32, "sequence": -1}),
            {"ok": False, "error": "argument_invalid", "field": "sequence"},
        ),
    ],
)
def test_convert_maps_validation_errors_to_fixed_codes(build, expected):
    assert _convert(build) == (None, expected)


# --- MCP: each inline ladder, driven end to end over HTTP --------------------------

LADDER_CASES = [
    ("broker", BrokerError("ticket_invalid"), {"ok": False, "error": "ticket_invalid"}),
    ("source", SourceError("source_cache_full"), {"ok": False, "error": "source_cache_full"}),
    ("allowlisted", ValueError(ALLOWLISTED), {"ok": False, "error": ALLOWLISTED}),
    ("other_value", ValueError(SECRET), {"ok": False, "error": "invalid_request"}),
    ("unexpected", RuntimeError(SECRET), GENERIC),
]


def _install(resident, path: str, exc: BaseException, monkeypatch) -> tuple[str, dict[str, Any]]:
    """Make the first broker call behind one MCP path raise; return its tool request."""
    if path == "device":  # _result ladder
        monkeypatch.setattr(resident, "status", _raise(exc))
        return "status", {"view": "device"}
    if path == "source":  # read_source_resources inline ladder
        monkeypatch.setattr(resident.profile_jobs.source_reads, "run", _async_raise(exc))
        return "status", {"view": "source", "project_id": "synthetic"}
    if path == "fetch":  # prepare_test SOURCE inline ladder
        monkeypatch.setattr(resident.profile_jobs, "prepare_source", _async_raise(exc))
        return "submit", {
            "kind": "fetch",
            "job_id": "fetch-1",
            "project_id": "synthetic",
            "source_commit": "a" * 40,
        }
    assert path == "build_logs"  # read_build_logs inline ladder
    monkeypatch.setattr(resident._store(), "job", _raise(exc))
    return "status", {"view": "logs", "stream": "build", "job_id": "build-1"}


@pytest.mark.parametrize("path", ["device", "source", "fetch", "build_logs"])
@pytest.mark.parametrize(
    ("label", "exc", "expected"), LADDER_CASES, ids=[c[0] for c in LADDER_CASES]
)
async def test_mcp_paths_share_one_ladder(
    registry_service, monkeypatch, path, label, exc, expected
):
    service = registry_service
    tool, request = _install(service.resident, path, exc, monkeypatch)
    async with connect(service.url, service.keys["first"].token) as client:
        result = await client.call_tool(tool, {"request": request})
    assert result.structured_content == expected
    assert bool(result.is_error) is (expected is GENERIC)
    rendered = json.dumps([c.model_dump() for c in result.content])
    assert SECRET not in rendered and "SYNTHETIC_PRIVATE" not in rendered


async def _lease(client) -> str:
    opened = await client.call_tool(
        "session", {"request": {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}}
    )
    return opened.structured_content["session"]


# --- passthrough codes that are not in PUBLIC_REASON_CODES -------------------------


@pytest.mark.parametrize("code", [*TICKET_CODES, "queue_full"])
async def test_ticket_error_reaches_continuation_error_unchanged(
    registry_service, monkeypatch, code
):
    service = registry_service
    monkeypatch.setattr(service.resident.tickets, "issue", _raise(TicketError(code)))
    async with connect(service.url, service.keys["first"].token) as client:
        session = await _lease(client)
        plan = await client.call_tool(
            "submit", {"request": {"kind": "diagnostics", "session": session, "job_id": "t-1"}}
        )
        result = await client.call_tool(
            "execute",
            {
                "request": {
                    "op": "run",
                    "session": session,
                    "plan_hash": plan.structured_content["plan_hash"],
                    "continues": True,
                }
            },
        )
    body = result.structured_content
    assert body["ok"] is True and body["continuation_error"] == code
    assert "continuation_ticket" not in body and not result.is_error


async def test_cancel_unknown_ticket_is_ticket_invalid(registry_service):
    service = registry_service
    async with connect(service.url, service.keys["first"].token) as client:
        result = await client.call_tool(
            "cancel", {"request": {"target": "ticket", "ticket": "0" * 8}}
        )
    assert result.structured_content == {"ok": False, "error": "ticket_invalid"}
    assert not result.is_error


@pytest.mark.parametrize("code", ["ticket_consumed", "ticket_invalid", "hold_expired"])
async def test_cancel_ticket_broker_error_is_refused_with_its_code(
    registry_service, monkeypatch, code
):
    service = registry_service
    monkeypatch.setattr(service.resident, "cancel_ticket", _raise(BrokerError(code)))
    async with connect(service.url, service.keys["first"].token) as client:
        result = await client.call_tool(
            "cancel", {"request": {"target": "ticket", "ticket": "0" * 8}}
        )
    assert result.structured_content == {"ok": False, "error": code}


async def test_cancel_ticket_unexpected_error_is_generic(registry_service, monkeypatch):
    service = registry_service
    monkeypatch.setattr(service.resident, "cancel_ticket", _raise(RuntimeError(SECRET)))
    async with connect(service.url, service.keys["first"].token) as client:
        result = await client.call_tool(
            "cancel", {"request": {"target": "ticket", "ticket": "0" * 8}}
        )
    assert result.is_error is True and result.structured_content == GENERIC


class _FakeEvents:
    def __init__(self, exc):
        self.exc = exc

    def read(self, *args):
        raise self.exc

    def expire(self):
        """maintenance_tick expires every app run's events; nothing to expire here."""


def _fake_app_run(principal_id: str, exc: BaseException):
    return SimpleNamespace(
        principal=principal_id,
        events=_FakeEvents(exc),
        command=_raise(exc),
    )


@pytest.mark.parametrize("code", APP_EVENT_CODES)
async def test_app_event_value_error_reaches_client_unchanged(registry_service, monkeypatch, code):
    service = registry_service
    principal = service.keys["first"].metadata.principal_id
    jobs = service.resident.app_sessions.jobs
    monkeypatch.setitem(jobs, "app-1", _fake_app_run(principal, ValueError(code)))
    async with connect(service.url, service.keys["first"].token) as client:
        result = await client.call_tool(
            "status", {"request": {"view": "logs", "stream": "app", "job_id": "app-1"}}
        )
    assert result.structured_content == {"ok": False, "error": code}


async def test_app_event_converter_does_not_leak_free_text(registry_service, monkeypatch):
    service = registry_service
    principal = service.keys["first"].metadata.principal_id
    jobs = service.resident.app_sessions.jobs
    monkeypatch.setitem(jobs, "app-1", _fake_app_run(principal, ValueError(SECRET)))
    async with connect(service.url, service.keys["first"].token) as client:
        result = await client.call_tool(
            "status", {"request": {"view": "logs", "stream": "app", "job_id": "app-1"}}
        )
    assert SECRET not in json.dumps(result.structured_content)


@pytest.mark.parametrize("code", APP_COMMAND_CODES)
async def test_app_command_value_error_reaches_client_unchanged(
    registry_service, monkeypatch, code
):
    service = registry_service
    resident = service.resident
    async with connect(service.url, service.keys["first"].token) as client:
        session = await _lease(client)
        lease = resident.session
        store = resident._store()
        real_job, real_plan = store.job, store.plan
        job = {"plan_hash": "f" * 64, "state": "running"}
        plan = {"test": "app_session", "broker_epoch": resident.epoch, "session_id": lease.id}
        monkeypatch.setattr(store, "job", lambda j: job if j == "app-1" else real_job(j))
        monkeypatch.setattr(store, "plan", lambda h: plan if h == "f" * 64 else real_plan(h))
        monkeypatch.setitem(
            resident.app_sessions.jobs,
            "app-1",
            _fake_app_run(lease.principal_id, ValueError(code)),
        )
        result = await client.call_tool(
            "execute",
            {
                "request": {
                    "op": "app_input",
                    "session": session,
                    "job_id": "app-1",
                    "boot_id": 1,
                    "run_id": 1,
                    "control_session": 1,
                    "operation": "status",
                }
            },
        )
    assert result.structured_content == {"ok": False, "error": code}
    assert not result.is_error


# --- status(view=capture) is the one domain refusal with isError=true --------------


async def test_capture_domain_refusal_is_is_error_with_broker_code(registry_service, monkeypatch):
    service = registry_service
    monkeypatch.setattr(service.resident, "read_capture", _raise(BrokerError("ticket_invalid")))
    async with connect(service.url, service.keys["reader"].token) as client:
        result = await client.call_tool("status", {"request": {"view": "capture", "job_id": "c-1"}})
    assert result.is_error is True
    assert result.structured_content == {"ok": False, "error": "ticket_invalid"}


@pytest.mark.parametrize("exc", [ValueError(SECRET), RuntimeError(SECRET)])
async def test_capture_non_broker_error_falls_to_generic_body(registry_service, monkeypatch, exc):
    service = registry_service
    monkeypatch.setattr(service.resident, "read_capture", _raise(exc))
    async with connect(service.url, service.keys["reader"].token) as client:
        result = await client.call_tool("status", {"request": {"view": "capture", "job_id": "c-1"}})
    assert result.is_error is True and result.structured_content == GENERIC
    assert SECRET not in json.dumps([c.model_dump() for c in result.content])
