"""Real localhost HTTP/MCP timeline flow with synthetic auth and mock captures."""

import time

import httpx2
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.gui_images import ImagePreview
from cardputer_mcp.gui_timeline import TimelineSnapshot
from cardputer_mcp.models import Origin


async def test_timeline_keeps_job_context_from_plan_through_image_delivery(registry_service):
    service = registry_service
    service.resident.settings = service.resident.settings.model_copy(
        update={"allow_mock_execution": False}
    )
    async with (
        httpx2.AsyncClient(
            base_url=service.url.removesuffix("/mcp"),
            trust_env=False,
            headers={"Authorization": "Bearer " + "a" * 64},
        ) as local,
        connect(service.url, service.keys["second"].token) as remote,
    ):
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        plan = await call(
            remote,
            "prepare_test",
            {"session": lease["session"], "job_id": "timeline-capture", "test": "capture_home"},
        )
        started_at = time.monotonic()
        timeline = (await local.get("/operator/timeline")).json()
        parsed = TimelineSnapshot.parse(timeline, started_at)
        assert parsed.cards[0].review is None
        assert timeline["ok"] and not timeline["plans_available"]
        card = next(row for row in timeline["cards"] if row["job_id"] == "timeline-capture")
        assert card["state"] == "prepared" and card["image"] is None
        assert card["plan_hash"] == plan["plan_hash"]
        retired = await local.post("/operator/approve-plan", json={"plan_hash": plan["plan_hash"]})
        assert retired.status_code == 410 and service.backend.calls == 0
        executed = await call(
            remote, "execute_test", {"session": lease["session"], "plan_hash": plan["plan_hash"]}
        )
        assert executed["ok"]
        await service.resident.queue.join()
        completed = (await local.get("/operator/timeline")).json()
        completed_model = TimelineSnapshot.parse(completed, time.monotonic())
        card = next(row for row in completed["cards"] if row["job_id"] == "timeline-capture")
        assert card["state"] == "succeeded" and card["image"]["delivery_state"] == "available"
        assert "image_base64" not in str(completed)
        allowed = await remote.call_tool(
            "read_capture", {"request": {"job_id": "timeline-capture"}}
        )
        assert not allowed.is_error
        preview = (await local.post("/operator/image", json={"job_id": "timeline-capture"})).json()
        preview_row = next(
            item.image for item in completed_model.cards if item.job_id == "timeline-capture"
        )
        assert preview_row is not None
        assert ImagePreview.parse(preview, preview_row, time.monotonic()).png
        assert preview["image_base64"]
        assert service.backend.calls == 1
        retired = await local.post("/operator/image-decision", json={"job_id": "timeline-capture"})
        assert retired.status_code == 410
        image = await remote.call_tool("read_capture", {"request": {"job_id": "timeline-capture"}})
        assert not image.is_error and any(item.type == "image" for item in image.content)
        assert service.backend.calls == 1


async def test_timeline_route_is_local_operator_only_and_never_an_mcp_tool(registry_service):
    service = registry_service
    reader = service.registry.issue("Synthetic local reader", Origin.LOCAL, scopes={"read"})
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False
    ) as http:
        for token in ("b" * 64, service.keys["second"].token, reader.token):
            response = await http.get(
                "/operator/timeline", headers={"Authorization": "Bearer " + token}
            )
            assert response.status_code == 403 and "cards" not in response.text
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
