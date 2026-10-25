"""Characterization (before stage 3a): `fixed_trial_persist_failed` on both finish paths.

A fixed DERBY trial is persisted by ``Store.finish_fixed_trial`` either from the failure
branch (a typed ``SerialFailure`` that carries a trial) or from the success commit. When
that write raises, the worker reviews the job with `fixed_trial_persist_failed` and sets
the runtime latch. Synthetic serial transport; never opens a USB port.
"""

from test_derby_fixed_workload import FixedWire, backend_for
from test_serial_http import serial_service as serial_service

from cardputer_mcp.client import call, connect

SECRET = "SYNTHETIC_PRIVATE_EXCEPTION_TEXT"


async def run_fixed(url, resident, job_id):
    async with connect(url, "a" * 64) as owner:
        lease = await call(
            owner, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 300}
        )
        prepared = await call(
            owner,
            "submit",
            {"session": lease["session"], "job_id": job_id, "kind": "derby_fixed"},
        )
        executed = await call(
            owner,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        assert executed["ok"], executed
        await resident.queue.join()
    return resident._store().job(job_id)


def refuse_trial_writes(resident):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args[3] if len(args) > 3 else kwargs.get("state"))
        raise RuntimeError(SECRET)

    resident._store().finish_fixed_trial = fail
    return calls


async def test_success_commit_persist_failure_is_reviewed_and_latched(serial_service):
    url, resident, _ = serial_service
    resident.backend = backend_for(FixedWire(), resident.settings.serial)
    calls = refuse_trial_writes(resident)
    job = await run_fixed(url, resident, "fixed-commit")
    assert calls == ["succeeded"]
    assert (job["state"], job["result"]) == ("needs_review", "fixed_trial_persist_failed")
    assert SECRET not in repr(job)
    assert resident.execution_uncertain is True
    assert resident.execution_uncertain_reason == "job_final_commit_failed"
    assert resident.active_job is None


async def test_failure_branch_persist_failure_is_reviewed_and_latched(serial_service):
    url, resident, _ = serial_service
    relaunch = b"APP_ID local.derby\nDERBY READY tones=true\n"
    resident.backend = backend_for(FixedWire(post_home=relaunch), resident.settings.serial)
    calls = refuse_trial_writes(resident)
    job = await run_fixed(url, resident, "fixed-failure")
    # Without the refusal this trial commits as `failed` (test_derby_fixed_workload).
    assert calls == ["failed"]
    assert (job["state"], job["result"]) == ("needs_review", "fixed_trial_persist_failed")
    assert SECRET not in repr(job)
    assert resident.execution_uncertain is True
    assert resident.execution_uncertain_reason == "fixed_trial_persist_failed"
    assert resident.active_job is None
