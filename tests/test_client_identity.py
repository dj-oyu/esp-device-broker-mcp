"""Read-only identity inspection is authenticated, current, and credential-free."""

import json

from test_authenticated_mock import LOCAL, REMOTE, authenticated_mock  # noqa: F401

from cardputer_mcp.client import call, connect


async def test_identity_reports_request_principal_without_acquiring(authenticated_mock):  # noqa: F811
    url, resident = authenticated_mock
    for token, expected in ((LOCAL, "local_cli"), (REMOTE, "remote_mcp")):
        async with connect(url, token) as client:
            result = await call(client, "status", {"view": "identity"})
        assert result == {
            "ok": True,
            "authenticated": True,
            "origin": expected,
            "principal_id": f"ephemeral:{expected}",
            "scopes": ["control", "read"],
            "tunnel_ingress_enabled": False,
            "tool_set_version": 2,
        }
        assert token not in json.dumps(result)
    assert resident.active_job is None and resident.session is None
    assert not resident.backend.hardware_connected
