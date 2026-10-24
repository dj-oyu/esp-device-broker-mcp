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


def source_submit(job="source-preflight"):
    return {
        "kind": "fetch",
        "job_id": job,
        "project_id": "fixture",
        "source_commit": "a" * 40,
    }


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


@pytest.mark.parametrize(
    ("change", "method"),
    [("revoke", "submit"), ("scope", "status"), ("read_scope", "submit"), ("principal", "status")],
)
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
                remaining = "read" if method == "submit" else "control"
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
                            "request": source_submit()
                            if method == "submit"
                            else {"view": "logs", "stream": "build", "job_id": "source-logs"}
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
            call(client, "status", {"view": "source", "project_id": "fixture"})
        )
        await entered_worker(entered)
        assert (await asyncio.wait_for(call(status_client, "status", {"view": "device"}), 0.5))[
            "ok"
        ]
        release.set()
        first = await task
        assert first["ok"] and first["source_cache"]["snapshots"] == 0
        assert first["cleanup_authorized"] is False
        entered.clear()
        release.clear()
        task = asyncio.create_task(
            call(client, "status", {"view": "source", "project_id": "fixture"})
        )
        await entered_worker(entered)
        changed[0] = True
        release.set()
        second = await task
        assert not second["ok"] and "source_cache" not in second
    assert broker._store().record_count("plans") == 0
    assert broker.backend.calls == 0


@pytest.mark.parametrize("change", ["principal", "settings", "epoch"])
async def test_prepare_scan_failure_rechecks_authority_before_returning_counters(
    resident, monkeypatch, change
):
    from cardputer_mcp.source_snapshot import CacheScanLimit

    broker, _ = resident
    entered, release = threading.Event(), threading.Event()
    owner = [("owner", "remote_mcp")]
    original_settings, original_epoch = broker.settings, broker.epoch

    def limited(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        raise CacheScanLimit("deadline", 12345, 5001)

    monkeypatch.setattr(SourceSnapshots, "usage", limited)
    task = asyncio.create_task(
        broker.profile_jobs.prepare_source(
            source_request(), "owner", "remote_mcp", reauthorize=lambda: owner[0]
        )
    )
    try:
        await entered_worker(entered)
        if change == "principal":
            owner[0] = ("other", "remote_mcp")
        elif change == "settings":
            broker.settings = broker.settings.model_copy(deep=True)
        else:
            broker.epoch = "changed"
        release.set()
        with pytest.raises(BrokerError) as caught:
            await task
        assert not hasattr(caught.value, "scan")
        assert str(caught.value) in (
            "principal_not_authorized",
            "profile_or_principal_no_longer_valid",
        )
    finally:
        release.set()
        broker.settings, broker.epoch = original_settings, original_epoch
    assert broker._store().record_count("plans") == 0
