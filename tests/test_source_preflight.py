"""Gated synthetic filesystem work; never real network, SDK, build, or USB."""

import asyncio
import threading
from dataclasses import replace

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult
from test_authenticated_mock import LOCAL, REMOTE
from test_authenticated_mock import authenticated_mock as authenticated_mock

from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject, Settings, SourceRepository
from cardputer_mcp.models import PrepareRequest
from cardputer_mcp.server import RequestAuth
from cardputer_mcp.source_preflight import SourceReadWorker
from cardputer_mcp.source_snapshot import SourceError, SourceSnapshots
from cardputer_mcp.store import Store


def registered(settings, tmp_path):
    project = BuildProject(
        project_dir=tmp_path / "synthetic-worktree",
        trusted_profile=tmp_path / "synthetic-profile.ps1",
        source_repository=SourceRepository(
            github_repository="synthetic/example",
            cache_root=tmp_path / "synthetic-cache",
            private_acl_confirmed=True,
        ),
    )
    return settings.model_copy(update={"build_projects": {"fixture": project}})


def source_request(job="source-preflight"):
    return PrepareRequest(
        job_id=job,
        test="firmware_fetch_source",
        project_id="fixture",
        source_commit="a" * 40,
    )


@pytest.fixture
async def resident(tmp_path):
    settings = registered(Settings(state_dir=tmp_path / "state"), tmp_path)
    owner_thread = threading.get_ident()
    valid = [True]

    def authorized(*args):
        assert threading.get_ident() == owner_thread
        return valid[0]

    broker = Broker(settings, principal_valid=authorized, lock_path=tmp_path / "device.lock")
    await broker.start()
    try:
        yield broker, valid
    finally:
        await broker.close()


@pytest.fixture
def gated_usage(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    settings_seen = []
    usage = SourceSnapshots.usage

    def slow(self, project_id, *, stop=None):
        settings_seen.append(self.settings)
        with self.cache_guard(project_id):
            entered.set()
            assert release.wait(timeout=3)
            return usage(self, project_id, stop=stop)

    monkeypatch.setattr(SourceSnapshots, "usage", slow)
    yield entered, release, settings_seen
    release.set()


async def entered_worker(entered):
    assert await asyncio.to_thread(entered.wait, 1)


async def test_source_prepare_offloads_io_and_keeps_store_authority_and_status_on_loop(
    resident, gated_usage, monkeypatch
):
    broker, _ = resident
    entered, release, seen = gated_usage
    owner_thread = threading.get_ident()
    store = broker._store()
    shared_settings = broker.firmware_source.snapshots.settings
    for name in ("job", "plan", "record_count", "put_plan"):
        original = getattr(store, name)

        def on_loop(*args, action=original, **kwargs):
            assert threading.get_ident() == owner_thread
            return action(*args, **kwargs)

        monkeypatch.setattr(store, name, on_loop)
    task = asyncio.create_task(
        broker.profile_jobs.prepare_source(source_request(), "owner", "remote_mcp")
    )
    await entered_worker(entered)
    try:
        assert not task.done()
        assert seen[0] is not broker.settings
        assert broker.firmware_source.snapshots.settings is shared_settings
        assert broker.status()["backend"] == "mock"

        async def check_authority():
            return broker.profile_jobs._authorized("owner", "remote_mcp")

        def serial_lane_check():
            return asyncio.run_coroutine_threadsafe(check_authority(), loop).result(timeout=0.5)

        loop = asyncio.get_running_loop()
        assert await asyncio.to_thread(serial_lane_check)
        with pytest.raises(SourceError, match="source_fetch_busy"):
            await broker.profile_jobs.prepare_source(
                source_request("second"), "owner", "remote_mcp"
            )
        assert store.record_count("plans") == 0
    finally:
        release.set()
    result = await task
    assert store.plan(result["plan_hash"]) == result["plan"]
    assert store.record_count("jobs") == 0 and broker.backend.calls == 0


@pytest.mark.parametrize("change", ["revoke", "nested_settings", "settings", "epoch", "uncertain"])
async def test_source_prepare_rejects_changed_authority_registration_or_state_after_io(
    resident, gated_usage, change
):
    broker, valid = resident
    entered, release, seen = gated_usage
    original_project = broker.settings.build_projects["fixture"]
    task = asyncio.create_task(
        broker.profile_jobs.prepare_source(source_request(), "owner", "remote_mcp")
    )
    await entered_worker(entered)
    if change == "revoke":
        valid[0] = False
    elif change == "nested_settings":
        broker.settings.build_projects.clear()
    elif change == "settings":
        broker.settings = broker.settings.model_copy(deep=True)
    elif change == "epoch":
        broker.epoch = "b" * 32
    else:
        broker.profile_jobs.state_uncertain = True
    release.set()
    with pytest.raises(BrokerError):
        await task
    assert seen[0].build_projects["fixture"] == original_project
    assert broker._store().record_count("plans") == 0


async def test_cancelled_source_prepare_retains_bounded_worker_and_shutdown_ownership(
    resident, gated_usage
):
    broker, _ = resident
    entered, release, _ = gated_usage
    task = asyncio.create_task(
        broker.profile_jobs.prepare_source(source_request(), "owner", "remote_mcp")
    )
    await entered_worker(entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    reads = broker.profile_jobs.source_reads
    assert reads.worker is not None and reads.stop.is_set()
    with pytest.raises(SourceError, match="source_fetch_busy"):
        await broker.profile_jobs.prepare_source(source_request("second"), "owner", "remote_mcp")
    closing = asyncio.create_task(broker.close())
    await asyncio.sleep(0)
    assert not closing.done() and broker._state_lock.is_locked and broker.store is not None
    release.set()
    await closing
    assert reads.worker is None
    assert broker.store is None and not broker._state_lock.is_locked


async def test_source_prepare_rechecks_job_conflict_created_by_final_authority_callback(
    resident, gated_usage
):
    broker, _ = resident
    entered, release, _ = gated_usage
    count = 0

    def authorized(*args):
        nonlocal count
        count += 1
        if count == 3:
            store = broker._store()
            store.put_plan("b" * 64, {"synthetic": True})
            store.add_job("source-preflight", "b" * 64)
        return True

    broker.principal_valid = authorized
    task = asyncio.create_task(
        broker.profile_jobs.prepare_source(source_request(), "owner", "remote_mcp")
    )
    await entered_worker(entered)
    release.set()
    with pytest.raises(BrokerError, match="job_id_conflict"):
        await task
    assert broker._store().record_count("plans") == 1


@pytest.mark.parametrize("retention", ["expiry", "eviction"])
async def test_real_http_source_prepare_and_source_log_inventory_do_not_block_status(
    authenticated_mock, tmp_path, gated_usage, retention
):
    url, broker = authenticated_mock
    broker.settings = registered(broker.settings, tmp_path)
    entered, release, _ = gated_usage
    async with connect(url, REMOTE) as client, connect(url, LOCAL) as status_client:
        task = asyncio.create_task(
            call(client, "prepare_test", source_request().model_dump(mode="json"))
        )
        await entered_worker(entered)
        try:
            assert (await asyncio.wait_for(call(status_client, "device_status"), 0.5))["ok"]
            assert not task.done()
        finally:
            release.set()
        assert (await task)["ok"]
        entered.clear()
        release.clear()
        plan = {
            "test": "firmware_fetch_source",
            "source_principal": "ephemeral:remote_mcp",
            "project_id": "fixture",
        }
        digest = digest_plan(plan)
        broker._store().put_plan(digest, plan)
        broker._store().add_job("source-logs", digest)
        logs = BuildLogs("ephemeral:remote_mcp")
        logs.append("synthetic retained output")
        broker.firmware_source.logs["source-logs"] = logs
        reading = asyncio.create_task(call(client, "read_build_logs", {"job_id": "source-logs"}))
        await entered_worker(entered)
        try:
            assert (await asyncio.wait_for(call(status_client, "device_status"), 0.5))["ok"]
            if retention == "expiry":
                logs.deadline = 0
            else:
                broker.firmware_source.logs.clear()
        finally:
            release.set()
        response = await reading
        assert response["ok"] and response["expired"] and response["entries"] == []
        assert "synthetic retained output" not in str(response)
    assert broker.backend.calls == 0


@pytest.mark.parametrize("change", ["revoke", "scope", "read_scope", "principal"])
@pytest.mark.parametrize("method", ["prepare_test", "read_build_logs"])
async def test_source_reads_recheck_current_grant_before_publishing_plan_or_output(
    authenticated_mock, tmp_path, gated_usage, monkeypatch, change, method
):
    url, broker = authenticated_mock
    broker.settings = registered(broker.settings, tmp_path)
    plan = {
        "test": "firmware_fetch_source",
        "source_principal": "ephemeral:remote_mcp",
        "project_id": "fixture",
    }
    digest = digest_plan(plan)
    broker._store().put_plan(digest, plan)
    broker._store().add_job("source-logs", digest)
    logs = BuildLogs("ephemeral:remote_mcp")
    logs.append("synthetic private output")
    broker.firmware_source.logs["source-logs"] = logs
    entered, release, _ = gated_usage
    changed = [False]
    authenticate = RequestAuth.authenticate

    def current(self, header):
        owner = authenticate(self, header)
        if changed[0] and owner is not None:
            if change == "revoke":
                return None
            if change == "scope":
                remaining = "read" if method == "prepare_test" else "control"
                return replace(owner, scopes=frozenset({remaining}))
            if change == "read_scope":
                return replace(owner, scopes=frozenset({"control"}))
            return replace(owner, principal_id="synthetic-other-owner")
        return owner

    monkeypatch.setattr(RequestAuth, "authenticate", current)
    async with connect(url, REMOTE) as client:
        # The high-level helper follows a result with tools/list. Use one typed
        # request so revocation tests inspect this already-dispatched response.
        task = asyncio.create_task(
            client.session.send_request(
                CallToolRequest(
                    params=CallToolRequestParams(
                        name=method,
                        arguments={
                            "request": source_request().model_dump(mode="json")
                            if method == "prepare_test"
                            else {"job_id": "source-logs"}
                        },
                    )
                ),
                CallToolResult,
            )
        )
        await entered_worker(entered)
        changed[0] = True
        release.set()
        result = (await task).structured_content
    assert result["ok"] is False
    assert result["error"] in (
        "authentication_required",
        "insufficient_scope",
        "source_log_not_available",
        "principal_not_authorized",
    )
    assert "entries" not in result and "synthetic private output" not in str(result)
    assert broker._store().record_count("plans") == 1


async def test_source_worker_failure_is_retrieved_after_cancel_without_late_publication():
    worker = SourceReadWorker()
    entered, release = threading.Event(), threading.Event()

    def failing(stop):
        entered.set()
        assert release.wait(timeout=2)
        raise SourceError("source_fetch_interrupted")

    task = asyncio.create_task(worker.run(failing))
    await entered_worker(entered)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker.worker is not None
    release.set()
    await worker.close()
    await asyncio.sleep(0)
    assert worker.worker is None


@pytest.mark.parametrize("known", [False, True])
async def test_source_prepare_worker_errors_are_fixed_and_do_not_publish(
    resident, monkeypatch, known
):
    broker, _ = resident

    def failed_usage(*args, **kwargs):
        if known:
            raise SourceError("source_cache_unknown_entry")
        raise RuntimeError("SYNTHETIC_PRIVATE_ERROR token=SYNTHETIC_SECRET")

    monkeypatch.setattr(SourceSnapshots, "usage", failed_usage)
    with pytest.raises(SourceError) as caught:
        await broker.profile_jobs.prepare_source(source_request(), "owner", "remote_mcp")
    assert caught.value.code == ("source_cache_unknown_entry" if known else "source_fetch_failed")
    assert "SYNTHETIC_PRIVATE" not in str(caught.value)
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert broker._store().record_count("plans") == 0


async def test_source_prepare_cannot_publish_into_a_replaced_store(resident, gated_usage, tmp_path):
    broker, _ = resident
    entered, release, _ = gated_usage
    original = broker._store()
    replacement = Store(tmp_path / "synthetic-replacement.sqlite3")
    task = asyncio.create_task(
        broker.profile_jobs.prepare_source(source_request(), "owner", "remote_mcp")
    )
    await entered_worker(entered)
    try:
        broker.store = replacement
        release.set()
        with pytest.raises(BrokerError, match="profile_or_principal_no_longer_valid"):
            await task
        assert original.record_count("plans") == replacement.record_count("plans") == 0
    finally:
        broker.store = original
        replacement.close()


async def test_source_logs_can_skip_inventory_and_survive_scan_deadline(
    authenticated_mock, tmp_path, monkeypatch
):
    from cardputer_mcp.source_snapshot import CacheScanLimit

    url, broker = authenticated_mock
    broker.settings = registered(broker.settings, tmp_path)
    plan = {
        "test": "firmware_fetch_source",
        "source_principal": "ephemeral:remote_mcp",
        "project_id": "fixture",
    }
    digest = digest_plan(plan)
    broker._store().put_plan(digest, plan)
    broker._store().add_job("resource-logs", digest)
    logs = BuildLogs("ephemeral:remote_mcp")
    logs.append("error: retained diagnostic")
    broker.firmware_source.logs["resource-logs"] = logs
    calls = []

    def limited(*args, **kwargs):
        calls.append(True)
        raise CacheScanLimit("deadline", 44000, 5001)

    monkeypatch.setattr(SourceSnapshots, "usage", limited)
    async with connect(url, REMOTE) as client:
        skipped = await call(
            client, "read_build_logs", {"job_id": "resource-logs", "include_source_cache": False}
        )
        assert skipped["ok"] and not calls
        assert skipped["source_cache"]["reason"] == "not_requested"
        response = await call(client, "read_build_logs", {"job_id": "resource-logs"})
        assert response["ok"] and len(calls) == 1
        assert response["source_cache"]["available"] is False
        assert response["source_cache"]["scan"]["reason"] == "deadline"
        assert any("retained diagnostic" in item["text"] for item in response["entries"])
    assert broker.backend.calls == 0


async def test_resource_tool_inventory_is_fresh_read_only_and_rechecks_authority(
    authenticated_mock, tmp_path, monkeypatch
):
    url, broker = authenticated_mock
    broker.settings = registered(broker.settings, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = SourceSnapshots.inventory

    def gated(self, project_id, *, stop=None):
        entered.set()
        assert release.wait(timeout=3)
        return original(self, project_id, stop=stop)

    monkeypatch.setattr(SourceSnapshots, "inventory", gated)
    changed = [False]
    authenticate = RequestAuth.authenticate

    def current(self, header):
        owner = authenticate(self, header)
        if changed[0] and owner is not None:
            return replace(owner, principal_id="other")
        return owner

    monkeypatch.setattr(RequestAuth, "authenticate", current)
    async with connect(url, REMOTE) as client, connect(url, LOCAL) as status_client:
        task = asyncio.create_task(
            call(client, "read_source_resources", {"project_id": "fixture"})
        )
        await entered_worker(entered)
        assert (await asyncio.wait_for(call(status_client, "device_status"), 0.5))["ok"]
        release.set()
        first = await task
        assert first["ok"] and first["source_cache"]["snapshots"] == 0
        assert first["cleanup_authorized"] is False
        entered.clear()
        release.clear()
        task = asyncio.create_task(
            call(client, "read_source_resources", {"project_id": "fixture"})
        )
        await entered_worker(entered)
        changed[0] = True
        release.set()
        second = await task
        assert not second["ok"] and "source_cache" not in second
    assert broker._store().record_count("plans") == 0
    assert broker.backend.calls == 0
