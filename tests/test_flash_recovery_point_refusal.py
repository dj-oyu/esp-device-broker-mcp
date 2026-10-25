"""A recovery-point refusal says the device's flash_baseline and one fixed next step.

The error code stays the same; the public gate adds only `flash_baseline`,
`lkg_recorded`, a fixed `next_step` id and its fixed `explanation`. No job id,
principal or path of anyone else ever appears. Real broker/store, synthetic ROM.
"""

import json

import pytest
import test_flash_policy

from cardputer_mcp.broker import BrokerError, RecoveryPointRefusal
from cardputer_mcp.models import AcquireRequest, LeaseRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.public_errors import to_public
from cardputer_mcp.public_reasons import RECOVERY_POINT_NEXT_STEPS
from cardputer_mcp.server import _result

env = test_flash_policy.env


def _forget_lkg(env):
    env.store.db.execute("DELETE FROM firmware_lkg")
    env.store.db.commit()


def _body(env, job_id, action, session=None, **references):
    def call():
        return env.broker.prepare(
            PrepareRequest(
                session=session or env.broker.session.capability,
                job_id=job_id,
                test=action,
                device_id="fixture",
                **references,
            )
        )

    return _result(call)


def _expect(body, code, baseline, lkg, step):
    assert body == {
        "ok": False,
        "error": code,
        "flash_baseline": baseline,
        "lkg_recorded": lkg,
        "next_step": step,
        "explanation": RECOVERY_POINT_NEXT_STEPS[step],
    }


@pytest.mark.parametrize(
    "changes,baseline,step",
    [
        ({}, "not_required", "run_firmware_backup_then_pass_backup_job_id"),
        ({"allow_backup": False}, "not_required", "backup_not_enabled_in_local_registration"),
        (
            {"allow_flash": False},
            "flash_not_enabled",
            "run_firmware_backup_then_pass_backup_job_id",
        ),
    ],
)
async def test_restore_without_any_lkg_names_the_baseline_and_next_step(
    env, changes, baseline, step
):
    env.configure(**changes)
    _forget_lkg(env)
    body = _body(env, "restore-none", Action.RESTORE)
    _expect(body, "verified_lkg_or_initial_backup_required", baseline, False, step)


@pytest.mark.parametrize("action", [Action.FLASH, Action.RESTORE])
async def test_unknown_lkg_receipt_points_at_status_recovery(env, action):
    env.configure()
    references = {"lkg_receipt": "0" * 64}
    if action == Action.FLASH:
        references["build_job_id"] = "build-one"
    body = _body(env, "unknown-receipt", action, **references)
    _expect(
        body,
        "verified_lkg_or_initial_backup_required",
        "not_required",
        True,
        "choose_lkg_receipt_from_status_recovery",
    )


async def test_another_principals_backup_is_refused_without_naming_it(env):
    env.configure()
    broker = env.broker
    broker.release(LeaseRequest(session=broker.session.capability))
    bob = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="bob")["session"]
    body = _body(env, "bob-restore", Action.RESTORE, session=bob, backup_job_id="backup-one")
    _expect(
        body,
        "successful_owned_backup_required",
        "not_required",
        True,
        "name_own_succeeded_backup_of_this_device",
    )
    text = json.dumps(body)
    assert "backup-one" not in text and "alice" not in text and "bob" not in text


@pytest.mark.parametrize("allow_backup", [True, False])
async def test_a_non_backup_job_named_as_backup(env, allow_backup):
    env.configure(allow_backup=allow_backup)
    body = _body(
        env, "flash-named", Action.FLASH, build_job_id="build-one", backup_job_id="build-one"
    )
    step = (
        "name_own_succeeded_backup_of_this_device"
        if allow_backup
        else "backup_not_enabled_in_local_registration"
    )
    _expect(body, "successful_owned_backup_required", "not_required", True, step)
    assert "build-one" not in json.dumps(body)


async def test_other_flash_refusals_keep_the_bare_code(env):
    env.configure(allow_restore=False)
    assert _body(env, "off", Action.RESTORE) == {
        "ok": False,
        "error": "registered_flash_operation_not_enabled",
    }


@pytest.mark.parametrize(
    "code,details",
    [
        ("verified_lkg_or_initial_backup_required", None),
        ("verified_lkg_or_initial_backup_required", {"flash_baseline": "not_required"}),
        (
            "verified_lkg_or_initial_backup_required",
            {"flash_baseline": "C:/private", "lkg_recorded": False, "next_step": "x"},
        ),
        (
            "verified_lkg_or_initial_backup_required",
            {
                "flash_baseline": "not_required",
                "lkg_recorded": 1,
                "next_step": "run_firmware_backup_then_pass_backup_job_id",
            },
        ),
        (
            "verified_lkg_or_initial_backup_required",
            {
                "flash_baseline": "not_required",
                "lkg_recorded": False,
                "next_step": "run_firmware_backup_then_pass_backup_job_id",
                "job_id": "someone-elses",
            },
        ),
        (
            "registered_flash_operation_not_enabled",
            {
                "flash_baseline": "not_required",
                "lkg_recorded": False,
                "next_step": "run_firmware_backup_then_pass_backup_job_id",
            },
        ),
    ],
)
def test_gate_drops_details_outside_the_fixed_vocabulary(code, details):
    assert to_public(RecoveryPointRefusal(code, details)) == {"error": code}


def test_gate_keeps_unreviewed_codes_closed():
    assert to_public(RecoveryPointRefusal("C:/private/path", None)) == {"error": "invalid_request"}
    assert to_public(BrokerError("verified_lkg_or_initial_backup_required")) == {
        "error": "verified_lkg_or_initial_backup_required"
    }
