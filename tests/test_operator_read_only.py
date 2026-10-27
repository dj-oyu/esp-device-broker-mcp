"""A local read-only key may read operator state, timeline, events and job logs only."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from cardputer_mcp import operator_stop
from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.models import Origin
from cardputer_mcp.operator_api import READ_ROUTES, OperatorAPI

READ_ONLY = SimpleNamespace(principal_id="viewer", origin=Origin.LOCAL, scopes=frozenset({"read"}))
CONTROL = SimpleNamespace(
    principal_id="operator", origin=Origin.LOCAL, scopes=frozenset({"read", "control"})
)
ACTIONS = (
    ("POST", "cancel"),
    ("POST", "job-cancel"),
    ("POST", "job-stop"),
    ("POST", "review"),
    ("POST", "recheck"),
    ("POST", "reorder"),
    ("POST", "release"),
    ("POST", "image"),
    ("GET", "images"),
    ("GET", "plans"),
    ("GET", "unknown-route"),
)


@pytest.fixture
async def operator(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "state"), lock_path=tmp_path / "device.lock")
    await broker.start()
    current = [READ_ONLY]

    async def unused(scope, receive, send):
        raise AssertionError("unexpected route")

    api = OperatorAPI(unused, broker, lambda header: current[0])
    try:
        yield api, broker, current
    finally:
        await broker.close()


async def call(api, method, route, body=None):
    received = asyncio.Queue()
    received.put_nowait({"type": "http.request", "body": json.dumps(body or {}).encode()})
    output = []

    async def send(message):
        output.append(message)

    headers = [(b"content-type", b"application/json")] if method == "POST" else []
    scope = {
        "type": "http",
        "path": "/operator/" + route,
        "method": method,
        "query_string": b"",
        "headers": headers,
    }
    await asyncio.wait_for(api(scope, received.get, send), 2)
    return output[0]["status"], json.loads(output[1]["body"])


def test_read_routes_are_reads_only():
    assert READ_ROUTES == {
        "/operator/state",
        "/operator/timeline",
        "/operator/events",
        "/operator/job-logs",
    }


async def test_read_only_local_key_reads_state_and_timeline(operator):
    api, broker, _ = operator
    for route in ("state", "timeline"):
        status, result = await call(api, "GET", route)
        assert status == 200 and result["ok"] is True, (route, result)
    assert broker.backend.calls == 0


@pytest.mark.parametrize("method,route", ACTIONS)
async def test_read_only_local_key_cannot_act(operator, method, route):
    api, broker, _ = operator
    status, result = await call(api, method, route, {"request_id": "r"})
    assert (status, result) == (403, {"ok": False, "error": "local_operator_required"})
    assert broker.backend.calls == 0


@pytest.mark.parametrize(
    "principal",
    [
        SimpleNamespace(principal_id="r", origin=Origin.REMOTE, scopes=frozenset({"read"})),
        SimpleNamespace(
            principal_id="r", origin=Origin.REMOTE, scopes=frozenset({"read", "control"})
        ),
        SimpleNamespace(principal_id="l", origin=Origin.LOCAL, scopes=frozenset()),
    ],
)
async def test_remote_or_scopeless_keys_still_cannot_read(operator, principal):
    api, _, current = operator
    current[0] = principal
    for method, route in (("GET", "state"), ("GET", "timeline"), ("POST", "job-logs")):
        status, result = await call(api, method, route, {"job_id": "j"})
        assert (status, result) == (403, {"ok": False, "error": "local_operator_required"})


@pytest.mark.parametrize("principal,offered", [(READ_ONLY, False), (CONTROL, True)])
async def test_timeline_offers_cancel_and_stop_only_with_control(
    operator, monkeypatch, principal, offered
):
    api, _, _ = operator
    running = {"job_id": "running", "state": "running"}
    queued = {"job_id": "queued", "state": "queued"}
    monkeypatch.setattr(api.timeline, "snapshot", lambda: {"cards": [dict(running), dict(queued)]})
    monkeypatch.setattr(
        api, "_operator_profile", lambda plan, who: plan is not None and plan["id"] == "running"
    )
    monkeypatch.setattr(api.broker, "_store", lambda: _Store())
    monkeypatch.setattr(operator_stop, "stop_available", lambda broker, job_id: True)
    cards = {card["job_id"]: card for card in api._timeline(principal)["cards"]}
    assert cards["running"]["cancel_available"] is offered
    assert cards["running"]["stop_available"] is False
    assert cards["queued"]["cancel_available"] is False
    assert cards["queued"]["stop_available"] is offered


class _Store:
    def job(self, job_id):
        return {"plan_hash": job_id}

    def plan(self, digest):
        return {"id": digest}
