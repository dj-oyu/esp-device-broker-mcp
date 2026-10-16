"""One pre-armed CLI slot; fake executables, clocks and credentials only."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import digest_plan
from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_launch import (
    CaptureLaunchScope,
    OperatorLaunchError,
    OperatorLaunchSlot,
    decode_message,
)

SID = "S-1-5-21-123-456-789-1001"
LOCAL = "a" * 64


class FakeProcess(BuildProcess):
    def __init__(self, review):
        super().__init__()
        self.review = review
        self.calls = []
        self.fail_approval = False
        self.fail_review = False

    def run(self, args, env, cwd, emit, **options):
        self.calls.append(
            {"args": list(args), "env": dict(env), "env_ref": env, "cwd": cwd, "options": options}
        )
        options["monitor"]()
        if args[5] == "plan-review":
            if self.fail_review:
                raise RuntimeError("synthetic-private-secret " + LOCAL)
            result = deepcopy(self.review)
        else:
            if self.fail_approval:
                raise TimeoutError("synthetic-private-secret " + LOCAL)
            result = {
                "ok": True,
                "approved": True,
                "execution_submitted": True,
                "plan_hash": self.review["plan_hash"],
                "job_state": "queued",
                "image_delivery_opt_in": "--allow-image-delivery" in args,
            }
        emit("synthetic private stderr " + LOCAL, "stderr")
        for line in json.dumps(result, indent=2).splitlines():
            emit(line, "stdout")
        return 0


@pytest.fixture
def armed(tmp_path):
    plan = {
        "version": 1,
        "broker_epoch": "e" * 32,
        "session_id": "d" * 32,
        "device": "mock-cardputer",
        "backend": "mock",
        "job_id": "capture-one",
        "test": "capture_home",
        **MockBackend().prepare(Action.CAPTURE),
    }
    digest = digest_plan(plan)
    scope = CaptureLaunchScope(
        broker_epoch=plan["broker_epoch"],
        job_id=plan["job_id"],
        plan_hash=digest,
        principal_id="synthetic-capture-owner",
    )
    review = {
        "ok": True,
        "plan": plan,
        "plan_hash": digest,
        "review_revision": "b" * 64,
        "execution_submitted": True,
        "attestation": "none",
        "attestation_text": "",
        "image_delivery_recipient": scope.principal_id,
        "lease_remaining_seconds": 300.0,
        "intent_remaining_seconds": 300.0,
    }
    executable = tmp_path / "cardputer.exe"
    executable.write_bytes(b"synthetic EXE, never executed")
    clock = SimpleNamespace(now=100.0)
    process = FakeProcess(review)
    slot = OperatorLaunchSlot(
        executable,
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        tmp_path,
        "http://127.0.0.1:9988/mcp",
        LOCAL,
        SID,
        scope,
        allow_image_delivery=True,
        clock=lambda: clock.now,
        process=process,
    )
    return SimpleNamespace(
        slot=slot,
        scope=scope,
        process=process,
        review=review,
        clock=clock,
        executable=executable,
        cwd=tmp_path,
    )


def request(env, action="plan-review", **changes):
    result = {**env.scope.model_dump(), "action": action}
    if action == "approve-and-start":
        result.update(review_revision="b" * 64, attestation="", allow_image_delivery=True)
    return {**result, **changes}


def test_only_exact_fixed_cli_children_receive_local_token_and_no_ambient_secrets(
    armed, monkeypatch
):
    for name in (
        "CARDPUTER_REMOTE_TOKEN",
        "CARDPUTER_CLIENT_KEY",
        "CONTROL_PLANE_API_KEY",
        "CARDPUTER_TUNNEL_API_KEY",
        "CARDPUTER_TUNNEL_BEARER",
        "OPENAI_API_KEY",
        "PYTHONPATH",
        "PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    ):
        monkeypatch.setenv(name, "synthetic-unrelated-secret")
    assert armed.slot.handle(request(armed), peer_sid=SID)["status"] == "reviewed"
    assert len(armed.process.calls) == 1
    assert armed.slot.handle(request(armed, "approve-and-start"), peer_sid=SID)["ok"]
    review, approval = armed.process.calls
    assert review["args"] == [
        str(armed.executable),
        "--url",
        "http://127.0.0.1:9988/mcp",
        "--auth-source",
        "environment",
        "plan-review",
        "--plan",
        armed.scope.plan_hash,
    ]
    assert approval["args"][5:] == [
        "approve-and-start",
        "--plan",
        armed.scope.plan_hash,
        "--review-revision",
        "b" * 64,
        "--attestation",
        "",
        "--allow-image-delivery",
    ]
    for call in armed.process.calls:
        assert call["cwd"] == armed.cwd
        assert call["env"]["CARDPUTER_LOCAL_TOKEN"] == LOCAL
        assert "synthetic-unrelated-secret" not in call["env"].values()
        assert call["env_ref"] == {}
        assert LOCAL not in " ".join(call["args"])
    assert LOCAL not in repr(armed.slot)
    assert armed.slot._local_token is None and armed.slot.closed
    assert not any(
        LOCAL.encode() in path.read_bytes() for path in armed.cwd.iterdir() if path.is_file()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("broker_epoch", "f" * 32),
        ("job_id", "another"),
        ("plan_hash", "c" * 64),
        ("principal_id", "another"),
        ("command", "arbitrary"),
        ("cwd", "elsewhere"),
        ("url", "http://example.invalid"),
        ("environment", {"secret": "no"}),
        ("action", "serve"),
        ("executable", "arbitrary.exe"),
    ],
)
def test_wrong_scope_or_extra_arguments_never_start_any_child(armed, field, value):
    result = armed.slot.handle(request(armed, **{field: value}), peer_sid=SID)
    assert result == {"ok": False, "error": "operator_launch_review_refused"}
    assert armed.process.calls == [] and armed.slot.closed


def test_another_os_user_cannot_review_or_consume_owner_slot(armed):
    assert not armed.slot.handle(request(armed), peer_sid="S-1-5-21-9-8-7-1002")["ok"]
    assert not armed.process.calls and not armed.slot.closed
    assert armed.slot.handle(request(armed), peer_sid=SID)["ok"]


@pytest.mark.parametrize("when", ["before_review", "before_approval"])
def test_original_deadline_cannot_be_extended_by_requests(armed, when):
    if when == "before_approval":
        armed.review["intent_remaining_seconds"] = 5.0
        assert armed.slot.handle(request(armed), peer_sid=SID)["ok"]
        armed.clock.now = 105.0
        payload = request(armed, "approve-and-start")
    else:
        armed.clock.now = 130.0
        payload = request(armed)
    assert not armed.slot.handle(payload, peer_sid=SID)["ok"]
    assert len(armed.process.calls) == (1 if when == "before_approval" else 0)
    assert armed.slot._local_token is None


@pytest.mark.parametrize(
    "change",
    [
        "epoch",
        "job",
        "recipient",
        "hash",
        "revision",
        "unsubmitted",
        "noncapture",
        "attestation",
        "expired",
        "nan",
    ],
)
def test_untrusted_or_changed_review_cannot_arm_approval(armed, change):
    detail = armed.review
    if change == "epoch":
        detail["plan"]["broker_epoch"] = "f" * 32
    elif change == "job":
        detail["plan"]["job_id"] = "other"
    elif change == "recipient":
        detail["image_delivery_recipient"] = "other"
    elif change == "hash":
        detail["plan_hash"] = "c" * 64
    elif change == "revision":
        detail["review_revision"] = "wrong"
    elif change == "unsubmitted":
        detail["execution_submitted"] = False
    elif change == "noncapture":
        detail["plan"]["test"] = "diagnostics"
    elif change == "attestation":
        detail["attestation_text"] = "unexpected secret"
    else:
        detail["intent_remaining_seconds"] = 0 if change == "expired" else float("nan")
    result = armed.slot.handle(request(armed), peer_sid=SID)
    assert result == {"ok": False, "error": "operator_launch_review_refused"}
    assert len(armed.process.calls) == 1 and armed.slot.closed
    assert LOCAL not in json.dumps(result)


@pytest.mark.parametrize("change", ["missing_review", "revision", "attestation", "image_policy"])
def test_approval_requires_review_exact_revision_and_explicit_authorized_effects(armed, change):
    if change != "missing_review":
        assert armed.slot.handle(request(armed), peer_sid=SID)["ok"]
    payload = request(armed, "approve-and-start")
    if change == "revision":
        payload["review_revision"] = "c" * 64
    elif change == "attestation":
        payload["attestation"] = "DEVICE HOME AND FIRMWARE VERIFIED"
    elif change == "image_policy":
        armed.slot._allow_image_delivery = False
    result = armed.slot.handle(payload, peer_sid=SID)
    assert not result["ok"] and armed.slot.closed
    assert len(armed.process.calls) == (0 if change == "missing_review" else 1)


def test_lost_approval_reply_and_concurrent_duplicates_never_replay(armed):
    assert armed.slot.handle(request(armed), peer_sid=SID)["ok"]
    armed.process.fail_approval = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: armed.slot.handle(request(armed, "approve-and-start"), peer_sid=SID),
                range(2),
            )
        )
    assert len(armed.process.calls) == 2
    assert all(not result["ok"] for result in results)
    assert any(result["error"] == "operator_launch_approval_outcome_unknown" for result in results)
    assert LOCAL not in json.dumps(results) and "synthetic-private-secret" not in json.dumps(
        results
    )


@pytest.mark.parametrize("changed", ["executable", "cwd"])
def test_fixed_paths_and_pinned_executable_are_rechecked_before_launch(armed, changed):
    if changed == "executable":
        armed.executable.write_bytes(b"replaced EXE")
    else:
        armed.slot._cwd_identity = (0, 0)
    assert not armed.slot.handle(request(armed), peer_sid=SID)["ok"]
    assert not armed.process.calls


@pytest.mark.parametrize("data", [b'{"a":1,"a":2}', b"[]", b"bad", b"{}" * 2000])
def test_message_decoder_rejects_duplicate_malformed_and_oversize_input(data):
    with pytest.raises(OperatorLaunchError):
        decode_message(data, 2048)


def test_review_failure_clears_child_env_and_does_not_leak_native_error(armed):
    armed.process.fail_review = True
    result = armed.slot.handle(request(armed), peer_sid=SID)
    assert not result["ok"] and armed.process.calls[0]["env_ref"] == {}
    assert LOCAL not in json.dumps(result) and "synthetic-private-secret" not in json.dumps(result)


def select_window(armed, epoch):
    armed.slot = OperatorLaunchSlot(
        armed.executable,
        hashlib.sha256(armed.executable.read_bytes()).hexdigest(),
        armed.cwd,
        "http://127.0.0.1:9988/mcp",
        LOCAL,
        SID,
        None,
        broker_epoch=epoch,
        clock=lambda: armed.clock.now,
        process=armed.process,
    )


@pytest.mark.parametrize("epoch", [None, "", "wrong", "f" * 31])
def test_unselected_window_requires_valid_epoch_at_construction(armed, epoch):
    with pytest.raises(OperatorLaunchError, match="operator_launch_invalid_scope"):
        select_window(armed, epoch)
    assert not armed.process.calls


def test_explicit_one_capture_window_binds_first_review_once_and_cannot_retarget(armed):
    select_window(armed, armed.scope.broker_epoch)
    assert armed.slot.handle(request(armed), peer_sid=SID)["ok"]
    assert armed.slot.scope == armed.scope
    assert not armed.slot.handle(request(armed, "approve-and-start", job_id="other"), peer_sid=SID)[
        "ok"
    ]
    assert len(armed.process.calls) == 1 and armed.slot.closed


def test_unselected_window_is_still_bound_to_original_broker_epoch(armed):
    select_window(armed, armed.scope.broker_epoch)
    assert not armed.slot.handle(request(armed, broker_epoch="f" * 32), peer_sid=SID)["ok"]
    assert not armed.process.calls and armed.slot.scope is None


def test_other_user_cannot_select_the_first_tuple_or_skip_review(armed):
    select_window(armed, armed.scope.broker_epoch)
    assert not armed.slot.handle(request(armed), peer_sid="S-1-5-21-9-8-7-1002")["ok"]
    assert armed.slot.scope is None and not armed.process.calls
    assert not armed.slot.handle(request(armed, "approve-and-start"), peer_sid=SID)["ok"]
    assert armed.slot.scope is None and not armed.process.calls and armed.slot.closed


def test_explicit_close_keeps_uncertain_owned_process_until_reaped(armed):
    owner = SimpleNamespace(stop=lambda _: False, close=lambda: pytest.fail("live owner closed"))
    armed.process.owner = owner
    armed.process.active = armed.process.uncertain = True
    assert armed.slot.close() is False
    assert armed.process.owner is owner and armed.slot._local_token is None
    closed = threading.Event()
    owner.stop = lambda _: True
    owner.close = closed.set
    assert armed.slot.close() and closed.is_set()
