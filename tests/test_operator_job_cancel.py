"""Local operator stop authority is narrow; remote ownership and cleanup stay intact."""

import asyncio
import json

import httpx2
import pytest
from test_profile_jobs import register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.client import call, connect
from cardputer_mcp.gui_timeline import TimelineSnapshot
from cardputer_mcp.models import Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action


def assert_refused(response):
    assert response.status_code in (400, 401, 403) or (
        response.status_code == 200 and response.json().get("ok") is False
    )


def local_operator(service):
    return service.registry.issue("Synthetic local stop operator", Origin.LOCAL, scopes={"control"})


def headers(key):
    return {"Authorization": "Bearer " + key.token}


def target(broker, prepared, job="cancel-job"):
    return dict(job_id=job, plan_hash=prepared["plan_hash"], broker_epoch=broker.epoch)


@pytest.mark.parametrize("uncertain", [False, True])
async def test_actual_cancel_http_waits_for_cleanup_and_retains_uncertainty(
    registry_service, tmp_path, monkeypatch, uncertain
):
    service = registry_service
    register(service, tmp_path)
    broker, key = service.resident, service.keys["second"]
    actor = local_operator(service)
    started, finish = asyncio.Event(), asyncio.Event()

    async def build(plan, stage, *, defer_publish):
        started.set()
        await finish.wait()
        if uncertain:
            broker.firmware_build.output_uncertain = True
            raise ValueError("synthetic private cleanup text")
        broker.firmware_build._check_cancelled()
        raise AssertionError("cancelled build cannot succeed")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    async with (
        connect(service.url, key.token) as client,
        httpx2.AsyncClient(
            base_url=service.url.removesuffix("/mcp"), trust_env=False, headers=headers(actor)
        ) as http,
    ):
        prepared = await call(
            client,
            "submit",
            dict(job_id="cancel-job", kind="build", project_id="fixture"),
        )
        assert prepared["ok"]
        assert (await call(client, "execute", {"op": "run", "plan_hash": prepared["plan_hash"]}))[
            "ok"
        ]
        await asyncio.wait_for(started.wait(), 2)
        try:
            listing = (await http.get("/operator/timeline")).json()
            row = next(row for row in listing["cards"] if row["job_id"] == "cancel-job")
            assert row["cancel_available"] and row["state"] == "running"
            # Remote clients retain their existing same-principal restriction.
            async with connect(service.url, service.keys["first"].token) as other:
                refused = await call(other, "cancel", {"target": "job", "job_id": "cancel-job"})
                assert refused["ok"] is False
            assert not broker.firmware_build.process.stop_requested.is_set()
            stopped = (
                await http.post("/operator/job-cancel", json=target(broker, prepared))
            ).json()
            assert stopped["ok"] and stopped["stop_requested"] and stopped["state"] == "running"
            assert broker.job("cancel-job")["state"] == "running"
            assert not broker.profile_jobs.queue.empty() or broker.profile_jobs.active is not None
            assert broker.session is None and service.backend.calls == 0
        finally:
            finish.set()
            await broker.profile_jobs.queue.join()
        status = broker.job("cancel-job")
        assert status["state"] == ("needs_review" if uncertain else "cancelled")
        listing = (await http.get("/operator/timeline")).json()
        row = next(row for row in listing["cards"] if row["job_id"] == "cancel-job")
        assert row["state"] == status["state"] and not row["cancel_available"]
        parsed = TimelineSnapshot.parse(listing, broker.clock())
        shown = next(card for card in parsed.cards if card.job_id == "cancel-job")
        assert shown.state == status["state"] and shown.result == status["result"]
        assert "private cleanup" not in json.dumps(row)
        assert not broker.firmware_build.manifests
        assert (
            broker._store().plan(prepared["plan_hash"])["principal_id"] == key.metadata.principal_id
        )
        assert broker._store().plan(prepared["plan_hash"])["origin"] == Origin.REMOTE.value
        if not uncertain:
            assert status["result"] == "operator_cancelled"
    broker.firmware_build.output_uncertain = False


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled", "needs_review"])
async def test_completion_race_is_a_read_of_current_terminal_job(
    registry_service, tmp_path, monkeypatch, state
):
    service = registry_service
    register(service, tmp_path)
    broker, key = service.resident, service.keys["second"]
    actor = local_operator(service)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="cancel-job", test=Action.BUILD, project_id="fixture"),
        key.metadata.principal_id,
        key.metadata.origin.value,
    )
    store = broker._store()
    store.add_job("cancel-job", prepared["plan_hash"])
    store.set_job("cancel-job", state)
    monkeypatch.setattr(
        broker.profile_jobs, "_stop", lambda *args: pytest.fail("must not stop completed work")
    )
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False, headers=headers(actor)
    ) as http:
        response = (await http.post("/operator/job-cancel", json=target(broker, prepared))).json()
    assert response["ok"] and response["state"] == state
    assert response["stop_requested"] is False
    assert broker.job("cancel-job")["state"] == state
    assert not broker.profile_jobs.stops and service.backend.calls == 0


async def test_endpoint_rejects_remote_scope_epoch_plan_and_device_jobs(registry_service, tmp_path):
    service = registry_service
    register(service, tmp_path)
    broker, key = service.resident, service.keys["second"]
    actor = local_operator(service)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="cancel-job", test=Action.BUILD, project_id="fixture"),
        key.metadata.principal_id,
        key.metadata.origin.value,
    )
    store = broker._store()
    store.add_job("cancel-job", prepared["plan_hash"])
    read_only = service.registry.issue("Synthetic local reader", Origin.LOCAL, scopes={"read"})
    async with connect(service.url, actor.token) as local:
        for tool, arguments in (
            ("execute", {"op": "run", "plan_hash": prepared["plan_hash"]}),
            ("cancel", {"target": "job", "job_id": "cancel-job"}),
        ):
            assert (await call(local, tool, arguments))["ok"] is False
            assert broker.job("cancel-job")["state"] == "queued"
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False, headers=headers(actor)
    ) as http:
        for invalid in (
            {**target(broker, prepared), "broker_epoch": "0" * 32},
            {**target(broker, prepared), "plan_hash": "0" * 64},
            {**target(broker, prepared), "job_id": "other"},
            {**target(broker, prepared), "principal_id": key.metadata.principal_id},
            {**target(broker, prepared), "session": "synthetic-lease"},
        ):
            rejected = await http.post("/operator/job-cancel", json=invalid)
            assert_refused(rejected)
            assert broker.job("cancel-job")["state"] == "queued"
        for credential in (service.keys["first"], service.keys["second"], read_only):
            response = await http.post(
                "/operator/job-cancel", json=target(broker, prepared), headers=headers(credential)
            )
            assert_refused(response)
            assert broker.job("cancel-job")["state"] == "queued"
        plan = dict(
            job_id="device-job",
            test="firmware_flash",
            session_id="synthetic-lease",
            principal_id=key.metadata.principal_id,
            origin=Origin.LOCAL.value,
        )
        digest = digest_plan(plan)
        store.put_plan(digest, plan)
        store.add_job("device-job", digest)
        response = await http.post(
            "/operator/job-cancel",
            json=dict(
                job_id="device-job",
                plan_hash=digest,
                broker_epoch=broker.epoch,
            ),
        )
        assert_refused(response)
        assert broker.job("device-job")["state"] == "queued"
        assert service.backend.calls == 0 and not broker.profile_jobs.stops
        cancelled = (await http.post("/operator/job-cancel", json=target(broker, prepared))).json()
        assert cancelled["ok"] and cancelled["state"] == "cancelled"
        assert cancelled["stop_requested"] is False
        assert broker.job("cancel-job")["result"] == "operator_cancelled"


async def test_source_job_uses_existing_stop_path_across_principals(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    register(service, tmp_path)
    broker, key = service.resident, service.keys["second"]
    actor = local_operator(service)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="cancel-job", test=Action.BUILD, project_id="fixture"),
        key.metadata.principal_id,
        key.metadata.origin.value,
    )
    plan = dict(prepared["plan"], test=Action.SOURCE.value)
    digest = digest_plan(plan)
    store = broker._store()
    store.put_plan(digest, plan)
    store.add_job("cancel-job", digest)
    store.set_job("cancel-job", "running")
    stopped = []
    monkeypatch.setattr(
        broker.firmware_source, "request_stop", lambda job: stopped.append(job) or True
    )
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False, headers=headers(actor)
    ) as http:
        result = (
            await http.post(
                "/operator/job-cancel",
                json=dict(job_id="cancel-job", plan_hash=digest, broker_epoch=broker.epoch),
            )
        ).json()
    assert result["ok"] and result["stop_requested"] and result["state"] == "running"
    assert stopped == ["cancel-job"] and broker.job("cancel-job")["state"] == "running"
    store.set_job("cancel-job", "cancelled")


@pytest.mark.parametrize(
    "test",
    [
        "firmware_flash",
        "firmware_restore",
        "firmware_backup",
        "diagnostics",
        "capture_home",
        "restart_and_observe",
    ],
)
async def test_local_operator_stop_grant_excludes_other_job_types(registry_service, tmp_path, test):
    service = registry_service
    register(service, tmp_path)
    broker, key = service.resident, service.keys["second"]
    actor = local_operator(service)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="cancel-job", test=Action.BUILD, project_id="fixture"),
        key.metadata.principal_id,
        key.metadata.origin.value,
    )
    plan = dict(prepared["plan"], test=test)
    digest = digest_plan(plan)
    store = broker._store()
    store.put_plan(digest, plan)
    store.add_job("cancel-job", digest)
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False, headers=headers(actor)
    ) as http:
        response = await http.post(
            "/operator/job-cancel",
            json=dict(job_id="cancel-job", plan_hash=digest, broker_epoch=broker.epoch),
        )
    assert_refused(response)
    assert broker.job("cancel-job")["state"] == "queued"
    assert not broker.profile_jobs.stops and broker.session is None and service.backend.calls == 0


async def test_operator_stop_requires_current_project_registration(registry_service, tmp_path):
    service = registry_service
    register(service, tmp_path)
    broker, key = service.resident, service.keys["second"]
    actor = local_operator(service)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="cancel-job", test=Action.BUILD, project_id="fixture"),
        key.metadata.principal_id,
        key.metadata.origin.value,
    )
    store = broker._store()
    store.add_job("cancel-job", prepared["plan_hash"])
    broker.settings = broker.settings.model_copy(update={"build_projects": {}})
    async with httpx2.AsyncClient(
        base_url=service.url.removesuffix("/mcp"), trust_env=False, headers=headers(actor)
    ) as http:
        assert_refused(await http.post("/operator/job-cancel", json=target(broker, prepared)))
    assert broker.job("cancel-job")["state"] == "queued" and not broker.profile_jobs.stops
