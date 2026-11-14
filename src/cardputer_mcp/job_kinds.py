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
from .logs import Event
from .models import FLASH_TESTS, TestName

if TYPE_CHECKING:
    from .broker import Broker, ExecutionIntent
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


async def _execute_backend(broker: Broker, run: AdmittedJob) -> Result:
    return await broker.backend.execute(run.plan, partial(broker._on_stage, run))


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
def _commit_succeeded(
    broker: Broker, run: AdmittedJob, result: Result, completed: Any
) -> tuple[Event, bool]:
    run.store.ledger.transition(run.job_id, "succeeded", completed)
    return Event.PASSED, False


# --- the registry -----------------------------------------------------------------------

_FLASH = JobKind(_execute_flash, _commit_flash)
_BACKEND = JobKind(_execute_backend, _commit_succeeded)

JOB_KINDS: Mapping[str, JobKind] = MappingProxyType(
    {
        TestName.APP_SESSION.value: JobKind(_execute_app_session, _commit_app_session),
        **{test.value: _FLASH for test in FLASH_TESTS},
        TestName.SOURCE.value: JobKind(_execute_source, _commit_source),
        TestName.BUILD.value: JobKind(_execute_build, _commit_succeeded),
        TestName.DIAGNOSTICS.value: _BACKEND,
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
