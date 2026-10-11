"""A POST body wait must not preserve a revoked operator grant."""

import json
from types import SimpleNamespace

import pytest

from cardputer_mcp.models import Origin
from cardputer_mcp.operator_api import OperatorAPI


@pytest.mark.parametrize(
    "route,body",
    [
        ("approve-plan", {"plan_hash": "a" * 64, "review_revision": "b" * 64, "attestation": ""}),
        (
            "image-decision",
            {
                "job_id": "fixture",
                "sha256": "a" * 64,
                "delivery_revision": "b" * 64,
                "decision": "approve",
            },
        ),
        ("policy-revoke", {"policy_id": "capture_policy_" + "a" * 32, "expected_revision": 1}),
        ("release", {"lease_id": "fixture"}),
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
