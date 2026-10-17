"""Bounded DERBY operator windows; synthetic CLI, identity, clock and pixels only."""

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest
from test_operator_launch import LOCAL, SID
from test_plan_approval import serial_backend

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.derby_operator_launch import (
    DerbyLaunchScope,
    DerbyOperatorLaunchSlot,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_launch import HOME_ATTESTATION


class Process(BuildProcess):
    def __init__(self, replies):
        super().__init__()
        self.replies = replies
        self.calls = []
        self.callback = lambda: None
        self.fail = False

    def run(self, args, env, cwd, emit, **options):
        self.calls.append((list(args), dict(env)))
        options["monitor"]()
        self.callback()
        if self.fail:
            raise TimeoutError("synthetic unconfirmed child")
        emit(json.dumps(self.replies[args[5]]), "stdout")
        return 0


@pytest.fixture
def window(tmp_path):
    backend, _, _ = serial_backend()
    plan = dict(
        version=1,
        broker_epoch="e" * 32,
        session_id="d" * 32,
        device="configured-cardputer",
        backend="serial",
        job_id="derby-one",
        test="derby_smoke",
        **backend.prepare(Action.DERBY),
    )
    scope = DerbyLaunchScope(
        broker_epoch=plan["broker_epoch"],
        job_id=plan["job_id"],
        plan_hash=digest_plan(plan),
        principal_id="synthetic-owner",
        device_fingerprint=plan["device_fingerprint"],
        expected_firmware_sha256=plan["expected_firmware_sha256"],
    )
    png = b"synthetic pixel buffer"
    image_sha = hashlib.sha256(png).hexdigest()
    replies = {
        "plan-review": dict(
            ok=True,
            plan=plan,
            plan_hash=scope.plan_hash,
            review_revision="b" * 64,
            execution_submitted=True,
            attestation="device_home_firmware",
            attestation_text=HOME_ATTESTATION,
            image_delivery_recipient=scope.principal_id,
            lease_remaining_seconds=300.0,
            intent_remaining_seconds=300.0,
        ),
        "approve-and-start": dict(
            ok=True,
            approved=True,
            execution_submitted=True,
            plan_hash=scope.plan_hash,
            job_state="queued",
        ),
        "image-review": dict(
            ok=True,
            derby_scope=scope.model_dump(),
            job_id=scope.job_id,
            recipient_principal=scope.principal_id,
            delivery_state="pending",
            remaining_seconds=299.0,
            sha256=image_sha,
            delivery_revision="c" * 64,
            image_base64=base64.b64encode(png).decode(),
        ),
        "image-decision": dict(ok=True, job_id=scope.job_id, delivery_state="approved"),
    }
    exe = tmp_path / "cardputer.exe"
    exe.write_bytes(b"synthetic executable")
    clock = SimpleNamespace(now=100.0)
    process = Process(replies)

    def arm(phase="execution", **kwargs):
        return DerbyOperatorLaunchSlot(
            exe,
            hashlib.sha256(exe.read_bytes()).hexdigest(),
            tmp_path,
            "http://127.0.0.1:9988/mcp",
            LOCAL,
            SID,
            scope=scope,
            phase=phase,
            clock=lambda: clock.now,
            process=process,
            **kwargs,
        )

    def request(action, **kwargs):
        return {**scope.model_dump(), "action": "derby-" + action, **kwargs}

    return SimpleNamespace(
        scope=scope,
        clock=clock,
        process=process,
        replies=replies,
        arm=arm,
        request=request,
        png=png,
        image_sha=image_sha,
    )


def approve(w):
    return w.request("approve-and-start", review_revision="b" * 64, attestation=HOME_ATTESTATION)


def decide(w):
    return w.request(
        "image-decision", sha256=w.image_sha, delivery_revision="c" * 64, decision="approve"
    )


def test_separate_execution_and_image_windows(window):
    w = window
    execution = w.arm()
    assert execution.handle(w.request("plan-review"), peer_sid=SID)["ok"]
    assert execution.handle(approve(w), peer_sid=SID)["ok"]
    assert execution.closed and execution._local_token is None
    assert not execution.handle(approve(w), peer_sid=SID)["ok"]
    w.clock.now += 180  # The expired execution window is never extended for the race.
    image = w.arm("image")
    result = image.handle(w.request("image-review"), peer_sid=SID)
    assert result["sha256"] == w.image_sha and "image_base64" not in result
    assert image.handle(decide(w), peer_sid=SID)["ok"]
    assert image.closed and image._local_token is None
    assert not image.handle(decide(w), peer_sid=SID)["ok"]
    assert [c[0][5] for c in w.process.calls] == [
        "plan-review",
        "approve-and-start",
        "image-review",
        "image-decision",
    ]
    assert all("--allow-image-delivery" not in c[0] for c in w.process.calls)
    assert w.process.calls[-1][0][6:] == [
        "--job",
        w.scope.job_id,
        "--sha256",
        w.image_sha,
        "--delivery-revision",
        "c" * 64,
        "--decision",
        "approve",
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "other-derby"),
        ("plan_hash", "9" * 64),
        ("broker_epoch", "9" * 32),
        ("principal_id", "other-owner"),
        ("device_fingerprint", "9" * 64),
        ("expected_firmware_sha256", "9" * 64),
        ("arbitrary_keys", "r"),
        ("allow_image_delivery", True),
        ("action", "plan-review"),
    ],
)
@pytest.mark.parametrize("phase,action", [("execution", "plan-review"), ("image", "image-review")])
def test_scope_cannot_be_selected_or_retargeted(window, field, value, phase, action):
    w = window
    slot = w.arm(phase)
    payload = w.request(action)
    payload[field] = value
    assert not slot.handle(payload, peer_sid=SID)["ok"]
    assert slot.closed and not w.process.calls


@pytest.mark.parametrize(
    "change",
    ["test", "firmware", "device", "smoke", "principal", "epoch", "submitted", "attestation"],
)
def test_review_checks_actual_child_plan_not_just_requested_scope(window, change):
    w = window
    detail = w.replies["plan-review"]
    if change in ("test", "firmware", "device", "smoke", "epoch"):
        p = detail["plan"]
        if change == "smoke":
            p["smoke"] = {**p["smoke"], "commands": ["r"]}
        else:
            p[
                {
                    "test": "test",
                    "firmware": "expected_firmware_sha256",
                    "device": "device_fingerprint",
                    "epoch": "broker_epoch",
                }[change]
            ] = "9" * 64 if change in ("firmware", "device") else "capture_home"
    elif change == "principal":
        detail["image_delivery_recipient"] = "other"
    elif change == "submitted":
        detail["execution_submitted"] = False
    else:
        detail["attestation_text"] = ""
    slot = w.arm()
    assert not slot.handle(w.request("plan-review"), peer_sid=SID)["ok"]
    assert slot.closed and len(w.process.calls) == 1


@pytest.mark.parametrize(
    "phase,review,decision",
    [("execution", "plan-review", approve), ("image", "image-review", decide)],
)
@pytest.mark.parametrize(
    "when", ["before_review", "during_review", "after_review", "decision_timeout"]
)
def test_expiry_and_uncertain_decision_never_rearm(window, phase, review, decision, when):
    w = window
    slot = w.arm(phase)
    if when == "before_review":
        w.clock.now += 30
    if when == "during_review":
        w.process.callback = lambda: setattr(w.clock, "now", 130.0)
    result = slot.handle(w.request(review), peer_sid=SID)
    if when in ("before_review", "during_review"):
        assert not result["ok"]
    else:
        assert result["ok"]
        if when == "after_review":
            w.clock.now = 130.0
        else:
            w.process.fail = True
        assert not slot.handle(decision(w), peer_sid=SID)["ok"]
    calls = len(w.process.calls)
    assert not slot.handle(decision(w), peer_sid=SID)["ok"]
    assert slot.closed and len(w.process.calls) == calls and slot._local_token is None


@pytest.mark.parametrize(
    "field,value", [("sha256", "9" * 64), ("delivery_revision", "9" * 64), ("decision", "invalid")]
)
def test_image_decision_binds_exact_review(window, field, value):
    w = window
    slot = w.arm("image")
    assert slot.handle(w.request("image-review"), peer_sid=SID)["ok"]
    payload = decide(w)
    payload[field] = value
    assert not slot.handle(payload, peer_sid=SID)["ok"]
    assert slot.closed and len(w.process.calls) == 1


@pytest.mark.parametrize("change", ["scope", "pixels", "recipient", "revision", "expired", "state"])
def test_image_preview_provenance_and_deadline(window, change):
    w = window
    d = w.replies["image-review"]
    if change == "scope":
        d["derby_scope"] = {**d["derby_scope"], "job_id": "another-derby"}
    elif change == "pixels":
        d["image_base64"] = base64.b64encode(b"different").decode()
    else:
        key, value = {
            "recipient": ("recipient_principal", "another"),
            "revision": ("delivery_revision", "invalid"),
            "expired": ("remaining_seconds", 0),
            "state": ("delivery_state", "rejected"),
        }[change]
        d[key] = value
    slot = w.arm("image")
    assert not slot.handle(w.request("image-review"), peer_sid=SID)["ok"]
    assert slot.closed


def test_reentrant_review_consumes_window(window):
    w = window
    slot = w.arm()
    w.process.callback = lambda: slot.handle(w.request("plan-review"), peer_sid=SID)
    assert not slot.handle(w.request("plan-review"), peer_sid=SID)["ok"]
    assert slot.closed and len(w.process.calls) == 1


def test_image_and_execution_actions_cannot_cross_windows(window):
    w = window
    assert not w.arm().handle(w.request("image-review"), peer_sid=SID)["ok"]
    assert not w.arm("image").handle(w.request("plan-review"), peer_sid=SID)["ok"]
    assert not w.process.calls


def test_image_review_accepts_bounded_full_size_png_output(window):
    w = window
    png = bytes(range(256)) * 400
    w.replies["image-review"].update(
        image_base64=base64.b64encode(png).decode(), sha256=hashlib.sha256(png).hexdigest()
    )
    assert w.arm("image").handle(w.request("image-review"), peer_sid=SID)["ok"]


def test_wrong_sid_never_starts_child(window):
    w = window
    slot = w.arm()
    assert not slot.handle(w.request("plan-review"), peer_sid="S-1-5-21-999")["ok"]
    assert not slot.closed and not w.process.calls


@pytest.mark.parametrize("phase,review", [("execution", "plan-review"), ("image", "image-review")])
def test_duplicate_review_and_decision_without_review_are_consumed(window, phase, review):
    w = window
    slot = w.arm(phase)
    assert slot.handle(w.request(review), peer_sid=SID)["ok"]
    assert not slot.handle(w.request(review), peer_sid=SID)["ok"]
    assert slot.closed and len(w.process.calls) == 1
    slot = w.arm(phase)
    assert not slot.handle(approve(w) if phase == "execution" else decide(w), peer_sid=SID)["ok"]
    assert slot.closed and len(w.process.calls) == 1


def test_derby_request_cli_uses_only_exact_payload_without_loading_credentials(
    window, monkeypatch, capsys
):
    from unittest.mock import Mock

    from cardputer_mcp import cli, operator_launch_pipe

    w = window
    sender = Mock(return_value={"ok": True})
    monkeypatch.setattr(operator_launch_pipe, "request_operator_launch", sender)
    unexpected = Mock(side_effect=AssertionError("No broker token lookup"))
    monkeypatch.setattr(cli, "load_environment_token", unexpected)
    monkeypatch.setattr(cli, "OperatorClient", unexpected)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cardputer",
            "derby-operator-request",
            "--pipe",
            "public-pipe",
            "--action",
            "derby-image-decision",
            "--epoch",
            w.scope.broker_epoch,
            "--job",
            w.scope.job_id,
            "--plan",
            w.scope.plan_hash,
            "--principal",
            w.scope.principal_id,
            "--device-fingerprint",
            w.scope.device_fingerprint,
            "--firmware-sha256",
            w.scope.expected_firmware_sha256,
            "--sha256",
            w.image_sha,
            "--delivery-revision",
            "c" * 64,
            "--decision",
            "approve",
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["ok"]
    assert sender.call_args.args == (
        "public-pipe",
        {
            **decide(w),
            "review_revision": None,
            "attestation": None,
        },
    )
    unexpected.assert_not_called()
