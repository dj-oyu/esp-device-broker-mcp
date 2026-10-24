"""A POST body wait must not preserve a revoked operator grant."""

import json
from types import SimpleNamespace

import pytest

from cardputer_mcp.models import Origin
from cardputer_mcp.operator_api import OperatorAPI


@pytest.mark.parametrize(
    "route,body",
    [
        ("cancel", {"request_id": "fixture"}),
    ],
)
@pytest.mark.parametrize("changed", [None, "read_only", "remote"])
async def test_operator_reauthenticates_after_fragmented_body(route, body, changed):
    current = SimpleNamespace(origin=Origin.LOCAL, scopes=frozenset({"read", "control"}))
    authentications = []

    def authenticate(header):
        authentications.append(header)
        return current

    async def unused_app(scope, receive, send):
        raise AssertionError("route was not handled")

    api = OperatorAPI(unused_app, SimpleNamespace(), authenticate)
    encoded = json.dumps(body).encode()
    chunks = [encoded[:3], encoded[3:]]

    async def receive():
        nonlocal current
        part = chunks.pop(0)
        if not chunks:
            current = (
                None
                if changed is None
                else SimpleNamespace(
                    origin=Origin.REMOTE if changed == "remote" else Origin.LOCAL,
                    scopes=frozenset({"read"})
                    if changed == "read_only"
                    else frozenset({"read", "control"}),
                )
            )
        return {"type": "http.request", "body": part, "more_body": bool(chunks)}

    output = []

    async def send(message):
        output.append(message)

    await api(
        {
            "type": "http",
            "path": "/operator/" + route,
            "method": "POST",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer SYNTHETIC"),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
        send,
    )
    assert len(authentications) == 2
    assert output[0]["status"] == (401 if changed is None else 403)
    assert b"SYNTHETIC" not in output[1]["body"]


async def test_retained_job_cancel_uses_final_authenticated_actor_after_body_wait():
    first = SimpleNamespace(
        principal_id="first", origin=Origin.LOCAL, scopes=frozenset({"read", "control"})
    )
    final = SimpleNamespace(
        principal_id="final", origin=Origin.LOCAL, scopes=frozenset({"read", "control"})
    )
    current = first
    calls = []

    def cancel(job, plan, epoch, actor, origin):
        calls.append((job, plan, epoch, actor, origin))
        return {"state": "cancelled"}

    broker = SimpleNamespace(epoch="b" * 32, profile_jobs=SimpleNamespace(operator_cancel=cancel))

    async def unused(scope, receive, send):
        raise AssertionError("unexpected delegation")

    api = OperatorAPI(unused, broker, lambda header: current)
    body = json.dumps({"job_id": "job", "plan_hash": "a" * 64, "broker_epoch": "b" * 32}).encode()

    async def receive():
        nonlocal current
        current = final
        return {"type": "http.request", "body": body, "more_body": False}

    output = []

    async def send(message):
        output.append(message)

    await api(
        {
            "type": "http",
            "path": "/operator/job-cancel",
            "method": "POST",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )
    assert output[0]["status"] == 200
    assert calls == [("job", "a" * 64, "b" * 32, "final", "local_cli")]
