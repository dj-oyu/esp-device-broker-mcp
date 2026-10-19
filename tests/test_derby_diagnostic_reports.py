"""Synthetic persisted DERBY history over existing guarded HTTP; no device IO."""

from copy import deepcopy

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult
from test_authenticated_mock import LOCAL, REMOTE
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_derby_http import derby_service as derby_service
from test_derby_smoke import Wire
from test_derby_telemetry import later, measure_wire
from test_derby_telemetry import wire as telemetry_wire
from test_http_integration import _service
from test_serial_http import serial_service as serial_service

from cardputer_mcp import derby_reports, server
from cardputer_mcp.broker import Broker, digest_plan
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.derby_provenance import capture_provenance
from cardputer_mcp.derby_reports import PROVENANCE_KEY, private_job, shared_job
from cardputer_mcp.derby_telemetry import DerbyDiagnosticsCollector
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend
from cardputer_mcp.server import RequestAuth

OWNER = "ephemeral:remote_mcp"


def retained(broker, *, state="succeeded"):
    plan = {
        "test": "derby_smoke",
        "backend": "serial",
        "job_id": "synthetic-observation",
        "broker_epoch": broker.epoch,
        "device_fingerprint": "d" * 64,
        "expected_firmware_sha256": "a" * 64,
    }
    digest = digest_plan(plan)
    store = broker._store()
    store.put_plan(digest, plan)
    store.enqueue_requested(plan["job_id"], digest, OWNER)
    collector = DerbyDiagnosticsCollector(clock=lambda: 100.0, origin=100.0)
    collector.feed(b"DERBY CAM WIDE 1")
    details = collector.snapshot().model_dump(mode="json", exclude_none=True)
    diagnostic = {
        "code": "synthetic_fixed_result",
        "derby_observation": {
            "version": 1,
            "photo_pixels_verified": False,
            "diagnostics": details,
        },
        PROVENANCE_KEY: capture_provenance(broker, plan, plan["job_id"], OWNER).model_dump(
            mode="json", exclude_none=True
        ),
    }
    store.set_job(plan["job_id"], state, "synthetic_fixed_result", diagnostic)
    return store, store.job(plan["job_id"]), plan, details


@pytest.mark.parametrize("state", ["succeeded", "needs_review"])
async def test_actual_job_status_preserves_shared_summary_and_restricts_new_details(
    authenticated_mock, state
):
    url, broker = authenticated_mock
    store, job, plan, details = retained(broker, state=state)
    original = deepcopy(job)
    async with connect(url, REMOTE) as owner, connect(url, LOCAL) as other:
        owned = await call(owner, "job_status", {"job_id": job["id"]})
        shared = await call(other, "job_status", {"job_id": job["id"]})
    assert owned["ok"] and owned["state"] == shared["state"] == state
    assert owned["result"] == shared["result"] == "synthetic_fixed_result"
    assert owned["derby_diagnostics"]["available"]
    assert owned["derby_diagnostics"]["observation"] == details
    assert "derby_diagnostics" not in shared
    for response in (owned, shared):
        assert "diagnostics" not in response["diagnostic"]["derby_observation"]
        assert PROVENANCE_KEY not in response["diagnostic"]
    assert store.job(job["id"]) == original
    assert store.plan(job["plan_hash"]) == plan
    assert broker.backend.calls == 0 and broker.queue.empty()


@pytest.mark.parametrize("damage", ["payload", "provenance", "binding", "overflow"])
async def test_invalid_new_details_never_leak_or_change_the_shared_job(
    authenticated_mock, monkeypatch, damage
):
    _, broker = authenticated_mock
    store, job, plan, _ = retained(broker)
    if damage == "payload":
        job["diagnostic"]["derby_observation"]["diagnostics"] = {
            "SYNTHETIC_PRIVATE": "token=SYNTHETIC_SECRET"
        }
    elif damage == "provenance":
        job["diagnostic"][PROVENANCE_KEY] = {"SYNTHETIC_PRIVATE": "SYNTHETIC_SECRET"}
    elif damage == "binding":
        job["diagnostic"][PROVENANCE_KEY]["job_id"] = "another-synthetic-job"
    else:
        monkeypatch.setattr(derby_reports, "MAX_REPORT_BYTES", 1)
    result = private_job(store, job, plan, OWNER)
    assert result["state"] == "succeeded" and result["result"] == job["result"]
    assert result["derby_diagnostics"] == {
        "available": False,
        "reason": "diagnostic_details_unavailable",
    }
    assert "SYNTHETIC_PRIVATE" not in str(result) and "SYNTHETIC_SECRET" not in str(result)
    assert "diagnostics" not in result["diagnostic"]["derby_observation"]
    assert "derby_diagnostics" not in private_job(store, job, plan, "another-owner")


async def test_legacy_row_and_missing_plan_never_acquire_or_leak_new_details(authenticated_mock):
    _, broker = authenticated_mock
    store, job, plan, _ = retained(broker)
    expected = shared_job(job)
    assert private_job(store, job, None, OWNER) == expected
    legacy = deepcopy(expected)
    assert private_job(store, legacy, plan, OWNER) == legacy
    assert job["diagnostic"]["derby_observation"]["diagnostics"]


@pytest.mark.parametrize("authenticated", [False, True])
async def test_persisted_history_keeps_legacy_anonymous_summary_and_owned_restart_read(
    tmp_path, authenticated
):
    broker = Broker(
        Settings(state_dir=tmp_path / "state"),
        lock_path=tmp_path / "synthetic-device.lock",
    )
    await broker.start()
    try:
        _, job, _, _ = retained(broker)
    finally:
        await broker.close()
    async with _service(tmp_path, authenticated) as url:
        async with connect(url, REMOTE if authenticated else None) as client:
            result = await call(client, "job_status", {"job_id": job["id"]})
    assert result["ok"] and result["state"] == "succeeded"
    assert "diagnostics" not in result["diagnostic"]["derby_observation"]
    assert PROVENANCE_KEY not in result["diagnostic"]
    assert ("derby_diagnostics" in result) is authenticated


@pytest.mark.parametrize("change", ["revoke", "owner", "job", "epoch", "principal"])
async def test_actual_job_status_rechecks_authority_and_owned_row_before_private_response(
    authenticated_mock, monkeypatch, change
):
    url, broker = authenticated_mock
    store, job, _, _ = retained(broker)
    changed = [False]
    authenticate = RequestAuth.authenticate
    project = server.private_job

    def current(auth, header):
        owner = authenticate(auth, header)
        if changed[0] and change == "revoke":
            return None
        if changed[0] and change == "principal" and owner is not None:
            from dataclasses import replace

            return replace(owner, principal_id="synthetic-other-owner")
        return owner

    def after_projection(*args):
        result = project(*args)
        if "derby_diagnostics" in result:
            changed[0] = True
            if change == "owner":
                store.db.execute(
                    "UPDATE execution_requests SET principal_id=? WHERE job_id=?",
                    ("synthetic-other-owner", job["id"]),
                )
                store.db.commit()
            elif change == "job":
                store.set_job(job["id"], "needs_review", "synthetic_new_result")
            elif change == "epoch":
                broker.epoch = "e" * 32
        return result

    monkeypatch.setattr(RequestAuth, "authenticate", current)
    monkeypatch.setattr(server, "private_job", after_projection)
    async with connect(url, REMOTE) as client:
        response = await client.session.send_request(
            CallToolRequest(
                params=CallToolRequestParams(
                    name="job_status", arguments={"request": {"job_id": job["id"]}}
                )
            ),
            CallToolResult,
        )
    result = response.structured_content
    assert "derby_diagnostics" not in result
    if change == "revoke":
        assert result == {"ok": False, "error": "authentication_required"}
    else:
        assert result["ok"] and result["state"] == "succeeded"
        assert "diagnostics" not in result["diagnostic"]["derby_observation"]
    assert broker.backend.calls == 0


@pytest.mark.parametrize("fault", [False, True])
async def test_single_reader_to_owned_http_report_on_success_and_failure(derby_service, fault):
    service = derby_service
    device = Wire()
    original = device.write

    def write(data):
        count = original(data)
        if data == b"e":
            records = (
                b"I (123) js: DERBY CAM WIDE 2\n"
                + telemetry_wire()
                + b"\n"
                + measure_wire()
                + b"\n"
                + later()
                + b"\n"
            )
            if fault:
                records += (
                    b"I (124) js: DERBY FRAMEFAIL race SYNTHETIC_PRIVATE_EXCEPTION token=SECRET\n"
                )
            device.queue.append(records)
        return count

    device.write = write
    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    backend = SerialBackend(
        config,
        inventory=lambda: [PortInfo(port="COM42", vid=1, pid=2, serial_number="synthetic")],
        factory=lambda: device,
        clock=lambda: device.now,
    )
    resident = service.resident
    resident.backend = backend
    resident.settings = resident.settings.model_copy(
        update={"backend": "serial", "serial": config, "allow_mock_execution": False}
    )
    async with connect(service.url, REMOTE) as owner, connect(service.url, LOCAL) as other:
        lease = await call(owner, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
        prepared = await call(
            owner,
            "prepare_test",
            {"session": lease["session"], "job_id": "observed-cycle", "test": "derby_smoke"},
        )
        assert prepared["ok"] and not device.writes
        executed = await call(
            owner,
            "execute_test",
            {"session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        assert executed["ok"]
        await resident.queue.join()
        result = await call(owner, "job_status", {"job_id": "observed-cycle"})
        shared = await call(other, "job_status", {"job_id": "observed-cycle"})
        replay = await call(
            owner,
            "execute_test",
            {"session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
    assert result["state"] == ("needs_review" if fault else "succeeded")
    assert result["result"] == (
        "serial_fault_marker" if fault else "derby_demo_cycle_observed_render_buffer_lcd_unverified"
    )
    details = result["derby_diagnostics"]
    assert details["available"] and len(details["observation"]["samples"]) == 2
    assert details["observation"]["measurement_wire_contract_commit"] == (
        "650c5e2b387b9b0b2e46559626c85a4e24a3e55d"
    )
    assert details["observation"]["measurements"][0]["stages"][0] == [10, 8, 2, 6, 5]
    assert details["observation"]["events"][0]["kind"] == "camera"
    assert details["observation"]["events"][0]["camera"] == 8
    if fault:
        assert details["observation"]["events"][-1]["kind"] == "frame_fail"
    assert details["observation"]["cameras"][0]["weighted_diagnostic_fps"] == 30
    provenance = details["provenance"]
    assert provenance["binding_status"] == "captured"
    assert provenance["job_id"] == "observed-cycle"
    assert provenance["plan_hash"] == prepared["plan_hash"]
    assert provenance["configured_expected_firmware_sha256"] == "a" * 64
    assert provenance["association"]["status"] == "missing"
    assert "derby_diagnostics" not in shared
    assert replay["ok"] and replay["deduplicated"]
    assert "diagnostics" not in replay["diagnostic"]["derby_observation"]
    assert PROVENANCE_KEY not in replay["diagnostic"]
    assert "SYNTHETIC_PRIVATE" not in str(result) and "token=SECRET" not in str(result)
    assert device.closed and not backend.hardware_connected and backend.active == 0
    assert device.writes.count(b"s") == (0 if fault else 1)
    assert device.writes.count(b"q") == (0 if fault else 1)
