"""The 6-tool surface never echoes caller-chosen names and keeps the shared job view."""

import json

import pytest
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.derby_reports import PROVENANCE_KEY

SECRET = "C:/Users/SYNTHETIC_PRIVATE/secret"


@pytest.mark.parametrize(
    "arguments",
    [
        {"stream": "broker"},
        {"stream": "build", "job_id": "synthetic-job"},
        {"stream": "app", "job_id": "synthetic-job"},
    ],
)
async def test_log_cursor_failure_names_only_the_cursor_argument(registry_service, arguments):
    service = registry_service
    cursor = {"epoch": "a" * 32, "sequence": 1, SECRET: 1}
    async with connect(service.url, service.keys["reader"].token) as client:
        refused = await call(client, "status", {"view": "logs", "cursor": cursor, **arguments})
    assert refused == {"ok": False, "error": "argument_invalid", "field": "cursor"}
    assert "SYNTHETIC_PRIVATE" not in json.dumps(refused)


async def test_cancel_of_a_requested_job_returns_the_shared_job_view(registry_service, monkeypatch):
    service = registry_service
    broker = service.resident
    row = {
        "id": "synthetic-job",
        "state": "cancelled",
        "diagnostic": {
            PROVENANCE_KEY: {"private": "SYNTHETIC_PROVENANCE"},
            "derby_observation": {"diagnostics": "SYNTHETIC_DIAGNOSTICS", "kept": True},
        },
        "derby_diagnostics": "SYNTHETIC_REPORT",
        "fixed_trial": "SYNTHETIC_TRIAL",
    }
    monkeypatch.setattr(broker, "cancel_deferred", lambda job_id, principal_id: None)
    monkeypatch.setattr(broker, "cancel_job_only", lambda job_id, principal_id: dict(row))
    async with connect(service.url, service.keys["second"].token) as client:
        cancelled = await call(client, "cancel", {"target": "job", "job_id": "synthetic-job"})
    assert cancelled["ok"] is True and cancelled["state"] == "cancelled"
    assert cancelled["diagnostic"] == {"derby_observation": {"kept": True}}
    assert "SYNTHETIC" not in json.dumps(cancelled)
