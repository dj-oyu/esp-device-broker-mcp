"""Read-only identity inspection is authenticated, current, and credential-free."""

import json

import httpx2
from test_authenticated_mock import LOCAL, REMOTE, authenticated_mock  # noqa: F401
from test_http_integration import service  # noqa: F401
from test_registry_http import registry_service  # noqa: F401

from cardputer_mcp.client import call, connect


async def test_identity_reports_request_principal_without_acquiring(authenticated_mock):  # noqa: F811
    url, resident = authenticated_mock
    for token, expected in ((LOCAL, "local_cli"), (REMOTE, "remote_mcp")):
        async with connect(url, token) as client:
            result = await call(client, "client_identity")
        assert result == {
            "ok": True,
            "authenticated": True,
            "origin": expected,
            "principal_id": f"ephemeral:{expected}",
            "scopes": ["control", "read"],
            "tunnel_ingress_enabled": False,
        }
        assert token not in json.dumps(result)
    assert resident.active_job is None and resident.session is None
    assert not resident.backend.hardware_connected


async def test_identity_remains_auth_required_on_legacy_mock(service):  # noqa: F811
    async with connect(service) as client:
        assert await call(client, "client_identity") == {
            "ok": False,
            "error": "authentication_required",
        }


async def test_identity_read_key_and_revocation(registry_service):  # noqa: F811
    svc = registry_service
    key = svc.keys["reader"]
    async with connect(svc.url, key.token) as client:
        result = await call(client, "client_identity")
        assert result["origin"] == "remote_mcp"
        assert result["principal_id"] == key.metadata.principal_id
        assert result["scopes"] == ["read"]
        assert key.token not in json.dumps(result)

    svc.registry.revoke(key.metadata.key_id)
    async with httpx2.AsyncClient(trust_env=False) as http:
        denied = await http.get(svc.url, headers={"Authorization": "Bearer " + key.token})
        assert denied.status_code == 401
        assert key.token not in denied.text
