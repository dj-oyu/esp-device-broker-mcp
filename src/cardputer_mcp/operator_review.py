"""Online local-operator acknowledgement of needs_review rows.

The person at this machine acknowledges a job or device request that ended in
`needs_review`. Only the durable `reviewed` flag is set. State, result, reason and
diagnostic are never changed, nothing is retried or replayed, and the device,
leases and queue are not touched. The offline CLI (`review`, `review-request`)
remains for a stopped broker.

Both functions are synchronous: the check and the guarded write share one loop step.
"""

from typing import Any

from .broker import Broker, BrokerError


def _check_epoch(broker: Broker, broker_epoch: str) -> None:
    if broker.closing:
        raise BrokerError("broker_closing")
    if broker_epoch != broker.epoch:
        raise BrokerError("stale_operator_snapshot")


def _home_recovery_source_in_use(broker: Broker, job_id: str) -> bool:
    # A queued or running HOME recovery needs its source to stay unreviewed until
    # it commits; acknowledging the source then would end that recovery for review.
    return any(
        entry.state in ("queued", "running") and entry.plan.get("source_job_id") == job_id
        for entry in broker.home_recovery.entries.values()
    )


def acknowledge_job(
    broker: Broker, job_id: str, plan_hash: str, broker_epoch: str
) -> dict[str, Any]:
    _check_epoch(broker, broker_epoch)
    store = broker._store()
    job = store.job(job_id)
    if job is None or job["plan_hash"] != plan_hash:
        raise BrokerError("review_target_not_found")
    if job["state"] != "needs_review":
        raise BrokerError("review_not_required")
    if _home_recovery_source_in_use(broker, job_id):
        raise BrokerError("review_target_in_use")
    already = bool(job["reviewed"])
    if not already and not store.ledger.mark_reviewed(job_id):
        raise BrokerError("review_not_required")
    after = store.job(job_id)
    if after is None or after["state"] != "needs_review" or not after["reviewed"]:
        raise BrokerError("review_not_required")
    broker.operator_changes.notify()
    return {
        "broker_epoch": broker.epoch,
        "job_id": job_id,
        "plan_hash": plan_hash,
        "state": after["state"],
        "reviewed": True,
        "already_reviewed": already,
        "replayed": False,
    }


def acknowledge_request(broker: Broker, request_id: str, broker_epoch: str) -> dict[str, Any]:
    _check_epoch(broker, broker_epoch)
    store = broker._store()
    request = store.request(request_id)
    if request is None:
        raise BrokerError("review_target_not_found")
    if request["state"] != "needs_review":
        raise BrokerError("review_not_required")
    already = bool(request["reviewed"])
    if not already and not store.acknowledge_request(request_id):
        raise BrokerError("review_not_required")
    after = store.request(request_id)
    if after is None or after["state"] != "needs_review" or not after["reviewed"]:
        raise BrokerError("review_not_required")
    broker.operator_changes.notify()
    return {
        "broker_epoch": broker.epoch,
        "request_id": request_id,
        "state": after["state"],
        "reviewed": True,
        "already_reviewed": already,
        "replayed": False,
    }
