"""Retired policy/arm endpoints still authenticate; none starts an operation."""

from types import SimpleNamespace

import pytest

from cardputer_mcp.models import Origin
from cardputer_mcp.operator_api import OperatorAPI


@pytest.mark.parametrize("method,path", [
    ("GET", "/operator/policy-options"),
    ("GET", "/operator/policies"),
    ("POST", "/operator/policy-create"),
    ("POST", "/operator/policy-update"),
    ("POST", "/operator/policy-revoke"),
    ("POST", "/operator/capture-arm"),
])
@pytest.mark.parametrize("identity,status", [
    ("missing", 401), ("revoked", 401), ("remote", 403), ("reader", 403), ("operator", 410),
])
async def test_policy_retirement_requires_authentication_before_gone(method, path, identity, status):
    principal = None if identity in ("missing", "revoked") else SimpleNamespace(
        principal_id="synthetic-client",
        origin=Origin.REMOTE if identity == "remote" else Origin.LOCAL,
        scopes=frozenset({"read"}) if identity == "reader" else frozenset({"read", "control"}),
    )
    headers_seen = []

    def authenticate(header):
        headers_seen.append(header)
        return principal

    async def never_called(*args):
        raise AssertionError("retired requests must not read a body, dispatch or touch hardware")

    api = OperatorAPI(never_called, SimpleNamespace(), authenticate)
    messages = []

    async def send(message):
        messages.append(message)

    header = [] if identity == "missing" else [(b"authorization", b"Bearer synthetic-token")]
    await api(
        {"type": "http", "path": path, "method": method, "headers": header, "query_string": b""},
        never_called,
        send,
    )
    assert len(headers_seen) == 1
    assert messages[0]["status"] == status
    if status == 410:
        assert b"product_approval_retired" in messages[1]["body"]
