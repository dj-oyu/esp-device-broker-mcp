"""Retired product review endpoints keep authentication, scopes and zero effects."""
from types import SimpleNamespace

import httpx2
import pytest

from cardputer_mcp.models import Origin
from cardputer_mcp.operator_api import OperatorAPI, RETIRED_ROUTES


@pytest.mark.parametrize("path", sorted(RETIRED_ROUTES))
@pytest.mark.parametrize("identity,status", [(None, 401), ("remote", 403), ("read", 403), ("local", 410)])
async def test_old_operator_endpoint_is_authenticated_terminal_and_never_executes(path, identity, status):
    identities = {
        "local": SimpleNamespace(principal_id="local", origin=Origin.LOCAL, scopes=frozenset({"read", "control"})),
        "read": SimpleNamespace(principal_id="local", origin=Origin.LOCAL, scopes=frozenset({"read"})),
        "remote": SimpleNamespace(principal_id="remote", origin=Origin.REMOTE, scopes=frozenset({"read", "control"})),
    }
    async def unexpected(scope, receive, send):
        raise AssertionError("Retired route must not delegate to a device or MCP app")
    # No broker methods exist: any attempted plan/state/device mutation fails.
    broker = SimpleNamespace()
    api = OperatorAPI(unexpected, broker, lambda _header: identities.get(identity))
    method = "GET" if path.endswith(("/plans", "/policies", "/policy-options")) else "POST"
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=api), base_url="http://localhost") as client:
        response = await client.request(method, path, json={"old_pending": "never execute"})
    assert response.status_code == status
    assert response.json()["ok"] is False
    if status == 410:
        assert response.json()["error"] == "product_approval_retired"
    assert vars(broker) == {}
