"""Actual guarded HTTP previews from retained synthetic broker captures; no Tk/USB."""

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime

import httpx2
import pytest
from test_authenticated_mock import LOCAL, REMOTE
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_authenticated_mock import event_loop_policy as event_loop_policy

from cardputer_mcp.auth import AuthState
from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import RetainedCapture, digest_plan
from cardputer_mcp.capture import LegacyCaptureDecoder, mock_image
from cardputer_mcp.derby_smoke import SMOKE
from cardputer_mcp.gui_live_preview import FAILED, PreviewState
from cardputer_mcp.gui_live_state import LiveState, apply, begin
from cardputer_mcp.gui_live_transport import AUTH_FAILED, LiveRunner
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_client import OperatorClient
from cardputer_mcp.plan_approval import PlanApprovals

PRINCIPAL = "ephemeral:local_cli"


def retain_capture(broker, *, derby=False):
    """Install bounded historical state and real PNG bytes, never a response fixture."""
    job_id = "preview_derby" if derby else "preview_capture"
    plan = {
        "version": 1,
        "broker_epoch": broker.epoch,
        "session_id": "c" * 32,
        "job_id": job_id,
        "test": Action.DERBY.value if derby else Action.CAPTURE.value,
        "device": "mock-cardputer",
        "backend": "mock",
        **MockBackend().prepare(Action.CAPTURE),
    }
    image = mock_image()
    result = "synthetic_capture_not_device"
    if derby:
        # A retained serial-job history can be inspected by a mock resident. The
        # serial schema is synthetic; no serial backend or inventory is called.
        plan.update(
            device="configured-cardputer",
            backend="serial",
            stages=["serial_open", "derby_smoke", "serial_close"],
            port="COM42",
            device_fingerprint="d" * 64,
            protocol="pocketjs-derby-smoke-v1",
            expected_firmware_sha256="f" * 64,
            firmware_verification="configured_expected_hash_only",
            observe_seconds=None,
            effects=["Synthetic retained history; no device operation"],
            smoke=SMOKE,
        )
        decoder = LegacyCaptureDecoder()
        decoder.feed(b"CAPTURE_BEGIN 240 135\n")
        for y in range(135):
            decoder.feed(f"PIX {y} ".encode() + b"0000" * 240 + b"\n")
        decoder.feed(b"CAPTURE_END\n")
        image = decoder.finish()
        result = "derby_demo_cycle_observed_render_buffer_lcd_unverified"
    PlanApprovals._schema(plan)
    plan_hash = digest_plan(plan)
    broker._store().put_plan(plan_hash, plan)
    broker._store().add_job(job_id, plan_hash)
    broker._store().set_job(job_id, "succeeded", result)
    retained = RetainedCapture(
        PRINCIPAL, image, broker.clock() + 120, "local_cli", datetime.now(UTC).isoformat()
    )
    broker.captures[job_id] = retained
    return job_id, retained, plan_hash


async def next_event(runner, kind, *, expected_disconnect_error=None):
    async with asyncio.timeout(3):
        while True:
            for event in runner.drain():
                if event.kind == "disconnected":
                    # Revocation may reject the held subscription before the
                    # independently dispatched explicit image read completes.
                    assert expected_disconnect_error is not None, event.payload
                    assert event.payload == {"error": expected_disconnect_error}
                    continue
                if event.kind == kind:
                    return event
            await asyncio.sleep(0.005)


async def available(runner, job_id):
    runner.start(1)
    message = await next_event(runner, "message")
    state = begin(LiveState())
    state = apply(state, message.payload, state.generation, message.started_at)
    assert state.connection == "live"
    card = state.card(job_id)
    assert card is not None and card.image is not None
    preview = PreviewState().reconcile(
        state.generation, state.timeline.broker_epoch, card.image, True, time.monotonic()
    )
    return state, preview


@pytest.mark.parametrize("derby", [False, True], ids=["capture", "derby"])
async def test_real_retained_preview_wire_accepts_current_epoch_without_renewal(
    authenticated_mock, monkeypatch, derby
):
    url, broker = authenticated_mock
    job_id, retained, plan_hash = retain_capture(broker, derby=derby)
    image_calls = []
    original = broker.operator_image

    def counted(job):
        image_calls.append(job)
        return original(job)

    monkeypatch.setattr(broker, "operator_image", counted)
    runner = LiveRunner(OperatorClient(url, LOCAL))
    try:
        state, preview = await available(runner, job_id)
        assert image_calls == []
        assert state.card(job_id).test == ("derby_smoke" if derby else "capture_home")
        async with httpx2.AsyncClient(base_url=url.removesuffix("/mcp"), trust_env=False) as http:
            for token, status in ((None, 401), ("c" * 64, 401), (REMOTE, 403)):
                headers = {"Authorization": "Bearer " + token} if token else {}
                denied = await http.post(
                    "/operator/image", json={"job_id": job_id}, headers=headers
                )
                assert denied.status_code == status and "image_base64" not in denied.text
        assert image_calls == []
        original_deadline = preview.row.expires_at
        request_id = runner.read_image(1, job_id, expires_at=original_deadline)
        preview = preview.started(request_id)
        event = await next_event(runner, "image")
        response = event.payload["response"]
        accepted = preview.accept(
            event.generation, job_id, request_id, response, event.started_at, time.monotonic()
        )
        # This is the actual Broker/operator JSON, with no response augmentation.
        assert response["broker_epoch"] == state.timeline.broker_epoch == broker.epoch
        assert response["recipient_principal"] == PRINCIPAL
        if derby:
            assert response["derby_scope"]["broker_epoch"] == response["broker_epoch"]
            assert response["derby_scope"]["plan_hash"] == plan_hash
            assert response["derby_scope"]["principal_id"] == PRINCIPAL
        else:
            assert "derby_scope" not in response
        assert accepted.preview is not None and accepted.preview.png == retained.image.png
        assert accepted.row.expires_at <= original_deadline
        assert broker.captures[job_id] is retained and retained.deadline > broker.clock()
        assert image_calls == [job_id] and broker.backend.calls == 0 and broker.queue.empty()
        assert broker._store().job(job_id)["state"] == "succeeded"
    finally:
        runner.close()
        assert not runner._thread.is_alive()


async def test_preview_read_rechecks_current_operator_authentication(
    authenticated_mock, monkeypatch
):
    url, broker = authenticated_mock
    job_id, retained, _ = retain_capture(broker)
    permitted = [True]
    original = AuthState.authenticate

    def authenticate(auth, header):
        if header == "Bearer " + LOCAL and not permitted[0]:
            return None
        return original(auth, header)

    monkeypatch.setattr(AuthState, "authenticate", authenticate)
    runner = LiveRunner(OperatorClient(url, LOCAL))
    try:
        _, preview = await available(runner, job_id)
        permitted[0] = False
        request_id = runner.read_image(1, job_id, expires_at=preview.row.expires_at)
        event = await next_event(runner, "image", expected_disconnect_error=AUTH_FAILED)
        assert event.payload["error"] == AUTH_FAILED and "response" not in event.payload
        rejected = preview.started(request_id).accept(
            1, job_id, request_id, None, event.started_at, time.monotonic()
        )
        assert rejected.preview is None and rejected.message == FAILED
        assert broker.captures[job_id] is retained and broker.backend.calls == 0
    finally:
        permitted[0] = True
        runner.close()
        assert not runner._thread.is_alive()


async def test_preview_read_does_not_revive_expired_retained_capture(authenticated_mock):
    url, broker = authenticated_mock
    job_id, retained, _ = retain_capture(broker)
    runner = LiveRunner(OperatorClient(url, LOCAL))
    try:
        _, preview = await available(runner, job_id)
        broker.captures[job_id] = replace(retained, deadline=broker.clock() - 1)
        request_id = runner.read_image(1, job_id, expires_at=preview.row.expires_at)
        event = await next_event(runner, "image")
        response = event.payload["response"]
        assert response == {"ok": False, "error": "capture_not_available"}
        rejected = preview.started(request_id).accept(
            1, job_id, request_id, response, event.started_at, time.monotonic()
        )
        assert rejected.preview is None and rejected.message == FAILED
        assert job_id not in broker.captures and broker.backend.calls == 0 and broker.queue.empty()
    finally:
        runner.close()
        assert not runner._thread.is_alive()
