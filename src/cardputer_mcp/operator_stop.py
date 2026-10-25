"""Online local-operator stop of a device-lane job owned by another principal.

The person at this machine can stop a queued or running device job that a cloud
client or another local agent started under the current device lease. This is the
owner's own stop (`Broker.cancel`) reached with the live lease of that job, so the
same per-kind table (`cancellation.device_stop`) and the same signals apply; only
the authority differs (local operator HTTP gate instead of the lease capability).

Never interrupted here: a running flash, restore or backup job. Its stop signal
reaches the flash driver at any stage, including during a write, so the operator
is refused with `operator_stop_refused_device_write` and the owner keeps its own
cancel. Other running kinds that the owner cannot stop either stay refused.

Ownership is not released: a stop only signals the running job, which keeps the
lease and device until its own confirmed close, exactly as an owner cancel does.
Each accepted stop leaves one `execution_audit` row naming the operator. The MCP
`cancel` tool is unchanged and remains owner-only.

Synchronous: the checks, the stop and the audit row share one loop step.
"""

from typing import Any

from .broker import Broker, BrokerError
from .cancellation import DeviceStop, device_stop
from .models import CancelRequest

# Device-lane stops that never interrupt a firmware write. QUEUED is a pure ledger
# transition (also for a queued flash job, which has written nothing yet).
OPERATOR_STOPPABLE = frozenset(
    {
        DeviceStop.QUEUED,
        DeviceStop.APP_EVENT,
        DeviceStop.SMOKE,
        DeviceStop.SOURCE,
        DeviceStop.BUILD,
    }
)
AUDIT_EVENTS = {
    DeviceStop.QUEUED: "operator_cancelled_queued",
    DeviceStop.APP_EVENT: "operator_stop_requested",
    DeviceStop.SMOKE: "operator_stop_requested",
    DeviceStop.SOURCE: "operator_stop_requested",
    DeviceStop.BUILD: "operator_stop_requested",
}


def _target(broker: Broker, job_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """The job and plan when it is a queued/running job of the current device lease."""
    store = broker._store()
    job = store.job(job_id)
    if job is None or job["state"] not in ("queued", "running"):
        return None
    plan = store.plan(job["plan_hash"])
    lease = broker.session
    if (
        plan is None
        or plan.get("job_id") != job_id
        or lease is None
        or lease.revoked
        or not isinstance(plan.get("session_id"), str)
        or plan["session_id"] != lease.id
    ):
        return None
    return job, plan


def stop_kind(broker: Broker, job_id: str) -> DeviceStop | None:
    """The stop the operator would get for `job_id`, or None when it is not a lease job."""
    target = _target(broker, job_id)
    if target is None:
        return None
    job, plan = target
    return device_stop(job["state"], str(plan.get("test")))


def stop_available(broker: Broker, job_id: str) -> bool:
    return stop_kind(broker, job_id) in OPERATOR_STOPPABLE


def stop_job(
    broker: Broker, job_id: str, plan_hash: str, broker_epoch: str, principal_id: str
) -> dict[str, Any]:
    if broker.closing:
        raise BrokerError("broker_closing")
    if broker_epoch != broker.epoch:
        raise BrokerError("stale_operator_snapshot")
    if not principal_id:
        raise BrokerError("local_operator_required")
    store = broker._store()
    job = store.job(job_id)
    if job is None or job["plan_hash"] != plan_hash:
        raise BrokerError("stop_target_not_found")
    if job["state"] not in ("queued", "running"):
        raise BrokerError("job_not_active")
    target = _target(broker, job_id)
    lease = broker.session
    if target is None or lease is None:
        # Registered-project build/source jobs use /operator/job-cancel instead.
        raise BrokerError("operator_stop_not_available")
    stop = device_stop(job["state"], str(target[1].get("test")))
    if stop is DeviceStop.FLASH:
        raise BrokerError("operator_stop_refused_device_write")
    if stop not in OPERATOR_STOPPABLE:
        raise BrokerError("operator_stop_not_supported")
    # The owner's own stop with the job's live lease: same table, same signals,
    # same lease/ownership semantics (nothing is released here).
    stopped = broker.cancel(CancelRequest(session=lease.capability, job_id=job_id))
    try:
        store.audit_execution(job_id, plan_hash, principal_id, AUDIT_EVENTS[stop])
        audited = True
    except Exception:
        # The stop was already requested; report the missing row instead of hiding it.
        audited = False
    broker.operator_changes.notify()
    return {
        "broker_epoch": broker.epoch,
        "job_id": job_id,
        "plan_hash": plan_hash,
        "state": stopped["state"],
        "stop_requested": bool(stopped.get("stop_requested", False)),
        "requires_review": bool(stopped.get("requires_review", False)),
        "stopped_by": "local_operator",
        "audit_recorded": audited,
    }
