"""Characterization (before stage 3a): how the device worker ends a job after admission.

Stage 3a splits ``Broker._work`` into ``admit()``, a per-kind executor and ``finish()``.
These rows pin what the current worker writes for each failure-mapping branch, for a
failed final commit, for result validation, and for the cleanup that runs after every
admitted job, so the split cannot change a code, a latch or the lane cancellation.
Mock backend only; no hardware. See docs/redesign/contracts/jobs.md section 1.3 and 5.
"""

import pytest
from test_pending_execution import prepare, running, submit

from cardputer_mcp.backend import BackendResult, DeviceDisconnected, DeviceOperationFailure
from cardputer_mcp.models import PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.source_snapshot import SourceError

JOB = "submitted"
LATER = "later"
SECRET = "SYNTHETIC_PRIVATE_EXCEPTION_TEXT"


def raising(exception):
    async def execute(plan, stage):
        raise exception

    return execute


def returning(value):
    async def execute(plan, stage):
        return value

    return execute


def observation():
    # Any non-None observation: the job kind check rejects it before its fields are read.
    return object()


FAILURES = [
    # (case id, backend.execute replacement, state, result, later job cancelled)
    # A typed failure with a confirmed close (the mock) is `failed`; the lane goes on.
    (
        "device_operation_failure",
        raising(DeviceOperationFailure("serial_deadline")),
        "failed",
        "serial_deadline",
        False,
    ),
    (
        "uncertain_device_operation_failure",
        raising(DeviceOperationFailure("serial_operation_uncertain")),
        "needs_review",
        "serial_operation_uncertain",
        True,
    ),
    (
        "device_disconnected",
        raising(DeviceDisconnected()),
        "failed",
        "device_disconnected_no_retry",
        False,
    ),
    (
        "source_error_outside_a_source_job",
        raising(SourceError("source_fetch_failed")),
        "needs_review",
        "backend_error_no_retry",
        True,
    ),
    (
        "unknown_exception",
        raising(RuntimeError(SECRET)),
        "needs_review",
        "backend_error_no_retry",
        True,
    ),
    (
        "observation_on_a_job_that_does_not_observe",
        returning(BackendResult("mock_pass", observation=observation())),
        "needs_review",
        "backend_error_no_retry",
        True,
    ),
]


@pytest.mark.parametrize(
    ("execute", "state", "result", "lane_cancelled"),
    [pytest.param(e, s, r, c, id=i) for i, e, s, r, c in FAILURES],
)
async def test_each_failure_branch_writes_its_code(
    tmp_path, execute, state, result, lane_cancelled
):
    async with running(tmp_path) as (broker, _):
        submit(broker, prepare(broker, JOB))
        submit(broker, prepare(broker, LATER))
        broker.backend.execute = execute
        await broker.queue.join()
        store = broker._store()
        job = store.job(JOB)
        assert (job["state"], job["result"]) == (state, result)
        assert SECRET not in repr(job)
        later = store.job(LATER)
        if lane_cancelled:
            assert (later["state"], later["result"]) == ("cancelled", "recovery_review_required")
        else:
            # The next job was admitted and ran through the same executor.
            assert (later["state"], later["result"]) == (state, result)
        # A mapped failure is history, not the runtime latch.
        assert broker.execution_uncertain is False
        assert broker.active_job is None and broker._worker_job is None
        assert JOB not in broker._execution_intents


async def test_capture_without_an_image_is_rejected_after_the_executor(tmp_path):
    async with running(tmp_path) as (broker, _):
        digest = broker.prepare(
            PrepareRequest(session=broker.session.capability, job_id=JOB, test=Action.CAPTURE)
        )["plan_hash"]
        submit(broker, digest)
        broker.backend.execute = returning(BackendResult("synthetic_capture_not_device", None))
        await broker.queue.join()
        job = broker._store().job(JOB)
        assert (job["state"], job["result"]) == ("needs_review", "backend_error_no_retry")
        assert JOB not in broker.captures
        assert broker.execution_uncertain is False


async def test_a_failed_final_commit_is_reviewed_and_latches(tmp_path):
    async with running(tmp_path) as (broker, _):
        ledger = broker._store().ledger
        original = ledger.transition

        def refuse_success(job_id, to, *args, **kwargs):
            if to == "succeeded":
                raise RuntimeError(SECRET)
            return original(job_id, to, *args, **kwargs)

        ledger.transition = refuse_success
        submit(broker, prepare(broker, JOB))
        submit(broker, prepare(broker, LATER))
        await broker.queue.join()
        store = broker._store()
        job = store.job(JOB)
        assert (job["state"], job["result"]) == ("needs_review", "firmware_final_commit_failed")
        assert SECRET not in repr(job)
        later = store.job(LATER)
        assert (later["state"], later["result"]) == ("cancelled", "recovery_review_required")
        assert broker.execution_uncertain is True
        assert broker.execution_uncertain_reason == "job_final_commit_failed"
        assert broker.active_job is None


async def test_closure_evidence_failure_never_changes_a_success(tmp_path):
    async with running(tmp_path) as (broker, _):
        calls = []

        def fail(*args):
            calls.append(args)
            raise RuntimeError(SECRET)

        broker.home_recovery.record_closed = fail
        submit(broker, prepare(broker, JOB))
        await broker.queue.join()
        job = broker._store().job(JOB)
        assert (job["state"], job["result"]) == ("succeeded", "mock_pass")
        assert len(calls) == 1
        assert broker.execution_uncertain is False
        assert broker.active_job is None


async def test_job_log_open_failure_is_outside_the_finish_path(tmp_path):
    """The job log opens after the running transition and before the executor's
    failure mapping; a failure there takes the outer latch and leaves the row running."""
    async with running(tmp_path) as (broker, _):

        def fail(*args):
            raise RuntimeError(SECRET)

        broker._open_device_job_log = fail
        submit(broker, prepare(broker, JOB))
        await broker.queue.join()
        job = broker._store().job(JOB)
        assert job["state"] == "running"
        assert broker.backend.calls == 0
        assert broker.execution_uncertain is True
        assert broker.execution_uncertain_reason == "durable_state_operation_failed"
        assert broker._worker_job is None and JOB not in broker._execution_intents
        assert broker.worker is not None and not broker.worker.done()
        broker.active_job = None  # let close() run


async def test_success_runs_the_cleanup_and_a_maintenance_tick(tmp_path):
    async with running(tmp_path) as (broker, _):
        ticks = []
        original = broker._expire

        def counted():
            row = broker._store().job(JOB)
            ticks.append((row and row["state"], broker.active_job, broker._worker_job))
            original()

        broker._expire = counted
        submit(broker, prepare(broker, JOB))
        await broker.queue.join()
        job = broker._store().job(JOB)
        assert (job["state"], job["result"]) == ("succeeded", "mock_pass")
        # The admission tick sees the queued row before the job is active; the
        # cleanup tick sees the committed row after active_job is released and
        # while the worker still holds the job.
        assert ("queued", None, None) in ticks
        assert ("succeeded", None, JOB) in ticks
        assert broker.backend.calls == 1
