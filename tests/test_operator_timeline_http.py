"""Real localhost HTTP/MCP timeline flow with synthetic auth and mock captures."""

import httpx2
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import connect
from cardputer_mcp.models import Origin


async def test_timeline_route_is_local_operator_only_and_never_an_mcp_tool(registry_service):
    service = registry_service
    reader = service.registry.issue("Synthetic local reader", Origin.LOCAL, scopes={"read"})
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False
    ) as http:
        for token in ("b" * 64, service.keys["second"].token):
            response = await http.get(
                "/operator/timeline", headers={"Authorization": "Bearer " + token}
            )
            assert response.status_code == 403 and "cards" not in response.text
        # A local read-only key may read the timeline but is offered no actions.
        response = await http.get(
            "/operator/timeline", headers={"Authorization": "Bearer " + reader.token}
        )
        assert response.status_code == 200 and response.json()["ok"]
        for card in response.json()["cards"]:
            assert not card["cancel_available"] and not card["stop_available"]
        assert (await http.get("/operator/timeline")).status_code == 401
        headers = {"Authorization": "Bearer " + "a" * 64}
        assert (await http.post("/operator/timeline", json={}, headers=headers)).status_code == 405
        assert (
            await http.get("/operator/timeline?secret=fixture", headers=headers)
        ).status_code == 400
        for extra in ({"Origin": "https://example.invalid"}, {"X-Forwarded-For": "127.0.0.1"}):
            response = await http.get("/operator/timeline", headers=headers | extra)
            assert response.status_code == 403 and "cards" not in response.text
    async with connect(service.url, service.keys["second"].token) as remote:
        catalog = await remote.list_tools()
        assert not any("timeline" in tool.name or "approve" in tool.name for tool in catalog.tools)
    assert service.backend.calls == 0
