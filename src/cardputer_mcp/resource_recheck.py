"""Runtime uncertainty latches: what is set, and an explicit operator re-check.

`uncertainty()` lists every latch that is set, with a fixed reason code. It is read
by `status` and the operator snapshot and changes nothing.

`recheck()` is an explicit local-operator action, never run automatically. For each
set latch it runs one concrete check and clears only the latches whose check passes:

| Latch                       | Check                                                    |
| --------------------------- | -------------------------------------------------------- |
| `serial_ownership`          | retained handle reports closed (existing confirmed-close |
| `flash_observation_serial`  | helper only; a port is never opened)                     |
| `build_process`             | retained job object has zero members and an exited      |
| `flash_process`             | leader, and every output reader finished (never kills)   |
| `flash_shutdown`            | the flash driver's existing confirmed close succeeds     |
| `profile_state`             | cause was only an owned process, and that process is now |
|                             | confirmed exited                                         |
| `profile_worker`            | SQLite write probe passes, no job row is `running`       |
| `execution`                 | as `profile_worker`, plus every device-lane latch clear  |
| `build_output`              | none: kept (no side-effect-free check)                   |
| `source_cleanup`            | none: kept (no side-effect-free check)                   |

Nothing runs while any device or registered-project work is active: every latch is
then kept with `owned_work_active`. Jobs, leases, the queue and history are not
changed, and nothing is retried.
"""

from typing import Any

# Latches that pin the physical device lane; `execution` clears only after them.
_DEVICE_LANE = (
    "serial_ownership",
    "flash_observation_serial",
    "build_process",
    "flash_process",
    "flash_shutdown",
)


def _engine(broker: Any) -> Any:
    return getattr(broker.firmware_flash, "engine", None)


def _observation(broker: Any) -> Any:
    engine = _engine(broker)
    return getattr(engine.driver, "_observation_backend", None) if engine is not None else None


def _flash_process(broker: Any) -> Any:
    engine = _engine(broker)
    return getattr(engine.driver, "process", None) if engine is not None else None


def uncertainty(broker: Any) -> list[dict[str, str]]:
    """Every set latch with its fixed reason; read-only."""
    b = broker
    flags: list[dict[str, str]] = []

    def add(flag: str, reason: str) -> None:
        flags.append({"flag": flag, "reason": reason})

    if getattr(b.backend, "ownership_uncertain", False):
        add("serial_ownership", "serial_close_unconfirmed")
    if getattr(_observation(b), "ownership_uncertain", False):
        add("flash_observation_serial", "serial_close_unconfirmed")
    if b.firmware_build.process.uncertain:
        add("build_process", "build_process_exit_unconfirmed")
    process = _flash_process(b)
    if process is not None and process.uncertain:
        add("flash_process", "flash_process_exit_unconfirmed")
    engine = _engine(b)
    if engine is not None and engine.uncertain:
        add("flash_shutdown", "flash_shutdown_unconfirmed")
    if b.firmware_build.output_uncertain:
        add("build_output", "build_output_release_unconfirmed")
    if b.firmware_source.snapshots.uncertain:
        add("source_cleanup", "source_cleanup_unconfirmed")
    profile = b.profile_jobs
    if profile.state_uncertain:
        add(
            "profile_state",
            profile.state_uncertain_reason or "registered_project_review_unresolved",
        )
    if profile.uncertain:
        add("profile_worker", "registered_project_state_write_failed")
    if b.execution_uncertain:
        add("execution", b.execution_uncertain_reason or "execution_state_unconfirmed")
    return flags


def _lane_idle(broker: Any) -> bool:
    """No device worker, registered-project job or HOME recovery is in progress."""
    return bool(
        not broker.closing
        and broker._worker_job is None
        and broker.profile_jobs.active is None
        and not any(
            entry.state == "running" or (entry.task is not None and not entry.task.done())
            for entry in broker.home_recovery.entries.values()
        )
    )


def _state_consistent(broker: Any) -> tuple[bool, str]:
    store = broker._store()
    if store.running_job_ids():
        return False, "job_row_still_running"
    if not store.write_probe():
        return False, "state_store_unavailable"
    return True, "lane_idle_state_consistent"


def recheck(broker: Any) -> dict[str, Any]:
    """Explicit operator action: check each set latch and clear only passing ones.

    Synchronous: every check and clear shares one loop step with no await.
    """
    b = broker
    before = {item["flag"] for item in uncertainty(b)}
    results: dict[str, tuple[bool, str]] = {}
    if before and not _lane_idle(b):
        results = {flag: (False, "owned_work_active") for flag in sorted(before)}
    elif before:
        if "serial_ownership" in before:
            check = getattr(b.backend, "recheck_closed", None)
            results["serial_ownership"] = (
                check() if check is not None else (False, "serial_handle_unknown")
            )
        if "flash_observation_serial" in before:
            check = getattr(_observation(b), "recheck_closed", None)
            results["flash_observation_serial"] = (
                check() if check is not None else (False, "serial_handle_unknown")
            )
        if "build_process" in before:
            results["build_process"] = b.firmware_build.process.recheck_exited()
        if "flash_process" in before:
            check = getattr(_flash_process(b), "recheck_exited", None)
            results["flash_process"] = (
                check() if check is not None else (False, "process_tree_unverifiable")
            )
        if "flash_shutdown" in before:
            check = getattr(_engine(b), "recheck_shutdown", None)
            results["flash_shutdown"] = (
                check() if check is not None else (False, "flash_driver_still_owned")
            )
        for flag in ("build_output", "source_cleanup"):
            if flag in before:
                results[flag] = (False, "no_side_effect_free_check")
        profile = b.profile_jobs
        if "profile_state" in before:
            process = b.firmware_build.process
            if profile.state_uncertain_cause != "owned_process":
                results["profile_state"] = (False, "no_side_effect_free_check")
            elif process.uncertain or process.active or b.firmware_source.active:
                results["profile_state"] = (False, "dependent_flag_kept")
            else:
                profile.state_uncertain = False
                profile.state_uncertain_reason = profile.state_uncertain_cause = None
                results["profile_state"] = (True, "owned_process_exit_confirmed")
        if "profile_worker" in before:
            consistent = _state_consistent(b)
            if consistent[0]:
                profile.uncertain = False
            results["profile_worker"] = consistent
        if "execution" in before:
            remaining = {item["flag"] for item in uncertainty(b)}
            if remaining & set(_DEVICE_LANE):
                results["execution"] = (False, "dependent_flag_kept")
            else:
                consistent = _state_consistent(b)
                if consistent[0]:
                    b.execution_uncertain = False
                    b.execution_uncertain_reason = None
                results["execution"] = consistent
        _release_stale_active_job(b)
        b.operator_changes.notify()
    elif b.active_job is not None and _lane_idle(b):
        # No latch, but a closed job is still recorded as active: drop it only when
        # every owned resource is idle (the same checks as below).
        _release_stale_active_job(b)
        b.operator_changes.notify()
    after = uncertainty(b)
    return {
        "broker_epoch": b.epoch,
        "checked": [
            {"flag": flag, "outcome": "cleared" if ok else "kept", "reason": reason}
            for flag, (ok, reason) in results.items()
        ],
        "remaining": after,
        "execution_uncertain": b._execution_uncertain(),
    }


def _release_stale_active_job(broker: Any) -> None:
    """Drop `active_job` left by an unconfirmed close once nothing holds the lane."""
    b = broker
    if (
        b.active_job is not None
        and _lane_idle(b)
        and not b.backend.active
        and not b.backend.hardware_connected
        and not getattr(b.backend, "ownership_uncertain", False)
        and not b.firmware_source.active
        and not b.firmware_build.process.active
        and not b.firmware_build.process.uncertain
        and not b.firmware_flash.active
        and not b.firmware_flash.uncertain
    ):
        b.active_job = None
