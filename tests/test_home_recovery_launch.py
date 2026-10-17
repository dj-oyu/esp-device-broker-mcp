"""The new local recovery pipe cannot reuse capture/DERBY scopes or arbitrary actions."""

import hashlib
import json

import pytest
from test_home_recovery import request, running
from test_operator_launch import LOCAL, SID

from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.home_recovery import ATTESTATION
from cardputer_mcp.home_recovery_launch import HomeRecoveryLaunchScope, HomeRecoveryLaunchSlot


class Process(BuildProcess):
    def __init__(self, review):
        super().__init__()
        self.review = review
        self.calls = []
        self.fail = False

    def run(self, args, env, cwd, emit, **options):
        options["monitor"]()
        self.calls.append((list(args), dict(env)))
        if self.fail:
            raise TimeoutError("synthetic private error")
        output = (
            self.review
            if args[5] == "home-recovery-review"
            else {
                "ok": True,
                "approved": True,
                "job_id": self.review["plan"]["job_id"],
                "state": "running",
            }
        )
        emit(json.dumps(output), "stdout")
        return 0


def arm(e, p, tmp_path):
    plan = p["plan"]
    scope = HomeRecoveryLaunchScope(
        plan_hash=p["plan_hash"],
        **{k: plan[k] for k in HomeRecoveryLaunchScope.model_fields if k != "plan_hash"},
    )
    exe = tmp_path / "synthetic.exe"
    exe.write_bytes(b"test executable never launched")
    process = Process({"ok": True, **e.m.review(p["plan_hash"])})
    slot = HomeRecoveryLaunchSlot(
        exe,
        hashlib.sha256(exe.read_bytes()).hexdigest(),
        tmp_path,
        "http://127.0.0.1:9998/mcp",
        LOCAL,
        SID,
        scope=scope,
        clock=e.clock,
        process=process,
    )
    return slot, process, scope


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", "another"),
        ("source_job_id", "another-source"),
        ("principal_id", "another-owner"),
        ("broker_epoch", "9" * 32),
        ("device_fingerprint", "9" * 64),
        ("expected_firmware_sha256", "9" * 64),
        ("plan_hash", "9" * 64),
        ("action", "plan-review"),
        ("keys", "Esc"),
        ("allow_image_delivery", True),
    ],
)
async def test_recovery_window_rejects_scope_change_without_cli_child(tmp_path, field, value):
    async with running(tmp_path) as e:
        slot, process, scope = arm(e, request(e), tmp_path)
        payload = {**scope.model_dump(), "action": "home-recovery-review", field: value}
        assert not slot.handle(payload, peer_sid=SID)["ok"]
        assert slot.closed and slot._local_token is None and not process.calls


@pytest.mark.parametrize("change", [None, "timeout", "wrong_attestation", "expired"])
async def test_one_recovery_review_approval_and_unknown_reply_are_never_rearmed(tmp_path, change):
    async with running(tmp_path) as e:
        slot, process, scope = arm(e, request(e), tmp_path)
        r = slot.handle({**scope.model_dump(), "action": "home-recovery-review"}, peer_sid=SID)
        assert r["ok"]
        if change == "timeout":
            process.fail = True
        elif change == "expired":
            e.clock.now += 30
        payload = {
            **scope.model_dump(),
            "action": "home-recovery-approve",
            "review_revision": r["review"]["review_revision"],
            "attestation": ATTESTATION,
        }
        if change == "wrong_attestation":
            payload["attestation"] = "DEVICE HOME AND FIRMWARE VERIFIED"
        result = slot.handle(payload, peer_sid=SID)
        assert result["ok"] is (change is None)
        assert slot.closed and slot._local_token is None
        count = len(process.calls)
        assert not slot.handle(payload, peer_sid=SID)["ok"] and len(process.calls) == count
        assert all(
            "--allow-image-delivery" not in args and "capture_home" not in args
            for args, _ in process.calls
        )
