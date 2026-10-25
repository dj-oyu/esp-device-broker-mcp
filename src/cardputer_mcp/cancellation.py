"""One entry for client and operator cancellation (`docs/redesign/contracts/jobs.md` section 4).

`cancel_job(resident, target)` selects the stop primitive by the kind of target. Each
kind keeps its own authority check and its own result shape, so routing through here
changes nothing a caller observes:

| Target               | Primitive                       | Authority (checked by the primitive) |
| -------------------- | ------------------------------- | ------------------------------------ |
| `LeasedDeviceJob`    | `Broker.cancel`                 | lease capability, plan session       |
| `JobLeaseJob`        | `Broker.cancel_job_only`        | job lease + recorded job principal   |
| `DeferredJob`        | `Broker.cancel_deferred`        | deferred row principal, pending only |
| `Ticket`             | `Broker.cancel_ticket`          | ticket principal                     |
| `DeviceRequestWait`  | `Broker.cancel_device_request`  | resume token, principal, session     |
| `ProfileJob`         | `ProfileJobs.cancel`            | current principal + plan binding     |
| `HomeRecoveryJob`    | `HomeRecovery.cancel`           | plan hash entry + plan principal     |
| `OperatorWait`       | `Broker.operator_cancel_wait`   | local operator (HTTP gate)           |
| `OperatorProfileJob` | `ProfileJobs.operator_cancel`   | local origin, epoch, plan hash       |
| `OperatorDeviceJob`  | `Broker.operator_stop_job`      | local operator (HTTP gate), epoch,   |
|                      | (then `Broker.cancel`)          | plan hash, current lease job         |

Callers keep the request-level checks they made before (authentication, `owned()`,
plan kind routing). Releases (`release`, `close_work_session`, `operator_release_idle`)
end ownership, not a job, and stay with `Broker._end_ownership`. Internal and automatic
stops (`_cancel_queued`, expiry, authority loss, shutdown) are not client entry points.

For a device job addressed by its lease, `device_stop()` is the per-kind table that
`Broker.cancel` follows. Everything here is synchronous.
"""

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, overload

from .models import FLASH_TESTS, CancelDeviceRequest, CancelRequest, TestName

if TYPE_CHECKING:  # both import broker.py, which imports this module
    from .broker import Broker
    from .home_recovery import CancelHomeRecovery


class DeviceStop(Enum):
    """How `Broker.cancel` stops a device-lane job, by state and kind."""

    INTENT = "intent"  # legacy awaiting_approval: cancel the execution intent
    APP_EVENT = "app_event"  # running app_session: set its cancelled event
    SOURCE = "source"  # running source: firmware_source.request_stop
    SMOKE = "smoke"  # running DERBY / DERBY fixed: backend.request_smoke_stop
    BUILD = "build"  # running build: firmware_build.request_stop
    FLASH = "flash"  # running flash/restore/backup: firmware_flash.request_stop
    QUEUED = "queued"  # queued: cancelled client_cancelled_queued
    REFUSE = "refuse"  # anything else: only_queued_jobs_can_be_cancelled


_SMOKE_TESTS = frozenset({TestName.DERBY.value, TestName.DERBY_FIXED.value})
_FLASH_TESTS = frozenset(test.value for test in FLASH_TESTS)


def device_stop(state: str, test: str) -> DeviceStop:
    """The order matters and matches the original if-ladder in `Broker.cancel`."""
    if state == "awaiting_approval":
        return DeviceStop.INTENT
    if state == "running":
        if test == TestName.APP_SESSION.value:
            return DeviceStop.APP_EVENT
        if test == TestName.SOURCE.value:
            return DeviceStop.SOURCE
        if test in _SMOKE_TESTS:
            return DeviceStop.SMOKE
        if test == TestName.BUILD.value:
            return DeviceStop.BUILD
        if test in _FLASH_TESTS:
            return DeviceStop.FLASH
    if state != "queued":
        return DeviceStop.REFUSE
    return DeviceStop.QUEUED


@dataclass(frozen=True)
class LeasedDeviceJob:
    request: CancelRequest


@dataclass(frozen=True)
class JobLeaseJob:
    job_id: str
    principal_id: str


@dataclass(frozen=True)
class DeferredJob:
    job_id: str
    principal_id: str


@dataclass(frozen=True)
class Ticket:
    ticket_id: str
    principal_id: str


@dataclass(frozen=True)
class DeviceRequestWait:
    request: CancelDeviceRequest
    principal_id: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class ProfileJob:
    request: CancelRequest
    principal_id: str
    origin: str


@dataclass(frozen=True)
class HomeRecoveryJob:
    request: "CancelHomeRecovery"
    principal_id: str


@dataclass(frozen=True)
class OperatorWait:
    request_id: str


@dataclass(frozen=True)
class OperatorProfileJob:
    job_id: str
    plan_hash: str
    broker_epoch: str
    principal_id: str
    origin: str


@dataclass(frozen=True)
class OperatorDeviceJob:
    """Local operator stop of a lease job; never interrupts a flash/restore/backup."""

    job_id: str
    plan_hash: str
    broker_epoch: str
    principal_id: str


# Targets whose primitive answers None when the caller owns no such job.
OptionalTarget = JobLeaseJob | DeferredJob
DefiniteTarget = (
    LeasedDeviceJob
    | Ticket
    | DeviceRequestWait
    | ProfileJob
    | HomeRecoveryJob
    | OperatorWait
    | OperatorProfileJob
    | OperatorDeviceJob
)
CancelTarget = OptionalTarget | DefiniteTarget


@overload
def cancel_job(resident: "Broker", target: OptionalTarget) -> dict[str, Any] | None: ...
@overload
def cancel_job(resident: "Broker", target: DefiniteTarget) -> dict[str, Any]: ...
def cancel_job(resident: "Broker", target: CancelTarget) -> dict[str, Any] | None:
    """Dispatch to the target kind's own stop primitive and return its result unchanged.

    `JobLeaseJob` and `DeferredJob` return None when the caller does not own such a
    job, exactly as their primitives do. Primitives are looked up on `resident` at call
    time, so the result and error are the primitive's own.
    """
    match target:
        case LeasedDeviceJob(request):
            return resident.cancel(request)
        case JobLeaseJob(job_id, principal_id):
            return resident.cancel_job_only(job_id, principal_id)
        case DeferredJob(job_id, principal_id):
            return resident.cancel_deferred(job_id, principal_id)
        case Ticket(ticket_id, principal_id):
            return resident.cancel_ticket(ticket_id, principal_id)
        case DeviceRequestWait(request, principal_id, scopes):
            return resident.cancel_device_request(request, principal_id=principal_id, scopes=scopes)
        case ProfileJob(request, principal_id, origin):
            return resident.profile_jobs.cancel(request, principal_id, origin)
        case HomeRecoveryJob(request, principal_id):
            return resident.home_recovery.cancel(request, principal_id)
        case OperatorWait(request_id):
            return resident.operator_cancel_wait(request_id)
        case OperatorProfileJob(job_id, plan_hash, broker_epoch, principal_id, origin):
            return resident.profile_jobs.operator_cancel(
                job_id, plan_hash, broker_epoch, principal_id, origin
            )
        case OperatorDeviceJob(job_id, plan_hash, broker_epoch, principal_id):
            return resident.operator_stop_job(job_id, plan_hash, broker_epoch, principal_id)
    raise TypeError("unknown cancellation target")
