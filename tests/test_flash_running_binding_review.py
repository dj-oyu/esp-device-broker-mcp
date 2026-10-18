"""Independent running-job identity regressions using only the synthetic ROM driver."""

import pytest
from test_flash_policy import env as env


@pytest.mark.parametrize("mutation", ["job_state", "plan_hash", "active_job", "broker_epoch"])
async def test_running_flash_rechecks_job_identity_after_authentication_callback(env, mutation):
    prepared = env.prepare("running-identity-review", build_job_id="build-one")
    previous = env.store.firmware_current("fixture", "alice")
    before = env.driver.writes
    probed = False
    changed = False

    def driver_phase(phase):
        nonlocal probed
        if phase == "probe":
            probed = True

    def validator(*args):
        nonlocal changed
        if probed and not changed:
            changed = True
            if mutation == "job_state":
                env.store.set_job("running-identity-review", "cancelled", "review_invalidation")
            elif mutation == "plan_hash":
                env.store.db.execute(
                    "UPDATE jobs SET plan_hash=? WHERE id=?",
                    ("f" * 64, "running-identity-review"),
                )
                env.store.db.commit()
            elif mutation == "active_job":
                env.broker.active_job = "different-job"
            else:
                env.broker.epoch = "f" * 32
        return True

    # Activate the adversarial auth callback only after the synthetic probe;
    # admission and dispatch retain their normal, valid bindings.
    env.driver.callback = driver_phase
    env.broker.principal_valid = validator
    env.execute(prepared)
    await env.broker.queue.join()
    assert changed
    assert env.driver.writes == before
    assert env.driver.closed
    assert env.broker.job("running-identity-review")["state"] == "needs_review"
    assert env.store.firmware_current("fixture", "alice") == previous


@pytest.mark.parametrize(
    "mutation", ["job_state", "running_state", "plan_hash", "result", "active_job"]
)
async def test_final_authentication_callback_cannot_commit_detached_flash_result(env, mutation):
    prepared = env.prepare("final-identity-review", build_job_id="build-one")
    previous = env.store.firmware_current("fixture", "alice")
    before = env.driver.writes
    original = env.store.finish_firmware
    calls = 0
    final_guard = False
    changed = False

    def validator(*args):
        nonlocal changed
        if final_guard and not changed:
            changed = True
            assert env.store.db.in_transaction
            assert env.store.job("final-identity-review")["state"] == "succeeded"
            if mutation == "job_state":
                env.store.db.execute(
                    "UPDATE jobs SET state='cancelled' WHERE id=?", ("final-identity-review",)
                )
            elif mutation == "running_state":
                env.store.db.execute(
                    "UPDATE jobs SET state='running' WHERE id=?", ("final-identity-review",)
                )
            elif mutation == "plan_hash":
                env.store.db.execute(
                    "UPDATE jobs SET plan_hash=? WHERE id=?",
                    ("f" * 64, "final-identity-review"),
                )
            elif mutation == "result":
                env.store.db.execute(
                    "UPDATE jobs SET result='changed-result' WHERE id=?", ("final-identity-review",)
                )
            else:
                env.broker.active_job = "different-job"
        return True

    def finish(job_id, result, promotion, guard, *args):
        def guarded():
            nonlocal calls, final_guard
            calls += 1
            final_guard = calls == 2
            return guard()

        return original(job_id, result, promotion, guarded, *args)

    env.store.finish_firmware = finish
    env.broker.principal_valid = validator
    env.execute(prepared)
    await env.broker.queue.join()
    assert calls == 2 and changed
    # The bytes were verified before the final transaction. A changed job must
    # still prevent success, summary publication and LKG selection atomically.
    assert env.driver.writes == before + 1
    assert env.driver.closed
    assert env.broker.job("final-identity-review")["state"] == "needs_review"
    assert env.broker.job("final-identity-review")["plan_hash"] == prepared["plan_hash"]
    assert env.store.firmware_current("fixture", "alice") == previous
    assert (
        env.store.db.execute(
            "SELECT count(*) FROM firmware_job_summaries WHERE job_id=?", ("final-identity-review",)
        ).fetchone()[0]
        == 0
    )
