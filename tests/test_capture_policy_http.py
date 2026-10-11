"""Actual localhost policy/approval requests with synthetic identities and mock images."""

import httpx2
import pytest
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.models import Origin


async def configure(service, http, principal, **changes):
    options = (await http.get("/operator/policy-options")).json()
    assert options["ok"]
    data = {
        "principal_id": principal,
        "mode": "persistent",
        "capture_home": True,
        "image_delivery": True,
        "ttl_seconds": 5,
        "expected_target": options["target"],
        "acknowledge_effects": True,
    } | changes
    response = await http.post("/operator/policy-create", json=data)
    return response.json()


def setup(service):
    service.resident.settings = service.resident.settings.model_copy(
        update={"allow_mock_execution": False}
    )
    service.resident.capture_policies.wall_clock = lambda: service.now[0]


async def test_policy_allows_capture_and_image_then_revocation_blocks_future_reads(
    registry_service,
):
    service = registry_service
    setup(service)
    key = service.keys["first"]
    base = service.url.removesuffix("/mcp")
    async with (
        httpx2.AsyncClient(
            base_url=base, trust_env=False, headers={"Authorization": "Bearer " + "a" * 64}
        ) as http,
        connect(service.url, key.token) as remote,
    ):
        grant = await configure(service, http, key.metadata.principal_id)
        assert grant["ok"], grant
        policy = grant["policy"]
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        plan = await call(
            remote,
            "prepare_test",
            {"session": lease["session"], "job_id": "policy-image", "test": "capture_home"},
        )
        assert plan["ok"]
        assert plan["approval_required"] is False and plan["policy_permit_available"] is True
        executed = await call(
            remote, "execute_test", {"session": lease["session"], "plan_hash": plan["plan_hash"]}
        )
        assert executed["ok"], executed
        await service.resident.queue.join()
        assert service.resident.job("policy-image")["state"] == "succeeded"
        image = await remote.call_tool("read_capture", {"request": {"job_id": "policy-image"}})
        assert not image.is_error
        assert any(item.type == "image" for item in image.content)
        assert service.resident.captures["policy-image"].delivery_state == "pending"
        revoked = await http.post(
            "/operator/policy-revoke",
            json={"policy_id": policy["policy_id"], "expected_revision": policy["revision"]},
        )
        assert revoked.json()["ok"]
        denied = await remote.call_tool("read_capture", {"request": {"job_id": "policy-image"}})
        assert denied.is_error
        assert denied.structured_content["error"] == "capture_delivery_approval_required"
        assert service.backend.calls == 1
        assert (
            service.resident._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0]
            == 0
        )


async def test_default_requires_manual_plan_and_image_approval(registry_service):
    service = registry_service
    setup(service)
    base = service.url.removesuffix("/mcp")
    async with (
        httpx2.AsyncClient(
            base_url=base, trust_env=False, headers={"Authorization": "Bearer " + "a" * 64}
        ) as http,
        connect(service.url, service.keys["second"].token) as remote,
    ):
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        prepared = await call(
            remote,
            "prepare_test",
            {"session": lease["session"], "job_id": "manual-image", "test": "capture_home"},
        )
        denied = await call(
            remote,
            "execute_test",
            {"session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        assert denied["error"] == "operator_approval_required"
        plans = (await http.get("/operator/plans")).json()
        assert [item["plan_hash"] for item in plans["plans"]] == [prepared["plan_hash"]]
        detail = (
            await http.post("/operator/plan", json={"plan_hash": prepared["plan_hash"]})
        ).json()
        approved = await http.post(
            "/operator/approve-plan",
            json={
                "plan_hash": prepared["plan_hash"],
                "review_revision": detail["review_revision"],
                "attestation": "",
            },
        )
        assert approved.json()["ok"], approved.json()
        assert service.backend.calls == 0
        assert (
            await call(
                remote,
                "execute_test",
                {"session": lease["session"], "plan_hash": prepared["plan_hash"]},
            )
        )["ok"]
        await service.resident.queue.join()
        image = await remote.call_tool("read_capture", {"request": {"job_id": "manual-image"}})
        assert image.is_error
        preview = (await http.post("/operator/image", json={"job_id": "manual-image"})).json()
        assert preview["image_base64"] and not preview["policy_allowed"]
        result = await http.post(
            "/operator/image-decision",
            json={
                "job_id": "manual-image",
                "sha256": preview["sha256"],
                "delivery_revision": preview["delivery_revision"],
                "decision": "approve",
            },
        )
        assert result.json()["ok"]
        image = await remote.call_tool("read_capture", {"request": {"job_id": "manual-image"}})
        assert not image.is_error
        assert service.backend.calls == 1


async def test_remote_and_read_only_cannot_modify_any_policy(registry_service):
    service = registry_service
    setup(service)
    readonly = service.registry.issue("Local reader", Origin.LOCAL, scopes={"read"})
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False
    ) as http:
        for token in ("b" * 64, service.keys["second"].token, readonly.token):
            headers = {"Authorization": "Bearer " + token}
            for path in ("policy-options", "policies", "plans", "images"):
                assert (await http.get("/operator/" + path, headers=headers)).status_code == 403
            for path in (
                "policy-create",
                "policy-update",
                "policy-revoke",
                "capture-arm",
                "approve-plan",
                "image-decision",
            ):
                assert (
                    await http.post("/operator/" + path, json={}, headers=headers)
                ).status_code == 403
    assert service.backend.calls == 0


@pytest.mark.parametrize("invalid", ["ephemeral", "scope", "missing_effects", "target"])
async def test_policy_creation_fails_closed_for_unsupported_requests(registry_service, invalid):
    service = registry_service
    setup(service)
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"),
        trust_env=False,
        headers={"Authorization": "Bearer " + "a" * 64},
    ) as http:
        principal = service.keys["reader" if invalid == "scope" else "second"].metadata.principal_id
        changes = {"principal_id": "ephemeral:remote_mcp"} if invalid == "ephemeral" else {}
        if invalid == "missing_effects":
            changes["acknowledge_effects"] = False
        if invalid == "target":
            changes["expected_target"] = "0" * 64
        response = await configure(service, http, principal, **changes)
        assert response.get("ok") is not True
        assert service.backend.calls == 0
