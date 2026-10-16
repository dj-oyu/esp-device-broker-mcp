"""Live operator CLI and exact one-capture consent; synthetic devices/keys only."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from test_pending_execution import prepare, running, submit
from test_plan_approval import running as serial_running
from test_plan_approval import serial_backend
from test_registry_http import LOCAL, REMOTE
from test_registry_http import registry_service as registry_service

from cardputer_mcp import cli
from cardputer_mcp.broker import BrokerError
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.models import CancelRequest, ExecuteRequest, RenewRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_cli import approve_live, review_plan
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError


class FakeClient:
    def __init__(self, submitted=False):
        self.detail = {
            "ok": True,
            "plan_hash": "a" * 64,
            "review_revision": "b" * 64,
            "execution_submitted": submitted,
            "attestation_text": "DEVICE HOME AND FIRMWARE VERIFIED",
            "image_delivery_recipient": "synthetic-owner",
            "plan": {"test": "capture_home", "job_id": "one"},
        }
        self.approve = Mock(return_value={"ok": True, "approved": True})
        self.image = Mock(return_value={"ok": True, "job_id": "one"})
        self.decide_image = Mock(return_value={"ok": True, "delivery_state": "approved"})

    def plan(self, plan_hash):
        assert plan_hash == self.detail["plan_hash"]
        return deepcopy(self.detail)


@pytest.mark.parametrize("submitted", [False, True])
def test_live_cli_review_and_approval_preserve_explicit_meaning(submitted):
    client = FakeClient(submitted)
    detail = review_plan(client, "a" * 64)
    assert "Esc" in detail["capture_notice"] and "AI service" in detail["capture_notice"]
    assert "does not extend" in detail["capture_notice"]
    assert ("submitted job" in detail["approval_effect"]) is submitted
    assert approve_live(
        client,
        "a" * 64,
        "b" * 64,
        "explicit-attestation",
        start_submitted=submitted,
        allow_image_delivery=True,
    )["approved"]
    client.approve.assert_called_once_with(
        "a" * 64, "b" * 64, "explicit-attestation", allow_image_delivery=True
    )


@pytest.mark.parametrize("change", ["revision", "submission", "noncapture", "recipient"])
def test_cli_never_refreshes_stale_consent_or_approves_the_wrong_action(change):
    client = FakeClient()
    if change == "revision":
        client.detail["review_revision"] = "c" * 64
    elif change == "submission":
        client.detail["execution_submitted"] = True
    elif change == "noncapture":
        client.detail["plan"]["test"] = "hello"
    else:
        client.detail["image_delivery_recipient"] = None
    with pytest.raises(OperatorClientError):
        approve_live(
            client, "a" * 64, "b" * 64, "", start_submitted=False, allow_image_delivery=True
        )
    client.approve.assert_not_called()


@pytest.mark.parametrize(
    "command",
    ["plan-review", "approve-live", "approve-and-start", "image-review", "image-decision"],
)
def test_cli_commands_require_local_environment_auth(monkeypatch, command, capsys):
    args = ["cardputer", "--auth-source", "key-environment", command]
    args += ["--job", "one"] if command.startswith("image-") else ["--plan", "a" * 64]
    if command.startswith("approve"):
        args += ["--review-revision", "b" * 64, "--attestation", ""]
    if command == "image-decision":
        args += ["--sha256", "a" * 64, "--delivery-revision", "b" * 64, "--decision", "approve"]
    monkeypatch.setattr("sys.argv", args)
    client = Mock()
    monkeypatch.setattr(cli, "OperatorClient", client)
    with pytest.raises(SystemExit, match="1"):
        cli.main()
    assert "operator_cli_requires_local_environment_credentials" in capsys.readouterr().err
    client.assert_not_called()


@pytest.mark.parametrize("command", ["approve-live", "approve-and-start"])
def test_cli_main_uses_only_live_client_and_explicit_option(monkeypatch, capsys, command):
    client = FakeClient(command == "approve-and-start")
    factory = Mock(return_value=client)
    monkeypatch.setattr(cli, "OperatorClient", factory)
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", "a" * 64)
    offline = Mock(side_effect=AssertionError("No database approval or server startup"))
    for name in ("approve_plan", "load_settings", "create_app", "run_client", "inventory_json"):
        monkeypatch.setattr(cli, name, offline)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cardputer",
            "--url",
            "http://127.0.0.1:9988/mcp",
            "--auth-source",
            "environment",
            command,
            "--plan",
            "a" * 64,
            "--review-revision",
            "b" * 64,
            "--attestation",
            "DEVICE HOME AND FIRMWARE VERIFIED",
            "--allow-image-delivery",
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["approved"]
    factory.assert_called_once_with("http://127.0.0.1:9988/mcp", "a" * 64)
    offline.assert_not_called()
    assert client.approve.call_args.kwargs == {"allow_image_delivery": True}


def approve_capture(service, digest, enabled=True):
    review = service.detail(digest)
    return service.approve(
        digest, review["review_revision"], review["attestation_text"], allow_image_delivery=enabled
    )


@pytest.mark.parametrize("command", ["image-review", "image-decision"])
def test_cli_image_commands_use_exact_existing_preview_binding(monkeypatch, capsys, command):
    client = FakeClient()
    monkeypatch.setattr(cli, "OperatorClient", Mock(return_value=client))
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", "a" * 64)
    args = ["cardputer", "--auth-source", "environment", command, "--job", "one"]
    if command == "image-decision":
        args += ["--sha256", "c" * 64, "--delivery-revision", "d" * 64, "--decision", "approve"]
    monkeypatch.setattr("sys.argv", args)
    cli.main()
    assert json.loads(capsys.readouterr().out)["ok"]
    if command == "image-review":
        client.image.assert_called_once_with("one")
        client.decide_image.assert_not_called()
    else:
        client.decide_image.assert_called_once_with("one", "c" * 64, "d" * 64, "approve")
        client.image.assert_not_called()
    client.approve.assert_not_called()


@pytest.mark.parametrize("submitted", [False, True])
async def test_exact_capture_consent_starts_only_submitted_and_never_transfers(tmp_path, submitted):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker, test=Action.CAPTURE)
        if submitted:
            submit(broker, digest)
        result = approve_capture(service, digest)
        assert result["execution_submitted"] is submitted
        assert result["image_delivery_opt_in"]
        assert broker.backend.calls == 0
        if not submitted:
            assert broker.queue.empty() and broker._store().job("submitted") is None
        request = ExecuteRequest(session=broker.session.capability, plan_hash=digest)
        broker.execute(request)
        assert broker.execute(request)["deduplicated"]
        await broker.queue.join()
        assert broker.job("submitted")["state"] == "succeeded"
        assert broker.backend.calls == 1
        assert broker.read_capture("submitted", "synthetic-client").png
        retained = broker.captures["submitted"]
        assert retained.deadline == clock.now + 300
        assert not broker.capture_delivery_consents
        with pytest.raises(BrokerError, match="capture_not_available"):
            broker.read_capture("submitted", "another-principal")
        other = prepare(broker, job="other", test=Action.CAPTURE)
        approve_capture(service, other, enabled=False)
        broker.execute(ExecuteRequest(session=broker.session.capability, plan_hash=other))
        await broker.queue.join()
        with pytest.raises(BrokerError, match="capture_delivery_approval_required"):
            broker.read_capture("other", "synthetic-client")
        clock.now = retained.deadline
        with pytest.raises(BrokerError, match="capture_not_available"):
            broker.read_capture("submitted", "synthetic-client")


@pytest.mark.parametrize(
    "change",
    [
        "epoch",
        "lease",
        "principal",
        "target",
        "firmware",
        "deadline",
        "work",
        "scope",
        "revoked",
        "validator",
        "work_expired",
        "nonfinite",
    ],
)
async def test_final_recipient_callback_cannot_expand_capture_consent(tmp_path, change):
    async with running(tmp_path, work_session=True) as (broker, service, clock):
        digest = prepare(broker, test=Action.CAPTURE)
        approve_capture(service, digest)
        consent = broker.capture_delivery_consents["submitted"]
        original = broker._consume_capture_delivery

        def consume(plan):
            def current(*args):
                if change == "epoch":
                    broker.epoch = "other"
                elif change == "lease":
                    broker.session = replace(broker.session)
                elif change == "principal":
                    broker.session.principal_id = "other"
                elif change == "target":
                    broker.backend.device = "changed-target"
                elif change == "firmware":
                    broker.settings = broker.settings.model_copy(
                        update={
                            "serial": SerialSettings(
                                selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
                                expected_firmware_sha256="b" * 64,
                            )
                        }
                    )
                elif change == "deadline":
                    clock.now = consent.intent.deadline
                elif change == "work":
                    broker.work_sessions[consent.work.id] = replace(consent.work)
                elif change == "scope":
                    consent.work.scopes = frozenset({"read"})
                elif change == "validator":
                    broker.principal_valid = lambda *args: False
                elif change == "work_expired":
                    consent.work.deadline = clock.now
                elif change == "nonfinite":
                    broker.session.deadline = float("inf")
                return change != "revoked"

            broker.principal_valid = current
            return original(plan)

        broker._consume_capture_delivery = consume
        broker.execute(ExecuteRequest(session=broker.session.capability, plan_hash=digest))
        await broker.queue.join()
        assert broker.captures["submitted"].delivery_state == "pending"
        assert not broker.capture_delivery_consents
        with pytest.raises(BrokerError, match="capture_delivery_approval_required"):
            broker.read_capture("submitted", "synthetic-client")


async def test_prepared_consent_cannot_renew_its_deadline_or_revive_after_cancel(tmp_path):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker, test=Action.CAPTURE)
        approve_capture(service, digest)
        consent = broker.capture_delivery_consents["submitted"]
        clock.now += 10
        broker.renew(RenewRequest(session=broker.session.capability, lease_seconds=300))
        approve_capture(service, digest)
        assert broker.capture_delivery_consents["submitted"] is consent
        assert consent.intent.deadline == 130
        clock.now = consent.intent.deadline
        broker.maintenance_tick()
        with pytest.raises(BrokerError, match="fresh_approval"):
            approve_capture(service, digest)
        broker.execute(ExecuteRequest(session=broker.session.capability, plan_hash=digest))
        broker.cancel(CancelRequest(session=broker.session.capability, job_id="submitted"))
        await broker.queue.join()
        assert broker.backend.calls == 0 and not broker.capture_delivery_consents
        with pytest.raises(BrokerError):
            approve_capture(service, digest)


@pytest.mark.parametrize("enabled", [True, "true", 1])
async def test_only_capture_and_strict_boolean_can_preapprove_delivery(tmp_path, enabled):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        review = service.detail(digest)
        with pytest.raises(BrokerError, match="capture_delivery_consent_not_available"):
            service.approve(digest, review["review_revision"], "", allow_image_delivery=enabled)
        assert not broker.capture_delivery_consents
        assert broker._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


async def test_capture_opt_in_keeps_home_attestation_and_serial_preflight_without_open(tmp_path):
    backend, inventory, calls = serial_backend()
    async with serial_running(tmp_path, backend=backend, valid=lambda *args: True) as (
        broker,
        service,
        _,
    ):
        from test_plan_approval import prepare as serial_prepare

        digest = serial_prepare(broker, test=Action.CAPTURE, principal="synthetic-owner")
        review = service.detail(digest)
        assert review["attestation_text"] == "DEVICE HOME AND FIRMWARE VERIFIED"
        for phrase in ("", "yes", "DEVICE FREE AND FIRMWARE VERIFIED"):
            with pytest.raises(BrokerError, match="device_attestation_required"):
                service.approve(
                    digest, review["review_revision"], phrase, allow_image_delivery=True
                )
            assert not broker.capture_delivery_consents
        service.approve(
            digest, review["review_revision"], review["attestation_text"], allow_image_delivery=True
        )
        assert len(broker.capture_delivery_consents) == 1
        assert backend.calls == 0 and not backend.hardware_connected
        assert calls == ["enumerate", "enumerate"]
        assert len(inventory) == 1


async def test_stale_prepared_review_cannot_start_new_submission_with_delivery_opt_in(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker, test=Action.CAPTURE)
        before = service.detail(digest)
        submit(broker, digest)
        with pytest.raises(BrokerError, match="stale_plan_review"):
            service.approve(digest, before["review_revision"], "", allow_image_delivery=True)
        assert broker.job("submitted")["state"] == "awaiting_approval"
        assert broker.queue.empty() and not broker.capture_delivery_consents
        assert broker._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


async def test_last_delivery_auth_callback_cannot_change_prepared_approval_into_start(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker, test=Action.CAPTURE)
        review = service.detail(digest)
        original = broker._capture_delivery_authorized
        calls = 0
        changed = False

        def authorize(consent, plan):
            nonlocal calls
            calls += 1
            if calls == 2:

                def current(*args):
                    nonlocal changed
                    if not changed:
                        changed = True
                        submit(broker, digest)
                    return True

                broker.principal_valid = current
            return original(consent, plan)

        broker._capture_delivery_authorized = authorize
        with pytest.raises(BrokerError, match="stale_plan_review"):
            service.approve(digest, review["review_revision"], "", allow_image_delivery=True)
        assert changed and calls == 2
        assert broker.job("submitted")["state"] == "awaiting_approval"
        assert broker.queue.empty() and not broker.capture_delivery_consents
        assert broker._store().db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


async def test_live_cli_and_local_api_approve_exact_submission_and_image(registry_service):
    env = registry_service
    env.resident.settings = env.resident.settings.model_copy(update={"allow_mock_execution": False})
    operator = OperatorClient(env.url, LOCAL)
    async with connect(env.url, env.keys["second"].token) as remote:
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        prepared = await call(
            remote,
            "prepare_test",
            {"session": lease["session"], "job_id": "one", "test": "capture_home"},
        )
        digest = prepared["plan_hash"]
        await call(
            remote,
            "execute_test",
            {"session": lease["session"], "plan_hash": digest, "wait_for_approval": True},
        )
        review = await asyncio.to_thread(review_plan, operator, digest)
        assert review["execution_submitted"]
        assert review["image_delivery_recipient"] == env.keys["second"].metadata.principal_id
        async with httpx2.AsyncClient(trust_env=False) as http:
            response = await http.post(
                env.url.removesuffix("/mcp") + "/operator/approve-plan",
                headers={"Authorization": f"Bearer {REMOTE}"},
                json={
                    "plan_hash": digest,
                    "review_revision": review["review_revision"],
                    "attestation": "",
                    "allow_image_delivery": True,
                },
            )
            assert response.status_code == 403
            assert not env.resident.capture_delivery_consents
            for invalid in ("true", 1):
                response = await http.post(
                    env.url.removesuffix("/mcp") + "/operator/approve-plan",
                    headers={"Authorization": f"Bearer {LOCAL}"},
                    json={
                        "plan_hash": digest,
                        "review_revision": review["review_revision"],
                        "attestation": "",
                        "allow_image_delivery": invalid,
                    },
                )
                assert response.status_code == 400
                assert not env.resident.capture_delivery_consents
        result = await asyncio.to_thread(
            approve_live,
            operator,
            digest,
            review["review_revision"],
            "",
            start_submitted=True,
            allow_image_delivery=True,
        )
        assert result["execution_submitted"] and result["image_delivery_opt_in"]
        await env.resident.queue.join()
        assert env.backend.calls == 1
        image = await remote.call_tool("read_capture", {"request": {"job_id": "one"}})
        assert not image.is_error
        assert any(item.type == "image" for item in image.content)
        preview = await asyncio.to_thread(operator.image, "one")
        deadline = env.resident.captures["one"].deadline
        await asyncio.to_thread(
            operator.decide_image, "one", preview["sha256"], preview["delivery_revision"], "approve"
        )
        assert env.resident.captures["one"].deadline == deadline
        env.registry.revoke(env.keys["second"].metadata.key_id)
        with pytest.raises(MCPError):
            await remote.call_tool("read_capture", {"request": {"job_id": "one"}})
