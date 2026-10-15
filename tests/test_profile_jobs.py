"""Registered profile execution without a USB lease or an approval intermediary."""

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from test_registry_http import registry_service as registry_service

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject, SourceRepository
from cardputer_mcp.models import (
    AcquireRequest,
    CancelRequest,
    ExecuteRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.source_snapshot import SourceError
from cardputer_mcp.store import Store


def register(service, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "CMakeLists.txt").write_text("synthetic source")
    profile = tmp_path / "profile.ps1"
    profile.write_text("synthetic trusted profile")
    project = BuildProject(
        project_dir=root,
        trusted_profile=profile,
        build_root=tmp_path / "builds",
        build_min_free_bytes=0,
    )
    service.resident.settings = service.resident.settings.model_copy(
        update={"build_projects": {"fixture": project}}
    )
    return project


def prepare(service, job):
    key = service.keys["second"].metadata
    return service.resident.profile_jobs.prepare(
        PrepareRequest(job_id=job, test=Action.BUILD, project_id="fixture"),
        key.principal_id,
        key.origin.value,
    )


@pytest.mark.parametrize(
    "stop", ["cancel", "revoke", "expire_key", "config", "profile", "deadline"]
)
async def test_running_profile_stop_is_terminal_without_review(
    registry_service, tmp_path, monkeypatch, stop
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"]
    started = asyncio.Event()

    async def build(plan, stage, *, defer_publish):
        assert defer_publish
        started.set()
        while not broker.firmware_build.process.stop_requested.is_set():
            await asyncio.sleep(0.01)
        broker.firmware_build._check_cancelled()
        raise AssertionError("stopped work cannot succeed")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    async with connect(service.url, key.token) as remote:
        plan = await call(
            remote,
            "prepare_test",
            {
                "job_id": "stopped",
                "test": "firmware_build",
                "project_id": "fixture",
            },
        )
        assert plan["ok"] and not plan["approval_required"]
        assert (await call(remote, "execute_test", {"plan_hash": plan["plan_hash"]}))["ok"]
        await asyncio.wait_for(started.wait(), 2)
        if stop == "cancel":
            result = await call(remote, "cancel_job", {"job_id": "stopped"})
            assert result["stop_requested"] and not result["requires_review"]
        elif stop == "revoke":
            service.registry.revoke(key.metadata.key_id)
        elif stop == "expire_key":
            service.now[0] = key.metadata.expires_at + 1
        elif stop == "profile":
            broker.settings.build_projects["fixture"].trusted_profile.write_text("changed profile")
        elif stop == "config":
            broker.settings = broker.settings.model_copy(update={"build_projects": {}})
        else:
            broker.profile_jobs.deadline = broker.clock() - 1
        await asyncio.wait_for(broker.profile_jobs.queue.join(), 2)
        assert broker.job("stopped")["state"] == "cancelled"
        assert not broker._store().needs_review()
        assert broker.session is None
        assert not broker.firmware_build.manifests
        assert service.backend.calls == 0


@pytest.mark.parametrize("change", ["cancel", "config", "revoke"])
async def test_change_at_build_success_publication_cannot_publish(
    registry_service, tmp_path, monkeypatch, change
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"].metadata
    original = broker.firmware_build.commit_deferred
    root = tmp_path / "candidate"
    root.mkdir()

    async def build(plan, stage, *, defer_publish):
        manifest = {"flash_authorized": False}
        (root / "broker-manifest.pending.json").write_text(json.dumps(manifest))
        broker.firmware_build._pending[plan["job_id"]] = (key.principal_id, root, manifest)
        broker.firmware_build.logs[plan["job_id"]] = BuildLogs(key.principal_id)
        return "firmware_build_succeeded_no_flash"

    def commit(plan, live):
        if change == "cancel":
            broker.profile_jobs.cancel(
                CancelRequest(job_id=plan["job_id"]), key.principal_id, key.origin.value
            )
        elif change == "config":
            broker.settings = broker.settings.model_copy(update={"build_projects": {}})
        else:
            service.registry.revoke(key.key_id)
        original(plan, live)

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    monkeypatch.setattr(broker.firmware_build, "commit_deferred", commit)
    plan = prepare(service, "publication-race")
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=plan["plan_hash"]), key.principal_id, key.origin.value
    )
    await broker.profile_jobs.queue.join()
    assert broker.job("publication-race")["state"] == "cancelled"
    assert not broker._store().needs_review()
    assert not (root / "broker-manifest.json").exists()
    assert not broker.firmware_build.manifests
    with pytest.raises(ValueError):
        broker.firmware_build.artifacts.resolve("publication-race", key.principal_id)


async def test_queue_cancel_read_scope_config_and_old_review_boundaries(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    project = register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"].metadata
    calls = []

    async def build(plan, stage, *, defer_publish):
        calls.append(plan["job_id"])
        raise ValueError("synthetic compiler failure")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    plan = prepare(service, "queued-cancel")
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=plan["plan_hash"]), key.principal_id, key.origin.value
    )
    broker.profile_jobs.cancel(
        CancelRequest(job_id="queued-cancel"), key.principal_id, key.origin.value
    )
    await broker.profile_jobs.queue.join()
    assert broker.job("queued-cancel")["state"] == "cancelled" and not calls
    prepared = prepare(service, "changed-config")
    broker.settings = broker.settings.model_copy(
        update={
            "build_projects": {
                "fixture": project.model_copy(update={"build_root": tmp_path / "other"})
            }
        }
    )
    with pytest.raises(BrokerError, match="profile_or_principal_no_longer_valid"):
        broker.profile_jobs.execute(
            ExecuteRequest(plan_hash=prepared["plan_hash"]), key.principal_id, key.origin.value
        )
    async with connect(service.url, service.keys["reader"].token) as reader:
        denied = await call(
            reader,
            "prepare_test",
            {
                "job_id": "read-denied",
                "test": "firmware_build",
                "project_id": "fixture",
            },
        )
        assert denied["error"] == "insufficient_scope"
    old_hash = "a" * 64
    broker._store().put_plan(old_hash, {"job_id": "old-review"})
    broker._store().add_job("old-review", old_hash)
    broker._store().set_job("old-review", "needs_review", "legacy_unreviewed")
    with pytest.raises(BrokerError, match="recovery_review_required"):
        prepare(service, "after-old-review")
    assert broker.job("old-review")["state"] == "needs_review"


@pytest.mark.parametrize("uncertainty", ["process", "output_lock", "database"])
async def test_actual_uncertainty_remains_fail_closed(
    registry_service, tmp_path, monkeypatch, uncertainty
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"].metadata

    async def build(plan, stage, *, defer_publish):
        if uncertainty == "process":
            broker.firmware_build.process.uncertain = True
        elif uncertainty == "output_lock":
            broker.firmware_build.output_uncertain = True
        else:
            raise sqlite3.DatabaseError("synthetic private storage text")
        raise ValueError("synthetic failure")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    plan = prepare(service, "uncertain")
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=plan["plan_hash"]), key.principal_id, key.origin.value
    )
    await broker.profile_jobs.queue.join()
    assert broker.job("uncertain")["state"] == "needs_review"
    with pytest.raises(BrokerError, match="recovery_review_required"):
        prepare(service, "cannot-follow")
    assert "private storage" not in json.dumps(broker.job("uncertain"))
    # Synthetic flags only; retain the durable unreviewed job.
    broker.firmware_build.process.uncertain = False
    broker.firmware_build.output_uncertain = False


def test_restart_does_not_replay_or_clear_old_review(tmp_path: Path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        for job, context in (
            ("profile-queued", "registered_project"),
            ("profile-running", "registered_project"),
            ("legacy-queued", "legacy"),
        ):
            digest = {"profile-queued": "a", "profile-running": "b", "legacy-queued": "c"}[job] * 64
            store.put_plan(digest, {"job_id": job, "execution_context": context})
            store.add_job(job, digest)
            if job == "profile-running":
                store.set_job(job, "running")
        store.recover()
        assert store.job("profile-queued")["state"] == "cancelled"
        assert store.job("profile-running")["state"] == "needs_review"
        assert store.job("legacy-queued")["state"] == "needs_review"
        assert store.needs_review()
    finally:
        store.close()


async def test_transport_disconnect_and_expired_device_lease_do_not_end_build(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    started, finish = asyncio.Event(), asyncio.Event()

    async def build(plan, stage, *, defer_publish):
        started.set()
        await finish.wait()
        broker.firmware_build._check_cancelled()
        raise ValueError("synthetic ordinary compile failure")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    async with connect(service.url, service.keys["second"].token) as remote:
        plan = await call(
            remote,
            "prepare_test",
            {
                "job_id": "long-build",
                "test": "firmware_build",
                "project_id": "fixture",
            },
        )
        assert (await call(remote, "execute_test", {"plan_hash": plan["plan_hash"]}))["ok"]
        await asyncio.wait_for(started.wait(), 2)
    # Client transport has closed; no cancellation or implicit replay.
    broker.acquire(
        AcquireRequest(owner=Origin.REMOTE, lease_seconds=1),
        principal_id=service.keys["first"].metadata.principal_id,
    )
    later = broker.clock() + 301
    monkeypatch.setattr(broker, "clock", lambda: later)
    broker.maintenance_tick()
    assert broker.session is None
    assert broker.job("long-build")["state"] == "running"
    assert broker.profile_jobs.deadline > later
    finish.set()
    await broker.profile_jobs.queue.join()
    assert broker.job("long-build")["state"] == "failed"
    assert not broker._store().needs_review()


async def test_source_pending_cleanup_refusal_is_durable_uncertainty(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    project = register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"]
    cache = tmp_path / "source-cache"
    project = project.model_copy(
        update={
            "source_repository": SourceRepository(
                github_repository="synthetic/firmware",
                cache_root=cache,
                private_acl_confirmed=True,
            )
        }
    )
    broker.settings = broker.settings.model_copy(update={"build_projects": {"fixture": project}})

    def failed_transport(*args):
        raise SourceError("source_download_failed")

    broker.firmware_source.snapshots.transport = failed_transport
    from cardputer_mcp import source_snapshot as source_module

    def denied_cleanup(path):
        raise PermissionError("synthetic private cleanup detail " + str(path))

    async with connect(service.url, key.token) as remote:
        prepared = await call(
            remote,
            "prepare_test",
            {
                "job_id": "cleanup-refused",
                "test": "firmware_fetch_source",
                "project_id": "fixture",
                "source_commit": "a" * 40,
            },
        )
        assert prepared["ok"]
        with monkeypatch.context() as refusal:
            refusal.setattr(source_module.shutil, "rmtree", denied_cleanup)
            assert (
                await call(
                    remote,
                    "execute_test",
                    {
                        "plan_hash": prepared["plan_hash"],
                    },
                )
            )["ok"]
            await broker.profile_jobs.queue.join()
        job = broker.job("cleanup-refused")
        assert job["state"] == "needs_review"
        assert job["result"] == "source_cleanup_unconfirmed"
        assert job["diagnostic"] == {"code": "source_cleanup_unconfirmed"}
        assert broker.firmware_source.snapshots.uncertain
        assert broker._store().needs_review()
        assert list(cache.glob("pending-*"))
        assert not broker.firmware_source.results
        assert "private cleanup detail" not in json.dumps(job)
        assert str(cache) not in json.dumps(job)
        denied = await call(
            remote,
            "prepare_test",
            {
                "job_id": "cannot-follow-cleanup",
                "test": "firmware_fetch_source",
                "project_id": "fixture",
                "source_commit": "b" * 40,
            },
        )
        assert denied["error"] == "recovery_review_required"
        assert list(cache.glob("pending-*"))


@pytest.mark.parametrize("stop", ["cancel", "revoke", "config"])
async def test_source_stop_confirms_pending_cleanup_without_usb_lease(
    registry_service, tmp_path, stop
):
    service = registry_service
    project = register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"].metadata
    cache = tmp_path / "source-cache"
    project = project.model_copy(
        update={
            "source_repository": SourceRepository(
                github_repository="synthetic/firmware",
                cache_root=cache,
                private_acl_confirmed=True,
            )
        }
    )
    broker.settings = broker.settings.model_copy(update={"build_projects": {"fixture": project}})
    started = threading.Event()

    def transport(url, deadline, live, limit):
        started.set()
        while live():
            time.sleep(0.01)
        raise SourceError("source_fetch_interrupted")

    broker.firmware_source.snapshots.transport = transport
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(
            job_id="source-stop", test=Action.SOURCE, project_id="fixture", source_commit="a" * 40
        ),
        key.principal_id,
        key.origin.value,
    )
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]),
        key.principal_id,
        key.origin.value,
    )
    assert await asyncio.to_thread(started.wait, 2)
    if stop == "cancel":
        broker.profile_jobs.cancel(
            CancelRequest(job_id="source-stop"),
            key.principal_id,
            key.origin.value,
        )
    elif stop == "revoke":
        service.registry.revoke(key.key_id)
    else:
        broker.settings = broker.settings.model_copy(update={"build_projects": {}})
    await asyncio.wait_for(broker.profile_jobs.queue.join(), 2)
    assert broker.job("source-stop")["state"] == "cancelled"
    assert not broker._store().needs_review()
    assert not broker.firmware_source.snapshots.uncertain
    assert not list(cache.glob("pending-*"))
    assert broker.session is None and service.backend.calls == 0


@pytest.mark.parametrize("release", ["raise", "still_locked"])
async def test_unconfirmed_output_release_retains_lock_reference_and_review(
    registry_service, tmp_path, monkeypatch, release
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"].metadata

    class UnconfirmedLock:
        is_locked = True
        calls = 0

        def release(self):
            self.calls += 1
            if release == "raise":
                raise OSError("synthetic private lock detail")

    held = UnconfirmedLock()

    async def build(plan, stage, *, defer_publish):
        broker.firmware_build._build_lock = held
        raise ValueError("synthetic compiler failure")

    monkeypatch.setattr(broker.firmware_build, "execute", build)
    prepared = prepare(service, "release-uncertain")
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]),
        key.principal_id,
        key.origin.value,
    )
    await broker.profile_jobs.queue.join()
    assert broker.job("release-uncertain")["state"] == "needs_review"
    assert broker.job("release-uncertain")["result"] == "build_output_release_unconfirmed"
    assert broker.firmware_build.output_uncertain
    assert broker.firmware_build._build_lock is held and held.calls == 1
    assert broker._store().needs_review()
    with pytest.raises(BrokerError, match="recovery_review_required"):
        prepare(service, "after-uncertain-release")
    broker.firmware_build._build_lock = None
    broker.firmware_build.output_uncertain = False


@pytest.mark.parametrize(
    "code",
    ["build_output_insufficient_space", "build_output_in_use", "build_output_identity_mismatch"],
)
async def test_build_failure_result_survives_execution_timeline_and_gui(
    registry_service, tmp_path, monkeypatch, code
):
    from cardputer_mcp.build_locations import BuildLocationError
    from cardputer_mcp.gui_timeline import TimelineSnapshot
    from cardputer_mcp.operator_timeline import OperatorTimeline
    from cardputer_mcp.plan_approval import PlanApprovals

    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    key = service.keys["second"].metadata

    async def fail_build(plan, stage, *, defer_publish):
        stage("idf_build")
        raise BuildLocationError(code)

    monkeypatch.setattr(broker.firmware_build, "execute", fail_build)
    prepared = prepare(service, "failure-display")
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), key.principal_id, key.origin.value
    )
    await broker.profile_jobs.queue.join()
    assert broker.job("failure-display")["state"] == "failed"
    assert broker.job("failure-display")["result"] == code
    listing = OperatorTimeline(broker, PlanApprovals(broker)).snapshot()
    row = next(x for x in listing["cards"] if x["job_id"] == "failure-display")
    assert row["result"] == code and row["stage"] == "idf_build"
    parsed = TimelineSnapshot.parse({"ok": True, **listing}, 0).cards
    shown = next(x for x in parsed if x.job_id == "failure-display")
    assert shown.result == code and shown.backend == "registered_project"
    assert not broker._store().needs_review()
