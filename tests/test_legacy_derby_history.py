"""Legacy DERBY rows in a synthetic durable state stay readable; nothing new can start.

The DERBY smoke/fixed job kinds and the diagnostic build options were removed. The real
state keeps their rows as history, so every read path must still show them without a crash
and without private details, and the removed kinds/options must be refused cleanly.
Everything here is a synthetic temporary SQLite state; no installed state is read.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

import httpx2
import pytest
from test_authenticated_mock import LOCAL, REMOTE
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_authenticated_mock import event_loop_policy as event_loop_policy

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.client import call, connect
from cardputer_mcp.gui_timeline import TimelineSnapshot
from cardputer_mcp.history_archive import archive_history
from cardputer_mcp.store import Store

PRINCIPAL = "ephemeral:remote_mcp"
SECRET = "SYNTHETIC_PRIVATE_DETAIL"
SERIAL = {
    "version": 1,
    "session_id": "c" * 32,
    "device": "configured-cardputer",
    "backend": "serial",
    "port": "COM42",
    "device_fingerprint": "d" * 64,
    "expected_firmware_sha256": "f" * 64,
    "firmware_verification": "configured_expected_hash_only",
    "observe_seconds": None,
    "effects": ["Synthetic retained history; no device operation"],
}
RAW = b"DERBY_FIXED v=1 synthetic\n"


def legacy_plans(epoch):
    smoke = {
        **SERIAL,
        "broker_epoch": epoch,
        "job_id": "legacy-smoke",
        "test": "derby_smoke",
        "stages": ["serial_open", "derby_smoke", "serial_close"],
        "protocol": "pocketjs-derby-smoke-v1",
        "smoke": {"synthetic": True},
    }
    fixed = {
        **SERIAL,
        "broker_epoch": epoch,
        "job_id": "legacy-fixed",
        "test": "derby_fixed_workload",
        "stages": ["serial_open", "derby_fixed_workload", "serial_close"],
        "protocol": "pocketjs-derby-fixed-v2",
        "fixed_workload": {"synthetic": True},
    }
    build = {
        "version": 2,
        "execution_context": "registered_project",
        "broker_epoch": epoch,
        "job_id": "legacy-build",
        "test": "firmware_build",
        "principal_id": PRINCIPAL,
        "origin": "remote_mcp",
        "project_id": "fixture",
        "source_receipt": "c" * 64,
        "derby_detail_mode": "ON",
        "derby_dma_mode": "OFF",
        "diagnostic_entry_sha256": "e" * 64,
        "registration_sha256": "a" * 64,
    }
    return smoke, fixed, build


def seed(store, epoch):
    """Rows as the removed features wrote them, including private extras."""
    smoke, fixed, build = legacy_plans(epoch)
    rows = {}
    for plan in (smoke, fixed, build):
        digest = digest_plan(plan)
        store.put_plan(digest, plan)
        store.add_job(plan["job_id"], digest)
        store.db.execute(
            "INSERT INTO execution_requests(job_id,plan_hash,principal_id) VALUES(?,?,?)",
            (plan["job_id"], digest, PRINCIPAL),
        )
        rows[plan["job_id"]] = digest
    store.set_job(
        "legacy-smoke",
        "succeeded",
        "derby_demo_cycle_observed_render_buffer_lcd_unverified",
        {
            "derby_diagnostics_provenance": {"private": SECRET},
            "derby_observation": {"exit_home_observed": True, "diagnostics": SECRET},
        },
    )
    store.set_job(
        "legacy-fixed",
        "failed",
        "serial_read_failed",
        {"device_fault_category": "fixed_workload_stall", "workload_end_observed": False},
    )
    store.set_job("legacy-build", "succeeded", "firmware_build_succeeded_no_flash")
    store.db.execute("UPDATE jobs SET stage='serial_close' WHERE id='legacy-smoke'")
    store.db.execute("UPDATE jobs SET stage='derby_fixed_workload' WHERE id='legacy-fixed'")
    store.db.execute(
        "INSERT INTO derby_fixed_trials(job_id,plan_hash,principal_id,raw,sha256,receipt,valid) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "legacy-fixed",
            rows["legacy-fixed"],
            PRINCIPAL,
            RAW,
            hashlib.sha256(RAW).hexdigest(),
            "{}",
            0,
        ),
    )
    store.db.commit()
    return rows


async def test_status_timeline_and_refusals_with_legacy_rows(authenticated_mock):
    url, broker = authenticated_mock
    rows = seed(broker._store(), broker.epoch)
    async with connect(url, REMOTE) as remote:
        for job_id, state in (
            ("legacy-smoke", "succeeded"),
            ("legacy-fixed", "failed"),
            ("legacy-build", "succeeded"),
        ):
            shown = await call(remote, "status", {"view": "job", "job_id": job_id})
            assert shown["ok"] and shown["state"] == state, shown
            assert SECRET not in json.dumps(shown)
            assert "derby_diagnostics" not in shown and "fixed_trial" not in shown
        listed = await call(remote, "status", {"view": "jobs", "limit": 100})
        assert listed["ok"], listed
        kinds = {job["job_id"]: job["kind"] for job in listed["jobs"]}
        # The removed kinds are listed as generic history; the build keeps its kind.
        assert kinds["legacy-smoke"] == kinds["legacy-fixed"] == "other"
        assert kinds["legacy-build"] == "build"
        # The same job-log read path serves the removed kinds' (empty) logs.
        logs = await call(
            remote, "status", {"view": "logs", "stream": "device", "job_id": "legacy-smoke"}
        )
        assert "error" not in logs or logs["error"] != "internal_error", logs
        # Execution of a stored legacy plan is refused; the stored rows are untouched.
        before = broker._store().job("legacy-smoke")
        lease = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 300}
        )
        refused = await call(
            remote,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": rows["legacy-smoke"]},
        )
        assert refused["ok"] is False and refused["error"] != "invalid_session", refused
        assert broker._store().job("legacy-smoke") == before
        for kind in ("derby", "derby_fixed"):
            with pytest.raises(ValueError, match="rejected"):
                await call(remote, "submit", {"kind": kind, "job_id": "new", "session": "a" * 64})
    async with httpx2.AsyncClient(
        base_url=url.removesuffix("/mcp"),
        trust_env=False,
        headers={"Authorization": "Bearer " + LOCAL},
    ) as http:
        response = await http.get("/operator/timeline")
        assert response.status_code == 200, response.text
        snapshot = TimelineSnapshot.parse(response.json(), time.monotonic())
        cards = {card.job_id: card for card in snapshot.cards}
        assert {"legacy-smoke", "legacy-fixed", "legacy-build"} <= set(cards)
        assert cards["legacy-smoke"].test == "derby_smoke"
        assert cards["legacy-fixed"].test == "derby_fixed_workload"
        assert SECRET not in response.text


def test_history_archive_moves_legacy_rows_including_the_inert_trial_table(tmp_path: Path):
    state = tmp_path / "state"
    state.mkdir()
    store = Store(state / "history.sqlite3")
    seed(store, "a" * 32)
    store.close()
    result = archive_history(state, keep_latest=0)
    assert result["ok"], result
    live = sqlite3.connect(state / "history.sqlite3")
    archived = sqlite3.connect(state / result["archive_directory"] / "history.sqlite3")
    try:
        assert live.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert {row[0] for row in archived.execute("SELECT id FROM jobs")} >= {
            "legacy-smoke",
            "legacy-fixed",
            "legacy-build",
        }
        assert archived.execute("SELECT raw FROM derby_fixed_trials").fetchone()[0] == RAW
    finally:
        live.close()
        archived.close()
