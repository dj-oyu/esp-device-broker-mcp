"""Characterization (stage 0.5): the ordered admission checks inside ``Broker._work``.

Each row pins the reason code one check writes today, so a later split of ``_work`` into
admission/executor/finish keeps the same order and codes. State is changed either before
the worker takes the job, or right after the worker's own maintenance tick (step 5 in
``docs/redesign/contracts/jobs.md`` section 5), so the later checks are reachable.
Mock backend only; no hardware.
"""

import dataclasses
import inspect
import json

import pytest
from test_pending_execution import prepare, running, submit

import cardputer_mcp.broker as broker_module
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.continuation import Deferred, Tickets
from cardputer_mcp.job_ledger import JobLedger
from cardputer_mcp.lease_manager import LeaseManager
from cardputer_mcp.models import LogRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.store import Store

JOB = "submitted"


def after_admission_tick(broker, mutate):
    """Run ``mutate`` once, right after the worker's in-admission ``_expire()`` tick."""
    original = broker._expire
    fired: list[bool] = []

    def wrapped():
        original()
        if not fired:
            fired.append(True)
            mutate()

    broker._expire = wrapped
    return fired


def rewrite_plan(broker, digest, **changes):
    store = broker._store()
    payload = store.plan(digest)
    payload.update(changes)
    store.db.execute(
        "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(payload, sort_keys=True), digest)
    )
    store.db.commit()


def other_lease(broker):
    return dataclasses.replace(broker.session, id="replaced-lease")


# --- the table ---------------------------------------------------------------------------
# (case id, mutate(broker, clock, digest, monkeypatch), expected state, expected result)


def _session_none(b, c, d, mp):
    b.session = None


def _session_replaced(b, c, d, mp):
    b.session = other_lease(b)


def _lease_deadline(b, c, d, mp):
    c.now = b.session.deadline


def _closing(b, c, d, mp):
    b.closing = True


def _plan_missing(b, c, d, mp):
    b._store().db.execute("DELETE FROM plans WHERE hash=?", (d,))
    b._store().db.commit()


def _plan_other_session(b, c, d, mp):
    rewrite_plan(b, d, session_id="another-lease")


def _log_delivery_missing(b, c, d, mp):
    # A firmware/restart plan must name the current lease as its log recipient.
    rewrite_plan(b, d, test=Action.FIRMWARE.value)


def _callback_false(b, c, d, mp):
    b.principal_valid = lambda *args: False


def _callback_raises(b, c, d, mp):
    def fail(*args):
        raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")

    b.principal_valid = fail


def _callback_replaces_lease(b, c, d, mp):
    def swap(*args):
        b.session = other_lease(b)
        return True

    b.principal_valid = swap


def _callback_expires_lease(b, c, d, mp):
    def expire(*args):
        c.now = b.session.deadline
        return True

    b.principal_valid = expire


def _flash_binding_invalid(b, c, d, mp):
    b._flash_bindings[JOB] = object()
    mp.setattr(broker_module, "validate_binding", lambda *args: False)


def _collector_raises(b, c, d, mp):
    rewrite_plan(b, d, flash_job_id="synthetic-flash")

    def fail(*args):
        raise ValueError("synthetic")

    mp.setattr(broker_module, "prepare_collector", fail)


def _collector_differs(b, c, d, mp):
    rewrite_plan(b, d, flash_job_id="synthetic-flash")
    mp.setattr(broker_module, "prepare_collector", lambda *args: {"device_fingerprint": "x"})


def _second_callback(answer_second):
    def mutate(b, c, d, mp):
        calls = []

        def valid(*args):
            calls.append(args)
            if len(calls) == 1:
                return True
            return answer_second(b)

        b.principal_valid = valid

    return mutate


def _swap_validator(b):
    b.principal_valid = lambda *args: True
    return True


def _runtime_binding_changed(b, c, d, mp):
    b._execution_bindings[JOB] = "changed"


def _uncertain(b, c, d, mp):
    b.execution_uncertain = True


def _busy(b, c, d, mp):
    b.active_job = "someone-else"


def _intent_removed(b, c, d, mp):
    b._execution_intents.pop(JOB)


def _plan_digest_changed(b, c, d, mp):
    rewrite_plan(b, d, extra_field=1)


ADMISSION = [
    # step 7: lease snapshot taken before the tick
    ("session_none", _session_none, "cancelled", "session_not_available"),
    ("session_replaced", _session_replaced, "cancelled", "session_not_available"),
    ("lease_deadline", _lease_deadline, "cancelled", "session_not_available"),
    ("closing", _closing, "cancelled", "session_not_available"),
    # step 8: no uncertainty latch, but the whole device lane is cancelled
    ("plan_missing", _plan_missing, "needs_review", "plan_not_available"),
    ("plan_other_session", _plan_other_session, "needs_review", "plan_not_available"),
    # step 9: log_delivery binding of firmware/restart plans
    (
        "log_delivery",
        _log_delivery_missing,
        "cancelled",
        "principal_or_lease_no_longer_valid",
    ),
    # step 10: first principal callback, then lease identity and deadline
    ("callback_false", _callback_false, "cancelled", "principal_or_lease_no_longer_valid"),
    ("callback_raises", _callback_raises, "cancelled", "principal_or_lease_no_longer_valid"),
    (
        "callback_replaces_lease",
        _callback_replaces_lease,
        "cancelled",
        "principal_or_lease_no_longer_valid",
    ),
    (
        "callback_expires_lease",
        _callback_expires_lease,
        "cancelled",
        "principal_or_lease_no_longer_valid",
    ),
    # step 11 / 12: flash binding and fixed collector re-derivation
    ("flash_binding", _flash_binding_invalid, "cancelled", "flash_binding_invalid"),
    ("collector_raises", _collector_raises, "cancelled", "collector_binding_changed"),
    ("collector_differs", _collector_differs, "cancelled", "collector_binding_changed"),
    # step 13 / 14: second callback and the callback-free composite
    (
        "second_callback_false",
        _second_callback(lambda b: False),
        "cancelled",
        "execution_intent_not_available",
    ),
    (
        "second_callback_swaps_validator",
        _second_callback(_swap_validator),
        "cancelled",
        "execution_intent_not_available",
    ),
    (
        "runtime_binding",
        _runtime_binding_changed,
        "cancelled",
        "execution_intent_not_available",
    ),
    ("uncertain", _uncertain, "cancelled", "execution_intent_not_available"),
    ("busy", _busy, "cancelled", "execution_intent_not_available"),
    ("intent_removed", _intent_removed, "cancelled", "execution_intent_not_available"),
    ("plan_digest", _plan_digest_changed, "cancelled", "execution_intent_not_available"),
]


@pytest.mark.parametrize(
    ("mutate", "state", "result"),
    [pytest.param(m, s, r, id=i) for i, m, s, r in ADMISSION],
)
async def test_each_admission_check_writes_its_reason_code(
    tmp_path, monkeypatch, mutate, state, result
):
    async with running(tmp_path) as (broker, clock):
        digest = prepare(broker, JOB)
        submit(broker, digest)
        fired = after_admission_tick(broker, lambda: mutate(broker, clock, digest, monkeypatch))
        await broker.queue.join()
        broker.active_job = None  # the "busy" row pins a foreign job; let close() run
        job = broker._store().job(JOB)
        assert fired == [True]
        assert (job["state"], job["result"]) == (state, result)
        assert broker.backend.calls == 0
        assert JOB not in broker._execution_intents
        # Only the outer exception handler sets the runtime latch; no admission check does.
        assert broker.execution_uncertain is (mutate is _uncertain)


EARLY = [
    # step 2: the row is no longer queued -> skipped silently, reason kept
    ("not_queued", lambda b: b._store().set_job(JOB, "cancelled", "synthetic_reason")),
    # step 3: no process-local intent -> cancelled before the admission tick runs
    ("no_intent", lambda b: b._execution_intents.pop(JOB)),
]


@pytest.mark.parametrize("mutate", [pytest.param(m, id=i) for i, m in EARLY])
async def test_checks_before_the_admission_tick(tmp_path, mutate):
    async with running(tmp_path) as (broker, _):
        submit(broker, prepare(broker, JOB))
        mutate(broker)
        fired = after_admission_tick(broker, lambda: None)
        await broker.queue.join()
        job = broker._store().job(JOB)
        assert fired == []  # neither path reaches the in-admission tick
        expected = "synthetic_reason" if mutate is EARLY[0][1] else "execution_intent_not_available"
        assert (job["state"], job["result"]) == ("cancelled", expected)
        assert broker.backend.calls == 0 and broker.execution_uncertain is False


async def test_not_queued_after_the_admission_tick_is_skipped_silently(tmp_path):
    async with running(tmp_path) as (broker, _):
        submit(broker, prepare(broker, JOB))
        after_admission_tick(
            broker, lambda: broker._store().set_job(JOB, "cancelled", "synthetic_reason")
        )
        await broker.queue.join()
        job = broker._store().job(JOB)
        assert (job["state"], job["result"]) == ("cancelled", "synthetic_reason")
        assert broker.backend.calls == 0


ORDER = [
    # (earlier check, later check) -> the earlier check's code wins
    ("session_before_plan", (_session_none, _plan_missing), "session_not_available"),
    ("plan_before_log_delivery", (_plan_missing, _callback_false), "plan_not_available"),
    (
        "log_delivery_before_flash",
        (_log_delivery_missing, _flash_binding_invalid),
        "principal_or_lease_no_longer_valid",
    ),
    (
        "callback_before_collector",
        (_callback_false, _collector_raises),
        "principal_or_lease_no_longer_valid",
    ),
    (
        "flash_before_collector",
        (_flash_binding_invalid, _collector_raises),
        "flash_binding_invalid",
    ),
    ("collector_before_composite", (_collector_raises, _busy), "collector_binding_changed"),
]


@pytest.mark.parametrize(("mutations", "result"), [pytest.param(m, r, id=i) for i, m, r in ORDER])
async def test_admission_order_earlier_check_wins(tmp_path, monkeypatch, mutations, result):
    async with running(tmp_path) as (broker, clock):
        digest = prepare(broker, JOB)
        submit(broker, digest)

        def both():
            for mutate in mutations:
                mutate(broker, clock, digest, monkeypatch)

        after_admission_tick(broker, both)
        await broker.queue.join()
        broker.active_job = None
        assert broker._store().job(JOB)["result"] == result
        assert broker.backend.calls == 0


async def test_plan_not_available_cancels_the_rest_of_the_device_lane(tmp_path):
    async with running(tmp_path) as (broker, clock):
        first = prepare(broker, JOB)
        submit(broker, first)
        submit(broker, prepare(broker, "second"))
        after_admission_tick(broker, lambda: _plan_missing(broker, clock, first, None))
        await broker.queue.join()
        store = broker._store()
        assert store.job(JOB)["state"] == "needs_review"
        second = store.job("second")
        assert (second["state"], second["result"]) == ("cancelled", "recovery_review_required")
        assert broker.backend.calls == 0
        assert broker.execution_uncertain is False  # history, not the runtime latch


async def test_stale_principal_write_keeps_a_concurrent_cancellation_reason(tmp_path):
    """jobs.md 1.4(2): the callback committed a terminal reason before the stale write.

    Stage 2b (DESIGN-REDESIGN 2.1, JobLedger): the first committed terminal reason is
    kept. The late queued->cancelled write is refused in SQL and recorded, instead of
    overwriting the reason with `principal_or_lease_no_longer_valid` as before.
    """
    async with running(tmp_path) as (broker, clock):
        submit(broker, prepare(broker, JOB))

        def mutate():
            def valid(*args):
                broker._store().set_job(JOB, "cancelled", "client_cancelled_queued")
                return False

            broker.principal_valid = valid

        after_admission_tick(broker, mutate)
        await broker.queue.join()
        job = broker._store().job(JOB)
        assert (job["state"], job["result"]) == ("cancelled", "client_cancelled_queued")
        assert broker._store().ledger.refusals == {("cancelled", "cancelled"): 1}
        events = [entry["event"] for entry in broker.read_logs(LogRequest())["entries"]]
        assert "job_transition_refused" in events


async def test_a_failure_after_a_committed_success_cannot_overwrite_it(tmp_path):
    """DESIGN-REDESIGN 2.1(3): the finish except branch used to rewrite a committed
    success as needs_review. Stage 2b keeps the success, refuses and records the late
    write, and still sets the runtime latch."""
    async with running(tmp_path) as (broker, _):
        ledger = broker._store().ledger
        original = ledger.transition

        def commit_then_fail(job_id, to, *args, **kwargs):
            written = original(job_id, to, *args, **kwargs)
            if to == "succeeded":
                raise RuntimeError("synthetic failure after the commit")
            return written

        ledger.transition = commit_then_fail
        submit(broker, prepare(broker, JOB))
        await broker.queue.join()
        assert broker._store().job(JOB)["state"] == "succeeded"
        assert ledger.refusals == {("succeeded", "needs_review"): 1}
        assert broker.execution_uncertain is True


async def test_job_not_found_sets_the_execution_uncertain_latch(tmp_path):
    async with running(tmp_path) as (broker, _):
        broker.queue.put_nowait("ghost-job")
        await broker.queue.join()
        assert broker._store().job("ghost-job") is None
        assert broker.execution_uncertain is True
        assert broker.status()["execution_uncertain"] is True
        # The latch then blocks new admission until teardown.
        with pytest.raises(BrokerError, match="^recovery_review_required$"):
            prepare(broker, "after-ghost")
        # The worker survived the failure and still serves the queue.
        assert broker.worker is not None and not broker.worker.done()


# --- structural guard for "no await between check and write" -----------------------------

SYNCHRONOUS = [
    (Broker, "maintenance_tick"),
    (Broker, "_expire"),
    (Broker, "_tickets_tick"),
    (Broker, "_release_hold"),
    (Broker, "_deferred_tick"),
    (Broker, "_advance_device_deferred"),
    (Broker, "_grant_job_lease"),
    (Broker, "_release_finished_job_lease"),
    (Broker, "_end_ownership"),
    (Broker, "_end_wait"),
    (Broker, "_cancel_queued"),
    (Broker, "_cancel_execution_intent"),
    (Broker, "_prune_execution_intents"),
    (Broker, "_execution_intent_live"),
    (Broker, "_running_job_binding_live"),
    (Broker, "_physical_busy"),
    (Broker, "_execution_uncertain"),
    (Broker, "acquire"),
    (Broker, "release"),
    (Broker, "prepare"),
    (Broker, "execute"),
    (Broker, "prepare_deferred"),
    (Broker, "execute_deferred"),
    (Broker, "request_device"),
    (Broker, "close_work_session"),
    (Broker, "operator_release_idle"),
    (Broker, "cancel"),
    (Broker, "cancel_job_only"),
    (Broker, "cancel_deferred"),
    (Broker, "cancel_ticket"),
    (Broker, "issue_ticket"),
    (Store, "set_job"),
    (JobLedger, "transition"),
    (JobLedger, "annotate"),
    (JobLedger, "mark_reviewed"),
    (Store, "enqueue_requested"),
    (Store, "finish_firmware"),
    (Store, "finish_fixed_trial"),
    (Store, "consume_home_attempt"),
    (Store, "finish_home_recovery"),
    (Store, "recover"),
    (Tickets, "issue"),
    (Tickets, "peek"),
    (Tickets, "consume"),
    (Tickets, "note_consumer"),
    (Tickets, "mark_state"),
    (Tickets, "set_fence"),
    (Tickets, "set_hold"),
    (Deferred, "add"),
    (Deferred, "finish"),
    # Stage 2c: the device lease has one writer.
    (Broker, "renew"),
    (LeaseManager, "grant"),
    (LeaseManager, "end"),
    (LeaseManager, "shorten"),
    (LeaseManager, "renew"),
    (LeaseManager, "overwrite"),
]


@pytest.mark.parametrize(
    ("owner", "name"), [pytest.param(o, n, id=f"{o.__name__}.{n}") for o, n in SYNCHRONOUS]
)
def test_check_then_write_functions_are_not_coroutines(owner, name):
    """A sync function cannot ``await``, so its check and write share one loop step."""
    function = getattr(owner, name)
    assert not inspect.iscoroutinefunction(function)
    assert not inspect.isasyncgenfunction(function)
    assert not inspect.isgeneratorfunction(function)


def test_the_worker_itself_is_the_only_async_step_in_the_device_lane():
    assert inspect.iscoroutinefunction(Broker._work)
    assert inspect.iscoroutinefunction(Broker._maintain)
