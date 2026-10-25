"""Reads are not bound to the job's principal; mutations stay owner-only (mock, localhost only).

REMOTE (`ephemeral:remote_mcp`) starts the work; LOCAL (`ephemeral:local_cli`) is another
read-scoped principal. Neither principal id nor any token may appear in a response.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from surface_harness import LOCAL, REMOTE, lease, raw, serve

from cardputer_mcp.client import call, connect

TERMINAL = {"succeeded", "failed", "cancelled", "needs_review"}


@pytest.fixture(scope="module")
def event_loop_policy():
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


def assert_no_identity(body: Any) -> None:
    text = json.dumps(body)
    for secret in ("ephemeral:", LOCAL, REMOTE):
        assert secret not in text


async def run_diagnostics(client: Any, session: str, job_id: str) -> dict[str, Any]:
    plan = await call(
        client, "submit", {"kind": "diagnostics", "session": session, "job_id": job_id}
    )
    queued = await call(
        client, "execute", {"op": "run", "plan_hash": plan["plan_hash"], "session": session}
    )
    assert queued["ok"], queued
    async with asyncio.timeout(10):
        while True:
            job = await call(client, "status", {"view": "job", "job_id": job_id})
            if job.get("state") in TERMINAL:
                return job
            await asyncio.sleep(0.01)


async def test_another_principal_reads_status_and_logs_but_cannot_cancel(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as owner, connect(surface.url, LOCAL) as other:
            session = await lease(owner)
            mine = await run_diagnostics(owner, session, "remote-job")
            theirs = await call(other, "status", {"view": "job", "job_id": "remote-job"})
            assert mine["owned_by_caller"] is True and theirs["owned_by_caller"] is False
            assert {k: v for k, v in theirs.items() if k != "owned_by_caller"} == {
                k: v for k, v in mine.items() if k not in {"owned_by_caller", "chain"}
            }
            assert_no_identity(theirs)
            # Device logs: whatever the owner gets, another reader gets byte for byte.
            request = {"view": "logs", "stream": "device", "job_id": "remote-job"}
            assert await call(other, "status", request) == await call(owner, "status", request)
            capture = {"view": "capture", "job_id": "remote-job"}
            assert await raw(other, "status", capture) == await raw(owner, "status", capture)
            # Mutations stay owner-only.
            refused = await call(other, "cancel", {"target": "job", "job_id": "remote-job"})
            assert refused["ok"] is False
            assert_no_identity(refused)


async def test_jobs_view_lists_every_principal_with_cursor_pages(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as owner, connect(surface.url, LOCAL) as other:
            session = await lease(owner)
            for name in ("job-1", "job-2", "job-3"):
                await run_diagnostics(owner, session, name)
            first = await call(other, "status", {"view": "jobs", "limit": 2})
            assert first["ok"] and first["has_more"] is True
            assert [job["job_id"] for job in first["jobs"]] == ["job-3", "job-2"]
            assert all(job["owned_by_caller"] is False for job in first["jobs"])
            assert set(first["jobs"][0]) == {
                "job_id",
                "kind",
                "state",
                "result",
                "created",
                "finished",
                "device_id",
                "owned_by_caller",
            }
            assert first["jobs"][0]["kind"] == "diagnostics"
            second = await call(
                other, "status", {"view": "jobs", "limit": 2, "cursor": first["next_cursor"]}
            )
            assert [job["job_id"] for job in second["jobs"]] == ["job-1"]
            assert second["has_more"] is False and second["next_cursor"] is None
            own = await call(owner, "status", {"view": "jobs"})
            assert all(job["owned_by_caller"] is True for job in own["jobs"])
            assert_no_identity(first)
            assert_no_identity(own)


@pytest.mark.parametrize(
    "cursor",
    [{}, {"before": 0}, {"before": "x"}, {"private_caller_key": 1}, {"before": 1, "epoch": "e"}],
)
async def test_jobs_cursor_refusal_names_only_cursor(tmp_path: Path, cursor: dict) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            error, body, text = await raw(client, "status", {"view": "jobs", "cursor": cursor})
    assert (error, body) == (False, {"ok": False, "error": "argument_invalid", "field": "cursor"})
    assert "private_caller_key" not in text


@pytest.mark.parametrize("view", ["jobs", "recovery"])
async def test_new_views_take_no_job_arguments(tmp_path: Path, view: str) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            body = await call(client, "status", {"view": view, "job_id": "x"})
    assert body == {"ok": False, "error": "argument_not_allowed", "field": "job_id"}


def seed_recovery(store: Any) -> None:
    """A device with two LKG receipts and one full backup, written like the flash path does."""

    def plan(digest: str, payload: dict[str, Any]) -> None:
        store.db.execute(
            "INSERT INTO plans(hash,payload) VALUES(?,?)", (digest, json.dumps(payload))
        )

    for index, (job_id, test) in enumerate(
        (
            ("backup-1", "firmware_backup"),
            ("flash-1", "firmware_flash"),
            ("flash-2", "firmware_flash"),
        )
    ):
        digest = str(index + 1) * 64
        plan(
            digest,
            {
                "test": test,
                "job_id": job_id,
                "device_id": "cardputer-1",
                "flash_principal": "ephemeral:remote_mcp",
                "expected_app_bin_sha256": ("1" if job_id == "flash-1" else "2") * 64,
                "expected_elf_sha256": "e" * 64,
                "backup_sha256": None,
            },
        )
        store.db.execute(
            "INSERT INTO jobs(id,plan_hash,state,result,finished) VALUES(?,?,'succeeded','done',?)",
            (job_id, digest, f"2026-10-25 10:00:0{index}"),
        )
    for receipt, job_id in (("c" * 64, "flash-1"), ("d" * 64, "flash-2")):
        store.db.execute(
            "INSERT INTO firmware_lkg_history VALUES (?,?,?,?,?,?)",
            (receipt, "cardputer-1", "ephemeral:remote_mcp", "artifact_bundle", "backup-1", job_id),
        )
    store.db.execute(
        "INSERT INTO firmware_lkg VALUES (?,?,?,?,?,?)",
        ("cardputer-1", "ephemeral:remote_mcp", "d" * 64, "artifact_bundle", "backup-1", "flash-2"),
    )
    store.db.commit()


async def test_recovery_view_lists_lkg_history_and_backups(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        seed_recovery(surface.resident._store())
        async with connect(surface.url, LOCAL) as other, connect(surface.url, REMOTE) as owner:
            body = await call(other, "status", {"view": "recovery"})
            own = await call(owner, "status", {"view": "recovery"})
    assert body["ok"] is True
    [device] = body["devices"]
    assert device["device_id"] == "cardputer-1" and device["registered"] is False
    assert device["current_lkg"] == "d" * 64
    assert [
        (item["receipt"], item["current"], item["job_id"]) for item in device["lkg_history"]
    ] == [
        ("d" * 64, True, "flash-2"),
        ("c" * 64, False, "flash-1"),
    ]
    newest = device["lkg_history"][0]
    assert newest["firmware_sha256"] == "2" * 64 and newest["elf_sha256"] == "e" * 64
    assert newest["baseline_job_id"] == "backup-1" and newest["recorded"]
    assert newest["recorded_by_caller"] is False
    assert own["devices"][0]["lkg_history"][0]["recorded_by_caller"] is True
    [backup] = device["full_backups"]
    assert backup["job_id"] == "backup-1" and backup["owned_by_caller"] is False
    # No retained receipt file in this mock: the digest is unknown, never guessed.
    assert backup["receipt_available"] is False and backup["sha256"] is None
    assert_no_identity(body)
    assert_no_identity(own)


async def test_recovery_view_is_empty_without_devices(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            body = await call(client, "status", {"view": "recovery"})
    assert body == {"ok": True, "devices": []}


# Durable fallbacks: after RAM loss the owner is still resolved, so another reader is
# served the owner's exact durable body (broker-level; the MCP ops use these helpers).


async def test_saved_capture_owner_survives_restart(tmp_path: Path) -> None:
    from test_capture_broker import PRINCIPAL, acquire, enqueue, running

    from cardputer_mcp import job_reads

    async with running(tmp_path) as broker:
        enqueue(broker, acquire(broker), "kept")
        await broker.queue.join()
        assert job_reads.capture_owner(broker, "kept") == PRINCIPAL
    async with running(tmp_path) as restarted:
        assert not restarted.captures
        assert job_reads.capture_owner(restarted, "kept") == PRINCIPAL
        owner = restarted.read_capture("kept", PRINCIPAL)
        served = restarted.read_capture("kept", job_reads.capture_owner(restarted, "kept"))
        assert served.png == owner.png and served.metadata() == owner.metadata()
        assert job_reads.capture_owner(restarted, "missing") is None


def test_durable_app_events_owner_survives_restart(tmp_path: Path) -> None:
    from test_durable_session_flash_records import _broker, _run_app

    from cardputer_mcp import job_reads
    from cardputer_mcp.app_models import AppEventsRequest

    async def scenario() -> None:
        broker = _broker(tmp_path)
        await broker.start()
        try:
            await _run_app(broker, "app-one")
        finally:
            await broker.close()
        restarted = _broker(tmp_path)
        await restarted.start()
        try:
            assert not restarted.app_sessions.jobs
            owner = job_reads.app_event_owner(restarted, "app-one")
            assert owner == "owner"
            request = AppEventsRequest(job_id="app-one")
            report = restarted.read_app_events(request, owner)
            assert report["source"] == "durable_job_log"
            assert report == restarted.read_app_events(request, "owner")
        finally:
            await restarted.close()

    asyncio.run(scenario())
