"""Unrelated device recovery history cannot gate explicit non-USB profile jobs."""

import asyncio

import pytest
from test_profile_jobs import register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.broker import BrokerError, digest_plan
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SourceRepository
from cardputer_mcp.models import AcquireRequest, Origin


@pytest.mark.parametrize("operation", ["firmware_fetch_source", "firmware_build"])
@pytest.mark.parametrize("boundary", ["prepare", "execute"])
@pytest.mark.parametrize("blocker", ["device_job", "device_request"])
async def test_unrelated_device_review_does_not_gate_explicit_profile_job(
    registry_service, tmp_path, monkeypatch, operation, boundary, blocker
):
    service = registry_service
    broker = service.resident
    project = register(service, tmp_path).model_copy(
        update={
            "source_repository": SourceRepository(
                github_repository="synthetic/firmware",
                cache_root=tmp_path / "source-cache",
                private_acl_confirmed=True,
            )
        }
    )
    broker.settings = broker.settings.model_copy(update={"build_projects": {"fixture": project}})
    effects = []

    async def source(plan, stage, live):
        assert live() is True
        effects.append(plan["job_id"])
        return "source_snapshot_fetched_no_build_no_flash"

    async def build(plan, stage, *, defer_publish):
        assert defer_publish and broker.firmware_build.authority() is True
        effects.append(plan["job_id"])
        return "build_completed_no_device_write"

    def commit_source(plan, result, live):
        assert live() is True
        broker._store().set_job(plan["job_id"], "succeeded", result)

    def commit_build(plan, live):
        assert live() is True

    monkeypatch.setattr(broker.firmware_source, "execute", source)
    monkeypatch.setattr(broker.firmware_source, "commit_success", commit_source)
    monkeypatch.setattr(broker.firmware_build, "execute", build)
    monkeypatch.setattr(broker.firmware_build, "commit_deferred", commit_build)
    request = {"job_id": "profile-candidate", "test": operation, "project_id": "fixture"}
    if operation == "firmware_fetch_source":
        request["source_commit"] = "a" * 40

    async with connect(service.url, service.keys["second"].token) as remote:
        prepared = await call(remote, "prepare_test", request)
        assert prepared["ok"] and prepared["approval_required"] is False
        store = broker._store()
        if blocker == "device_job":
            old_plan = {"job_id": "old-device", "test": "derby_smoke"}
            digest = digest_plan(old_plan)
            store.put_plan(digest, old_plan)
            store.add_job("old-device", digest)
            store.set_stage("old-device", "serial_close")
            store.set_job("old-device", "needs_review", "serial_marker_timeout")
            historical = store.job("old-device")
        else:
            store.put_request("old-request", None, 30, state="needs_review")
            historical = store.request("old-request")
        assert store.needs_review()
        with pytest.raises(BrokerError, match="recovery_review_required"):
            broker.acquire(
                AcquireRequest(owner=Origin.REMOTE),
                principal_id=service.keys["second"].metadata.principal_id,
            )
        if boundary == "prepare":
            request["job_id"] = "later-profile"
            prepared = await call(remote, "prepare_test", request)
            assert prepared["ok"] and prepared["approval_required"] is False
        await asyncio.sleep(0)
        assert effects == [] and broker.profile_jobs.queue.empty()
        assert store.job(request["job_id"]) is None
        result = await call(remote, "execute_test", {"plan_hash": prepared["plan_hash"]})
        assert result["ok"] is True
        await asyncio.wait_for(broker.profile_jobs.queue.join(), 3)
        assert store.job(request["job_id"])["state"] == "succeeded"
        assert effects == [request["job_id"]]
        assert broker.session is None and service.backend.calls == 0
        assert store.needs_review()
        if blocker == "device_job":
            assert store.job("old-device") == historical
        else:
            assert store.request("old-request") == historical
