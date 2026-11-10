"""The device-lane job kinds, declared in one place (DESIGN-REDESIGN stage 3a).

``Broker._work`` is split into three parts:

- ``Broker.admit()``: synchronous admission. Every check and the ``running``
  transition share one loop step.
- ``JOB_KINDS[test].execute``: the per-kind executor. Waiting happens only here.
- ``Broker.finish()``: synchronous terminal handling. It maps a failure to its code,
  or calls the kind's synchronous ``commit``. ``Broker._close_admitted()`` then
  runs the cleanup that follows every admitted job.

Functions marked ``@sync_section`` (and ``admit``/``finish``) are plain ``def`` with
no ``await``; ``tests/test_stage3a_sync_sections.py`` checks this in the AST.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

from .backend import BackendResult
from .fixed_diagnostic import DiagnosticContext, finish_fixed_diagnostic
from .logs import Event
from .models import FLASH_TESTS, TestName

if TYPE_CHECKING:
    from .broker import Broker, ExecutionIntent
    from .derby_provenance import DerbyProvenance
    from .flash_binding import FlashBinding
    from .job_logs import DeviceLogTap, JobLog
    from .lease_manager import Session
    from .store import Store

_F = TypeVar("_F", bound=Callable[..., Any])


def sync_section(function: _F) -> _F:
    """Mark a check-then-write section. It must stay a plain ``def`` with no ``await``."""
    return function


@dataclass(eq=False)
class AdmittedJob:
    """What admission fixed for one job; executors and finish read only this."""

    job_id: str
    store: Store
    job: dict[str, Any]
    plan: dict[str, Any]
    lease: Session
    queued_intent: ExecutionIntent
    current_binding: str | None
    flash_binding: FlashBinding | None
    capture_principal: str | None
    derby_scope: DerbyProvenance | None
    job_log: JobLog | None = None
    device_tap: DeviceLogTap | None = None

    @property
    def test(self) -> str:
        return str(self.plan["test"])


Result = str | BackendResult
Executor = Callable[["Broker", AdmittedJob], Awaitable[Result]]
# A commit returns the terminal log event and whether the job now needs review.
Commit = Callable[["Broker", AdmittedJob, Result, Any], tuple[Event, bool]]


@dataclass(frozen=True)
class JobKind:
    execute: Executor
    commit: Commit


# --- executors (the only awaiting part of the device lane) ----------------------------


async def _execute_app_session(broker: Broker, run: AdmittedJob) -> Result:
    return await broker.app_sessions.execute(
        broker.backend,
        run.plan,
        run.capture_principal or "",
        partial(broker._on_stage, run),
        partial(broker._running_authorized, run),
        run.job_log,
    )


async def _execute_flash(broker: Broker, run: AdmittedJob) -> Result:
    from .broker import BrokerError

    if run.capture_principal != run.plan.get("flash_principal"):
        raise BrokerError("flash_principal_mismatch")
    return await broker.firmware_flash.execute(
        run.plan, partial(broker._on_stage, run), partial(broker._flash_live, run)
    )


async def _execute_source(broker: Broker, run: AdmittedJob) -> Result:
    return await broker.firmware_source.execute(
        run.plan, partial(broker._on_stage, run), partial(broker._running_authorized, run)
    )


async def _execute_build(broker: Broker, run: AdmittedJob) -> Result:
    from .broker import BrokerError

    if run.capture_principal != run.plan.get("build_principal"):
        raise BrokerError("build_principal_mismatch")
    return await broker.firmware_build.execute(run.plan, partial(broker._on_stage, run))


async def _execute_smoke(broker: Broker, run: AdmittedJob) -> Result:
    from .broker import BrokerError
    from .serial_backend import SerialBackend

    if not isinstance(broker.backend, SerialBackend):
        raise BrokerError("backend_not_available")
    running_authorized = partial(broker._running_authorized, run)
    result = await broker.backend.execute_smoke(
        run.plan, partial(broker._on_stage, run), running_authorized
    )
    if run.test == TestName.DERBY.value and (
        not running_authorized() or broker.backend.smoke_stop.is_set()
    ):
        raise BrokerError("operation_interrupted_review_required")
    return result


async def _execute_backend(broker: Broker, run: AdmittedJob) -> Result:
    return await broker.backend.execute(run.plan, partial(broker._on_stage, run))


async def _execute_native(broker: Broker, run: AdmittedJob) -> Result:
    from .serial_backend import SerialBackend

    if not isinstance(broker.backend, SerialBackend):
        raise ValueError("backend_not_available")
    return await broker.backend.execute_native(
        run.plan, partial(broker._on_stage, run), partial(broker._running_authorized, run)
    )


# --- commits (synchronous; run by Broker.finish after a valid result) -------------------


@sync_section
def _commit_app_session(
    broker: Broker, run: AdmittedJob, result: Result, completed: Any
) -> tuple[Event, bool]:
    # Only a confirmed stop and close reach here, so running for the whole duration
    # (app_session_duration_elapsed) is a normal end; every abnormal end raised instead.
    terminal = "cancelled" if completed == "app_session_cancelled" else "succeeded"
    run.store.ledger.transition(run.job_id, terminal, completed)
    return {
        "succeeded": Event.PASSED,
        "failed": Event.FAILED,
        "cancelled": Event.CANCELLED,
    }[terminal], False


@sync_section
def _commit_flash(
    broker: Broker, run: AdmittedJob, result: Result, completed: Any
) -> tuple[Event, bool]:
    broker.firmware_flash.commit_success(
        run.plan, completed, partial(broker._flash_live, run, committing=True)
    )
    return Event.PASSED, False


@sync_section
def _commit_source(
    broker: Broker, run: AdmittedJob, result: Result, completed: Any
) -> tuple[Event, bool]:
    broker.firmware_source.commit_success(
        run.plan, completed, partial(broker._running_authorized, run)
    )
    return Event.PASSED, False


@sync_section
def _commit_fixed_trial(
    broker: Broker, run: AdmittedJob, result: Result, completed: Any
) -> tuple[Event, bool]:
    from .serial_backend import SerialBackend

    if (
        not isinstance(result, BackendResult)
        or result.fixed_trial is None
        or run.capture_principal is None
        or not isinstance(broker.backend, SerialBackend)
    ):
        raise ValueError("fixed_trial_result_invalid")
    fixed_review = not broker._running_authorized(run) or broker.backend.smoke_stop.is_set()
    finish_fixed_diagnostic(
        DiagnosticContext(
            run.capture_principal,
            origin=run.lease.owner,
            epoch=broker.epoch,
            lease_id=run.lease.id,
        ),
        run.job_id,
        run.job["plan_hash"],
        completed,
        result.fixed_trial,
        fixed_review,
        run.store,
    )
    return Event.PASSED, fixed_review


@sync_section
def _commit_succeeded(
    broker: Broker, run: AdmittedJob, result: Result, completed: Any
) -> tuple[Event, bool]:
    diagnostic = (
        {"derby_observation": result.derby_observation.model_dump(mode="json", exclude_none=True)}
        if isinstance(result, BackendResult)
        and result.derby_observation is not None
        and run.test == TestName.DERBY.value
        else None
    )
    run.store.ledger.transition(
        run.job_id, "succeeded", completed, broker._derby_diagnostic(run, diagnostic)
    )
    return Event.PASSED, False


# --- the registry -----------------------------------------------------------------------


@sync_section
def _commit_native(
    broker: Broker, run: AdmittedJob, result: Result, completed: Any
) -> tuple[Event, bool]:
    from .native_diagnostic import request_identity
    from .serial_backend import SerialBackend

    if (
        not isinstance(result, BackendResult)
        or not isinstance(broker.backend, SerialBackend)
        or broker.backend.active
        or broker.backend.hardware_connected
        or broker.backend.ownership_uncertain
        or broker.backend.last_closed_plan != run.plan
        or not broker._running_authorized(run)
    ):
        raise ValueError("native_diagnostic_final_commit_not_authorized")
    snapshot = result.native_diagnostic
    if snapshot is None:
        if (
            completed != "native_diagnostic_cancelled_before_request"
            or not broker.backend.native_cancel.is_set()
        ):
            raise ValueError("native_diagnostic_protocol_invalid")
        if not run.store.ledger.transition(
            run.job_id,
            "cancelled",
            completed,
            {
                "native_diagnostic": {
                    "state": "NOT_REQUESTED",
                    "work_not_started": True,
                    "work_finished": False,
                    "output_drained": False,
                    "home_restored": False,
                    "borrows_released": False,
                    "owner_released": False,
                    "serial_close_confirmed": True,
                }
            },
            expect={"running"},
        ):
            raise ValueError("native_diagnostic_final_commit_not_authorized")
        return Event.CANCELLED, False
    expected = (
        ("succeeded", "native_diagnostic_completed", Event.PASSED)
        if snapshot.completed
        else ("cancelled", "native_diagnostic_cancelled", Event.CANCELLED)
        if snapshot.cancelled
        else ("failed", "native_diagnostic_refused", Event.FAILED)
        if snapshot.state == "REFUSED" and snapshot.flags == 0x20
        else None
    )
    if (
        expected is None
        or snapshot.request_id != request_identity(run.plan)
        or completed != expected[1]
    ):
        raise ValueError("native_diagnostic_protocol_invalid")
    diagnostic = {
        "native_diagnostic": {
            "state": snapshot.state,
            "flags": snapshot.flags,
            "work_finished": bool(snapshot.flags & 1),
            "output_drained": bool(snapshot.flags & 2),
            "home_restored": bool(snapshot.flags & 4),
            "borrows_released": bool(snapshot.flags & 8),
            "owner_released": bool(snapshot.flags & 16),
            "work_not_started": bool(snapshot.flags & 32),
            "serial_close_confirmed": True,
        }
    }
    if not run.store.ledger.transition(
        run.job_id, expected[0], completed, diagnostic, expect={"running"}
    ):
        raise ValueError("native_diagnostic_final_commit_not_authorized")
    return expected[2], False


_FLASH = JobKind(_execute_flash, _commit_flash)
_BACKEND = JobKind(_execute_backend, _commit_succeeded)

JOB_KINDS: Mapping[str, JobKind] = MappingProxyType(
    {
        TestName.APP_SESSION.value: JobKind(_execute_app_session, _commit_app_session),
        **{test.value: _FLASH for test in FLASH_TESTS},
        TestName.SOURCE.value: JobKind(_execute_source, _commit_source),
        TestName.BUILD.value: JobKind(_execute_build, _commit_succeeded),
        TestName.DERBY.value: JobKind(_execute_smoke, _commit_succeeded),
        TestName.DERBY_FIXED.value: JobKind(_execute_smoke, _commit_fixed_trial),
        TestName.DIAGNOSTICS.value: _BACKEND,
        TestName.NATIVE_DIAGNOSTIC.value: JobKind(_execute_native, _commit_native),
        TestName.WORKFLOW.value: _BACKEND,
        TestName.OBSERVE.value: _BACKEND,
        TestName.FIRMWARE.value: _BACKEND,
        TestName.RESTART.value: _BACKEND,
        TestName.HELLO.value: _BACKEND,
        TestName.CAPTURE.value: _BACKEND,
    }
)


def kind_for(test: str) -> JobKind:
    """The kind for a plan's test; any other name runs on the backend, as before."""
    return JOB_KINDS.get(test, _BACKEND)
