"""Retired operator requests authenticate first and cannot queue serial work."""

import httpx2
import pytest
from test_derby_http import derby_service as derby_service
from test_registry_http import LOCAL
from test_serial_http import serial_service as serial_service


@pytest.mark.parametrize(
    "route",
    ["approve-plan", "capture-arm"],
)
async def test_retired_operator_mutations_never_execute(derby_service, route):
    env = derby_service
    async with httpx2.AsyncClient(base_url=env.url.removesuffix("/mcp"), trust_env=False) as http:
        response = await http.post("/operator/" + route, json={})
        assert response.status_code == 401
        response = await http.post(
            "/operator/" + route,
            json={},
            headers={"Authorization": "Bearer " + env.keys["second"].token},
        )
        assert response.status_code == 403
        response = await http.post(
            "/operator/" + route,
            json={"attestation": "DEVICE HOME AND FIRMWARE VERIFIED"},
            headers={"Authorization": "Bearer " + LOCAL},
        )
        assert response.status_code == 410
        assert response.json() == {"error": "request_rejected"}
    assert env.resident.queue.empty() and env.resident.active_job is None
    assert env.resident._store().record_count("jobs") == 0
    assert env.resident._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
