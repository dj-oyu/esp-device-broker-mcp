"""Characterization (before stage 3b): the four pre-run / pre-write authority gates.

Stage 3b moves the enqueue final gate, the dispatch final gate in ``admit()``,
``_running_authorized`` and ``_flash_live`` behind one ``ExecutionAuthority``. The gates
are not identical (docs/redesign/contracts/flash.md section 3 and pitfall 14), so this
file flips one condition at a time and pins what each checkpoint answers:

- ENQUEUE: the final check block of ``Broker._execute`` (raises a reason code).
- DISPATCH: the final check block of ``Broker.admit`` (cancels with a reason code).
- RUNNING: ``Broker._running_authorized`` (non-flash executors and commits).
- FLASH_IO / FLASH_COMMIT: the ``live`` callback handed to the flash executor, and the
  ``committing=True`` guard that ``Store.finish_firmware`` runs inside ``BEGIN IMMEDIATE``.

A flip is applied inside the gate's last principal callback, so every earlier check has
already passed (``before`` flips are applied before the gate instead). Mock backend and
the synthetic ROM driver only; no hardware.
"""

import dataclasses
import json
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from test_flash_policy import env as env
from test_pending_execution import prepare, running, submit

from cardputer_mcp import collector_binding, flash_binding, registration_digest
from cardputer_mcp.broker import BrokerError

JOB = "submitted"
ALLOW, DENY, RAISE = "allow", "deny", "raise"
PNA, EINA = "principal_not_authorized", "execution_intent_not_available"


def patch_everywhere(mp, name, value):
    """Patch ``name`` in every package module that imported the same function object."""
    original = {
        "collector_live": collector_binding.collector_live,
        "live_binding": flash_binding.live_binding,
        "flash_digests": registration_digest.flash_digests,
    }[name]
    for module_name, module in tuple(sys.modules.items()):
        if module_name.startswith("cardputer_mcp") and getattr(module, name, None) is original:
            mp.setattr(module, name, value)


@dataclass
class Ctx:
    broker: Any
    mp: Any
    job_id: str
    plan_hash: str
    # Single-run matrices (RUNNING, FLASH_*) roll DB changes back with a savepoint.
    savepoint: bool = False
    opened: bool = False

    @property
    def store(self):
        return self.broker._store()

    def write(self, sql, params):
        db = self.store.db
        if self.savepoint:
            if not self.opened:
                db.execute("SAVEPOINT authority_flip")
                self.opened = True
            db.execute(sql, params)
        else:
            db.execute(sql, params)
            db.commit()

    def undo(self):
        if self.opened:
            self.store.db.execute("ROLLBACK TO authority_flip")
            self.store.db.execute("RELEASE authority_flip")
            self.opened = False


# --- flips: (apply, answer of the last principal callback, apply before the gate) ------


def _nothing(c):
    pass


def _validator_replaced(c):
    c.mp.setattr(c.broker, "principal_valid", lambda *args: True)


def _validator_removed(c):
    c.mp.setattr(c.broker, "principal_valid", None)


def _runtime_binding(c):
    b = c.broker
    c.mp.setattr(
        b,
        "settings",
        b.settings.model_copy(update={"mock_stage_seconds": b.settings.mock_stage_seconds + 1}),
    )


def _collector_unbound(c):
    patch_everywhere(c.mp, "collector_live", lambda *args: False)


def _flash_binding_not_live(c):
    patch_everywhere(c.mp, "live_binding", lambda *args: False)


def _digest_unlisted(c):
    patch_everywhere(c.mp, "flash_digests", lambda *args: frozenset())


def _session_replaced(c):
    c.mp.setattr(c.broker, "session", dataclasses.replace(c.broker.session))


def _session_revoked(c):
    c.mp.setattr(c.broker.session, "revoked", True)


def _deadline_passed(c):
    c.mp.setattr(c.broker.session, "deadline", c.broker.clock() - 1)


def _deadline_not_finite(c):
    c.mp.setattr(c.broker.session, "deadline", float("nan"))


def _closing(c):
    c.mp.setattr(c.broker, "closing", True)


def _uncertain(c):
    c.mp.setattr(c.broker, "execution_uncertain", True)


def _owned_profile_cleanup(c):
    # Uncertain for admission, but a peer build's owned normal teardown is not
    # revoked authority for a running job (allow_profile_cleanup=True).
    process = c.broker.firmware_build.process
    c.mp.setattr(c.broker.profile_jobs, "active", "peer-build")
    c.mp.setattr(process, "active", True)
    c.mp.setattr(process, "cleanup_pending", True)
    c.mp.setattr(process, "uncertain", True)


def _physically_busy(c):
    c.mp.setattr(c.broker.backend, "active", c.broker.backend.active + 1)


def _queue_full(c):
    c.mp.setattr(c.broker.queue, "qsize", lambda: 10**6)


def _history_full(c):
    c.mp.setattr(c.store, "record_count", lambda table: 10000)


def _job_row_present(c):
    c.store.add_job(c.job_id, c.plan_hash)


def _job_row_cancelled(c):
    c.write("UPDATE jobs SET state='cancelled' WHERE id=?", (c.job_id,))


def _job_plan_hash_changed(c):
    c.write("UPDATE jobs SET plan_hash=? WHERE id=?", ("f" * 64, c.job_id))


def _stored_plan_changed(c):
    payload = c.store.plan(c.plan_hash)
    payload["synthetic_extra_field"] = 1
    c.write(
        "UPDATE plans SET payload=? WHERE hash=?",
        (json.dumps(payload, sort_keys=True), c.plan_hash),
    )


def _epoch_changed(c):
    c.mp.setattr(c.broker, "epoch", "f" * 32)


def _device_lock_released(c):
    c.mp.setattr(c.broker, "_device_lock", SimpleNamespace(is_locked=False))


def _state_lock_released(c):
    c.mp.setattr(c.broker, "_state_lock", SimpleNamespace(is_locked=False))


def _active_job_changed(c):
    c.mp.setattr(c.broker, "active_job", "different-job")


@dataclass(frozen=True)
class Flip:
    apply: Any
    answer: Any = True  # True, False or RAISE: the last principal callback's answer
    before: bool = False


N = None  # not applicable at this checkpoint
# name: (flip, ENQUEUE, DISPATCH, RUNNING, FLASH_IO, FLASH_COMMIT)
MATRIX = {
    "nothing": (Flip(_nothing), ALLOW, ALLOW, ALLOW, ALLOW, ALLOW),
    "callback_false": (Flip(_nothing, False), PNA, EINA, DENY, DENY, DENY),
    # Only the flash gate lets a callback exception through.
    "callback_raises": (Flip(_nothing, RAISE), PNA, EINA, DENY, RAISE, RAISE),
    "validator_replaced": (Flip(_validator_replaced), EINA, EINA, DENY, DENY, DENY),
    "validator_removed_before": (Flip(_validator_removed, before=True), N, N, DENY, DENY, DENY),
    "runtime_binding": (Flip(_runtime_binding), EINA, EINA, DENY, DENY, DENY),
    # flash_live has no collector_live.
    "collector_unbound": (Flip(_collector_unbound), EINA, EINA, DENY, ALLOW, ALLOW),
    # A non-flash plan has no flash binding; RUNNING never reads one.
    "flash_binding_not_live": (Flip(_flash_binding_not_live), ALLOW, ALLOW, ALLOW, DENY, DENY),
    # Registration allow/digest is read only by the flash gate, before its callback.
    "digest_unlisted_before": (
        Flip(_digest_unlisted, before=True),
        ALLOW,
        ALLOW,
        ALLOW,
        DENY,
        DENY,
    ),
    "digest_unlisted_in_callback": (Flip(_digest_unlisted), ALLOW, ALLOW, ALLOW, ALLOW, ALLOW),
    "session_replaced": (Flip(_session_replaced), EINA, EINA, DENY, DENY, DENY),
    # Admission reads the lease clock, not the revocation flag; a running job the reverse.
    # (LeaseManager.revoke also moves the deadline to now, so admission refuses it too.)
    "session_revoked": (Flip(_session_revoked), ALLOW, ALLOW, DENY, DENY, DENY),
    "deadline_passed": (Flip(_deadline_passed), EINA, EINA, ALLOW, ALLOW, ALLOW),
    "deadline_not_finite": (Flip(_deadline_not_finite), EINA, EINA, DENY, DENY, DENY),
    "closing": (Flip(_closing), EINA, EINA, DENY, DENY, DENY),
    "uncertain": (Flip(_uncertain), EINA, EINA, DENY, DENY, DENY),
    # Running gates use the weaker _execution_uncertain(allow_profile_cleanup=True).
    "owned_profile_cleanup": (Flip(_owned_profile_cleanup), EINA, EINA, ALLOW, ALLOW, ALLOW),
    # _physical_busy only at dispatch.
    "physically_busy": (Flip(_physically_busy), ALLOW, EINA, ALLOW, ALLOW, ALLOW),
    # Capacity and history size only at enqueue.
    "queue_full": (Flip(_queue_full), EINA, ALLOW, ALLOW, ALLOW, ALLOW),
    "history_full": (Flip(_history_full), EINA, ALLOW, ALLOW, ALLOW, ALLOW),
    "job_row_present": (Flip(_job_row_present), EINA, N, N, N, N),
    # A dispatch refusal writes its reason only to a row that is still queued with the
    # intent's plan hash; otherwise the row is left as the callback changed it.
    "job_row_cancelled": (Flip(_job_row_cancelled), N, DENY, DENY, DENY, DENY),
    "job_plan_hash_changed": (Flip(_job_plan_hash_changed), N, DENY, DENY, DENY, DENY),
    "stored_plan_changed": (Flip(_stored_plan_changed), EINA, EINA, DENY, DENY, DENY),
    "epoch_changed": (Flip(_epoch_changed), EINA, EINA, DENY, DENY, DENY),
    "device_lock_released": (Flip(_device_lock_released), EINA, EINA, DENY, DENY, DENY),
    "state_lock_released": (Flip(_state_lock_released), EINA, EINA, DENY, DENY, DENY),
    # Enqueue does not read active_job; the later gates do.
    "active_job_changed": (Flip(_active_job_changed), ALLOW, EINA, DENY, DENY, DENY),
}
CHECKPOINTS = ("ENQUEUE", "DISPATCH", "RUNNING", "FLASH_IO", "FLASH_COMMIT")


def expected(checkpoint):
    column = CHECKPOINTS.index(checkpoint) + 1
    return {name: row[column] for name, row in MATRIX.items() if row[column] is not None}


def cases(checkpoint):
    return [pytest.param(name, want, id=name) for name, want in expected(checkpoint).items()]


def answering(flip, ctx):
    """The last principal callback: apply the flip (unless ``before``), then answer."""
    if not flip.before:
        flip.apply(ctx)
    if flip.answer is RAISE:
        raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
    return flip.answer


def counting(last, flip, ctx):
    """A validator that answers True until its ``last`` call, which runs the flip."""
    calls = []

    def valid(*args):
        calls.append(args)
        if len(calls) == last:
            return answering(flip, ctx)
        return True

    return valid, calls


def outcome(gate):
    try:
        return ALLOW if gate() is True else DENY
    except RuntimeError:
        return RAISE


def evaluate(broker, gate, flip, ctx):
    """One direct evaluation of a ``live``-style gate under one flip, then undo it."""
    with pytest.MonkeyPatch.context() as mp:
        ctx.mp = mp
        try:
            mp.setattr(broker, "principal_valid", lambda *args: answering(flip, ctx))
            if flip.before:
                flip.apply(ctx)
            return outcome(gate)
        finally:
            ctx.undo()


# --- ENQUEUE ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "want"), cases("ENQUEUE"))
async def test_enqueue_final_gate(tmp_path, name, want):
    flip = MATRIX[name][0]
    async with running(tmp_path) as (broker, _):
        # The final gate's callback is the last principal call of an enqueue.
        baseline, calls = counting(0, flip, None)
        first = prepare(broker, "baseline")
        broker.principal_valid = baseline
        submit(broker, first)
        last = len(calls)
        await broker.queue.join()
        assert last >= 1 and broker.job("baseline")["state"] == "succeeded"
        broker.principal_valid = lambda *args: True
        digest = prepare(broker, JOB)
        with pytest.MonkeyPatch.context() as mp:
            ctx = Ctx(broker, mp, JOB, digest)
            valid, calls = counting(last, flip, ctx)
            broker.principal_valid = valid
            try:
                result = submit(broker, digest)
                got = ALLOW if result["state"] == "queued" else result["state"]
            except BrokerError as error:
                got = str(error)
            finally:
                broker.principal_valid = lambda *args: True
        assert len(calls) == last
        assert got == want
        await broker.queue.join()
        if want == ALLOW:
            assert broker.job(JOB)["state"] == "succeeded"
        elif name != "job_row_present":
            assert broker._store().job(JOB) is None


# --- DISPATCH --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "want"), cases("DISPATCH"))
async def test_dispatch_final_gate(tmp_path, name, want):
    flip = MATRIX[name][0]
    async with running(tmp_path) as (broker, _):
        digest = prepare(broker, JOB)
        submit(broker, digest)
        mp = pytest.MonkeyPatch()
        ctx = Ctx(broker, mp, JOB, digest)
        # After the in-admission tick: call 1 is the first lease check, call 2 the
        # final gate's callback (tests/test_stage05_admission.py).
        valid, calls = counting(2, flip, ctx)
        original_expire, original_admit = broker._expire, broker.admit
        admitted = []

        def expire():
            original_expire()
            if not admitted:
                broker.principal_valid = valid

        def admit(*args):
            try:
                run = original_admit(*args)
                job = broker._store().job(JOB)
                admitted.append(ALLOW if run is not None else job["result"] or DENY)
                return run
            finally:
                mp.undo()
                ctx.undo()
                broker.principal_valid = lambda *args: True

        broker._expire, broker.admit = expire, admit
        await broker.queue.join()
        assert len(calls) == 2
        assert admitted == [want]
        job = broker._store().job(JOB)
        if want == ALLOW:
            assert job["state"] == "succeeded"
        else:
            assert broker.backend.calls == 0
            if want == EINA:
                assert job["state"] == "cancelled"


# --- RUNNING ---------------------------------------------------------------------------


async def test_running_authorized(tmp_path):
    async with running(tmp_path) as (broker, _):
        digest = prepare(broker, JOB)
        original_admit, original_execute = broker.admit, broker.backend.execute
        runs, got = [], {}

        def admit(*args):
            run = original_admit(*args)
            runs.append(run)
            return run

        async def execute(plan, stage):
            run = runs[-1]
            for name in expected("RUNNING"):
                ctx = Ctx(broker, None, JOB, digest, savepoint=True)
                gate = lambda run=run: broker._running_authorized(run)  # noqa: E731
                got[name] = evaluate(broker, gate, MATRIX[name][0], ctx)
            return await original_execute(plan, stage)

        broker.admit, broker.backend.execute = admit, execute
        submit(broker, digest)
        await broker.queue.join()
        assert got == expected("RUNNING")
        # Every flip was undone: the job itself still ran and succeeded.
        assert broker.job(JOB)["state"] == "succeeded"


# --- FLASH_IO and FLASH_COMMIT -----------------------------------------------------------


async def test_flash_live_and_its_commit_guard(env):
    broker, store = env.broker, env.store
    prepared = env.prepare("authority-matrix", build_job_id="build-one")
    job_id, digest = "authority-matrix", prepared["plan_hash"]
    original_execute = broker.firmware_flash.execute
    original_commit = broker.firmware_flash.commit_success
    original_finish = store.finish_firmware
    io_live: list[Any] = []
    commit_live: list[Any] = []
    got_io, got_commit, extra = {}, {}, {}

    def matrix(gate, checkpoint):
        return {
            name: evaluate(
                broker, gate, MATRIX[name][0], Ctx(broker, None, job_id, digest, savepoint=True)
            )
            for name in expected(checkpoint)
        }

    async def execute(plan, stage, live):
        io_live.append(live)
        got_io.update(matrix(live, "FLASH_IO"))
        return await original_execute(plan, stage, live)

    def commit_success(plan, result, live):
        commit_live.append(live)
        original_commit(plan, result, live)
        # Committed, before cleanup: the succeeded row is refused outside the transaction.
        assert not store.db.in_transaction and broker.active_job == job_id
        extra["commit_outside_transaction"] = outcome(live)

    def finish(job, result, promotion, guard, *args):
        calls = 0

        def guarded():
            nonlocal calls
            calls += 1
            row = store.db.execute("SELECT state,result FROM jobs WHERE id=?", (job_id,)).fetchone()
            assert store.db.in_transaction
            if calls == 1:
                assert row[0] == "running"
                extra["commit_while_running"] = outcome(commit_live[-1])
            else:
                # Inside BEGIN IMMEDIATE, the row is already succeeded with the exact result.
                assert tuple(row) == ("succeeded", "firmware_flash_completed")
                got_commit.update(matrix(commit_live[-1], "FLASH_COMMIT"))
                extra["io_live_on_succeeded_row"] = outcome(io_live[-1])
                ctx = Ctx(broker, None, job_id, digest, savepoint=True)
                ctx.write("UPDATE jobs SET result='changed-result' WHERE id=?", (job_id,))
                extra["commit_other_result"] = outcome(commit_live[-1])
                ctx.undo()
            return guard()

        return original_finish(job, result, promotion, guarded, *args)

    broker.firmware_flash.execute = execute
    broker.firmware_flash.commit_success = commit_success
    store.finish_firmware = finish
    env.execute(prepared)
    await broker.queue.join()
    assert broker.job(job_id)["state"] == "succeeded", broker.job(job_id)
    assert got_io == expected("FLASH_IO")
    assert got_commit == expected("FLASH_COMMIT")
    assert extra == {
        "commit_while_running": ALLOW,
        "io_live_on_succeeded_row": DENY,
        "commit_other_result": DENY,
        "commit_outside_transaction": DENY,
    }
