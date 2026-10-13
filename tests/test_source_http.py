"""Authenticated MCP and local approval with synthetic source transport."""

import json

import httpx2
from test_registry_http import registry_service as registry_service
from test_source_snapshot import COMMIT, FILES, REPOSITORY, archive

from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject, SourceRepository
from cardputer_mcp.source_snapshot import SourceError, file_receipt, tree_hash


async def test_explicit_source_approval_then_owned_receipt_build_plan(registry_service, tmp_path):
    service = registry_service
    key = service.keys["second"]
    profile = tmp_path / "profile.ps1"
    profile.write_text("synthetic")
    project = BuildProject(
        project_dir=tmp_path / "user-worktree", trusted_profile=profile,
        build_root=tmp_path / "builds", build_min_free_bytes=0,
        source_repository=SourceRepository(
            github_repository=REPOSITORY, cache_root=tmp_path / "source-cache",
            private_acl_confirmed=True,
        ),
    )
    settings = service.resident.settings.model_copy(update={"build_projects": {"fixture": project}})
    service.resident.settings = settings
    service.resident.firmware_build.settings = settings
    service.resident.firmware_source.settings = settings
    service.resident.firmware_source.snapshots.settings = settings
    calls = []
    tree = tree_hash({name: file_receipt(data, "100644") for name, data in FILES.items()})

    def transport(url, deadline, live, limit):
        calls.append(url)
        if not live():
            raise SourceError("source_fetch_interrupted")
        return (json.dumps({"sha": COMMIT, "tree": {"sha": tree}}).encode()
                if url.startswith("https://api.github.com/") else archive())

    service.resident.firmware_source.snapshots.transport = transport
    async with (
        httpx2.AsyncClient(
            base_url=service.url.removesuffix("/mcp"), trust_env=False,
            headers={"Authorization": "Bearer " + "a" * 64},
        ) as http,
        connect(service.url, key.token) as remote,
    ):
        catalog = await remote.list_tools()
        schema = next(tool.input_schema for tool in catalog.tools if tool.name == "prepare_test")
        assert "firmware_fetch_source" in json.dumps(schema)
        assert "source_commit" in json.dumps(schema) and "source_receipt" in json.dumps(schema)
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
        prepared = await call(remote, "prepare_test", {
            "session": lease["session"], "job_id": "fetch-one", "test": "firmware_fetch_source",
            "project_id": "fixture", "source_commit": COMMIT,
        })
        assert prepared["ok"] and prepared["approval_required"], prepared
        assert prepared["storage"]["source_cache"]["snapshots"] == 0
        assert not calls
        execution = {"session": lease["session"], "plan_hash": prepared["plan_hash"]}
        assert (await call(remote, "execute_test", execution))["error"] == "operator_approval_required"
        detail = (await http.post("/operator/plan", json={"plan_hash": prepared["plan_hash"]})).json()
        assert detail["attestation"] == "none"
        approved = await http.post("/operator/approve-plan", json={
            "plan_hash": prepared["plan_hash"], "review_revision": detail["review_revision"],
            "attestation": "",
        })
        assert approved.status_code == 200 and approved.json()["ok"], approved.text
        assert (await call(remote, "execute_test", execution))["ok"]
        await service.resident.queue.join()
        job = service.resident.job("fetch-one")
        assert job["state"] == "succeeded", json.dumps(job)
        logs = await call(remote, "read_build_logs", {"job_id": "fetch-one"})
        assert logs["source"]["commit"] == COMMIT
        assert logs["source_cache"]["snapshots"] == 1
        receipt = logs["source"]["source_receipt"]
        assert service.backend.calls == 0 and not service.resident.firmware_build.manifests
        assert not project.project_dir.exists()
        built = await call(remote, "prepare_test", {
            "session": lease["session"], "job_id": "build-one", "test": "firmware_build",
            "project_id": "fixture", "source_receipt": receipt,
        })
        assert built["ok"] and built["approval_required"], built
        assert built["plan"]["source_provenance"]["commit"] == COMMIT
        source, _ = service.resident.firmware_source.snapshots.resolve("fixture", receipt, key.metadata.principal_id)
        (source / "CMakeLists.txt").write_bytes(b"modified after prepare")
        changed = await call(remote, "prepare_test", {
            "session": lease["session"], "job_id": "changed-build", "test": "firmware_build",
            "project_id": "fixture", "source_receipt": receipt,
        })
        assert not changed["ok"]
