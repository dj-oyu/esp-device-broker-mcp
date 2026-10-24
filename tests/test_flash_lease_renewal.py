"""Renewal of running unattended flash, with synthetic ROM and a fake clock only."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_flash_policy import env as env

from cardputer_mcp.broker import BrokerError, WorkSession
from cardputer_mcp.models import CancelRequest, RenewRequest


def setup(env, *, work=False):
    broker = env.broker
    clock = SimpleNamespace(now=0.0)
    broker.clock = lambda: clock.now
    broker.session.deadline = 30.0
    if work:
        session = broker.session
        session.work_session_id = "work-one"
        broker.work_sessions["work-one"] = WorkSession(
            "synthetic-resume",
            "work-one",
            session.owner,
            "alice",
            frozenset({"read", "control"}),
            100.0,
        )
    env.configure()
    return clock


def renew(env, clock):
    clock.now = 20.0
    env.broker.renew(RenewRequest(session=env.broker.session.capability, lease_seconds=30))
    assert env.broker.session.deadline == 50.0


async def test_running_renewal_crosses_original_expiry_and_both_atomic_guards(env):
    clock = setup(env, work=True)
    prepared = env.prepare("renewed", build_job_id="build-one")
    original_execute = env.broker.firmware_flash.execute
    original_finish = env.store.finish_firmware
    guards = []

    async def execute(plan, stage, live):
        assert env.store.job("renewed")["state"] == "running"
        renew(env, clock)
        return await original_execute(plan, stage, live)

    def callback(phase):
        if phase == "write":
            clock.now = 30.0

    def finish(job_id, result, promotion, guard, *args):
        def guarded():
            clock.now += 1
            allowed = guard()
            guards.append((env.store.job(job_id)["state"], clock.now, allowed))
            return allowed

        return original_finish(job_id, result, promotion, guarded, *args)

    env.broker.firmware_flash.execute = execute
    env.driver.callback = callback
    env.store.finish_firmware = finish
    env.execute(prepared)
    _token = env.broker.session
    await env.broker.queue.join()
    assert guards == [("running", 31.0, True), ("succeeded", 32.0, True)]
    assert env.broker.job("renewed")["state"] == "succeeded"
    assert env.store.firmware_current("fixture")["completed_job"] == "renewed"
    assert env.driver.closed
    # Renewing ownership after completion cannot revive the old authorization.
    env.broker.renew(RenewRequest(session=env.broker.session.capability, lease_seconds=30))


@pytest.mark.parametrize("renewed", [False, True])
async def test_running_flash_expires_at_current_deadline_and_cannot_be_revived(env, renewed):
    clock = setup(env)
    prepared = env.prepare("expired", build_job_id="build-one")
    original = env.broker.firmware_flash.execute
    previous = env.store.firmware_current("fixture")

    async def execute(plan, stage, live):
        if renewed:
            renew(env, clock)
        return await original(plan, stage, live)

    def callback(phase):
        if phase == "write":
            clock.now = 50.0 if renewed else 30.0

    env.broker.firmware_flash.execute = execute
    env.driver.callback = callback
    env.execute(prepared)
    token = env.broker.session
    await env.broker.queue.join()
    assert env.broker.job("expired")["state"] == "needs_review"
    assert env.store.firmware_current("fixture") == previous
    with pytest.raises(BrokerError, match="invalid_session|lease_expired"):
        env.broker.renew(RenewRequest(session=token.capability, lease_seconds=300))


# Representatives of each authority family at the final atomic guard (where the
# uncommitted success row exists), plus one at the first guard.
_CASES = [
    ("lease", 2),
    ("revoked", 2),
    ("registration", 2),
    ("work_scopes", 2),
    ("work_nonfinite", 2),
    ("lease", 1),
]


@pytest.mark.parametrize(("change", "guard_number"), _CASES)
async def test_renewal_preserves_authority_at_both_commit_guards(env, guard_number, change):
    clock = setup(env, work=True)
    prepared = env.prepare("changed", build_job_id="build-one")
    original_execute = env.broker.firmware_flash.execute
    original_finish = env.store.finish_firmware
    previous = env.store.firmware_current("fixture")
    calls = 0

    async def execute(plan, stage, live):
        renew(env, clock)
        return await original_execute(plan, stage, live)

    def mutate():
        broker = env.broker
        work = broker.work_sessions["work-one"]
        if change == "lease":
            broker.session = replace(broker.session)  # Even the same lease ID is rejected.
        elif change == "principal":
            broker.session.principal_id = "bob"
        elif change == "owner":
            broker.session.owner = "other"
        elif change == "epoch":
            broker.epoch = "other"
        elif change == "registration":
            env.configure(allow_flash=False)
        elif change == "revoked":
            broker.principal_valid = lambda *args: False
        elif change == "work_expired":
            work.deadline = clock.now
        elif change == "work_replaced":
            broker.work_sessions[work.id] = replace(work)
        elif change == "work_scopes":
            work.scopes = frozenset({"read"})
        elif change == "work_closed":
            work.state = "closed"
        elif change == "lease_nonfinite":
            broker.session.deadline = float("inf")
        else:
            work.deadline = float("inf")

    def finish(job_id, result, promotion, guard, *args):
        def guarded():
            nonlocal calls
            calls += 1
            clock.now = 31.0
            if calls == guard_number:
                mutate()
            return guard()

        return original_finish(job_id, result, promotion, guarded, *args)

    env.broker.firmware_flash.execute = execute
    env.store.finish_firmware = finish
    env.execute(prepared)
    await env.broker.queue.join()
    assert calls == guard_number
    assert env.broker.job("changed")["state"] == "needs_review"
    assert env.store.firmware_current("fixture") == previous


async def test_queued_unattended_authorization_keeps_original_deadline(env):
    clock = setup(env)
    prepared = env.prepare("queued-expired", build_job_id="build-one")
    before = env.driver.writes
    env.execute(prepared)
    _token = env.broker.session
    renew(env, clock)
    clock.now = 30.0
    await env.broker.queue.join()
    assert env.broker.job("queued-expired")["state"] == "cancelled"
    assert env.driver.writes == before
    env.broker.renew(RenewRequest(session=env.broker.session.capability, lease_seconds=300))


@pytest.mark.parametrize("guard_number", [1, 2])
async def test_renewal_cannot_revive_cancelled_running_flash(env, guard_number):
    clock = setup(env)
    prepared = env.prepare("cancel-renew", build_job_id="build-one")
    original = env.store.finish_firmware
    previous = env.store.firmware_current("fixture")
    calls = 0

    def finish(job_id, result, promotion, guard, *args):
        def guarded():
            nonlocal calls
            calls += 1
            if calls == guard_number:
                # At the second guard the uncommitted row already says succeeded;
                # cancellation is latched in the engine and must still win.
                assert env.broker.firmware_flash.request_stop(job_id)
                renew(env, clock)
                clock.now = 31.0
            return guard()

        return original(job_id, result, promotion, guarded, *args)

    env.store.finish_firmware = finish
    env.execute(prepared)
    _token = env.broker.session
    await env.broker.queue.join()
    assert calls == guard_number
    assert env.broker.job("cancel-renew")["state"] == "needs_review"
    assert env.store.firmware_current("fixture") == previous


async def test_renewal_cannot_revive_explicitly_cancelled_queued_flash(env):
    clock = setup(env)
    prepared = env.prepare("cancel-queued", build_job_id="build-one")
    before = env.driver.writes
    env.execute(prepared)
    token = env.broker.session
    env.broker.cancel(CancelRequest(session=token.capability, job_id="cancel-queued"))
    renew(env, clock)
    clock.now = 31.0
    await env.broker.queue.join()
    assert env.broker.job("cancel-queued")["state"] == "cancelled"
    assert env.driver.writes == before


@pytest.mark.parametrize("change", ["lease", "registration", "work"])
async def test_reentrant_renewal_cannot_bypass_binding_after_auth_callback(env, change):
    clock = setup(env, work=True)
    prepared = env.prepare("reentrant-renew", build_job_id="build-one")
    original = env.broker.firmware_flash.execute
    previous = env.store.firmware_current("fixture")
    changed = False

    def current(*args):
        nonlocal changed
        if env.driver.active and not changed:
            changed = True  # Prevent recursion through renewal's maintenance check.
            renew(env, clock)
            if change == "lease":
                env.broker.session = replace(env.broker.session)
            elif change == "registration":
                env.configure(allow_flash=False)
            else:
                env.broker.work_sessions["work-one"].state = "closed"
        return True

    async def execute(plan, stage, live):
        env.broker.principal_valid = current
        return await original(plan, stage, live)

    before = env.driver.writes
    env.broker.firmware_flash.execute = execute
    env.execute(prepared)
    await env.broker.queue.join()
    assert changed
    assert env.broker.job("reentrant-renew")["state"] == "needs_review"
    assert env.driver.writes == before
    assert env.store.firmware_current("fixture") == previous
