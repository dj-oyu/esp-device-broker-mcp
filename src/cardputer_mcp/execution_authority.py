"""The pre-run / pre-write authority checks, in one place (DESIGN-REDESIGN stage 3b).

Before stage 3b the broker had four hand-copied check chains: the enqueue final gate
(``Broker._execute``), the dispatch final gate (``Broker.admit``), ``_running_authorized``
and ``_flash_live``. They are not identical, and the differences are intentional
(docs/redesign/contracts/flash.md section 3 and pitfall 14). Here each gate is a named
``Checkpoint`` whose ``Rules`` list its checks in the order they run, so a difference
is a visible row of ``RULES`` and not a missing ``and`` in a copied expression:

- ``ENQUEUE`` alone checks queue capacity, history size and that the job row does not
  exist yet. A failed principal callback is ``principal_not_authorized``.
- ``DISPATCH`` alone checks ``_physical_busy`` and the queued row, plan epoch and the
  queued lease; it keeps the validator snapshot taken before admission's earlier
  callbacks, re-reads the validator for its call, and skips the call for a lease
  without a principal.
- ``RUNNING`` (non-flash executors and commits) turns any exception into a refusal.
- ``FLASH_IO`` / ``FLASH_COMMIT`` alone check the registration (``allow_<op>`` and the
  digest), before the callback; they have no ``collector_live``; their running-job
  check uses the weaker ``_execution_uncertain(allow_profile_cleanup=True)``; a callback
  exception is not swallowed; ``FLASH_COMMIT`` passes ``committing=True`` (the guard
  inside ``Store.finish_firmware``'s ``BEGIN IMMEDIATE``).

The principal callback can change any state, so every check listed after it runs
after it. ``ExecutionAuthority.check`` is synchronous (``@sync_section``): no
``await`` may appear in this module (tests/test_stage3b_execution_authority.py).
The existing chokepoints stay where they are: ``FlashLifecycle.run`` → ``driver.write``,
``Store.finish_firmware``, ``BuildProcess`` and ``Broker._end_ownership``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .collector_binding import collector_live
from .flash_binding import live_binding
from .job_kinds import sync_section
from .registration_digest import flash_digests

if TYPE_CHECKING:
    from .broker import Broker, ExecutionIntent
    from .flash_binding import FlashBinding
    from .job_kinds import AdmittedJob
    from .lease_manager import Session
    from .store import Store

PrincipalValid = Callable[[str, str, frozenset[str]], bool]
SCOPES = frozenset({"read", "control"})
INTENT_NOT_AVAILABLE = "execution_intent_not_available"


class Checkpoint(StrEnum):
    ENQUEUE = "enqueue"
    DISPATCH = "dispatch"
    RUNNING = "running"
    FLASH_IO = "flash_io"
    FLASH_COMMIT = "flash_commit"


@dataclass(eq=False)
class Gate:
    """What the caller fixed before the checkpoint; the checks compare against it."""

    store: Store
    job_id: str
    plan_hash: str
    plan: dict[str, Any]
    lease: Session
    intent: ExecutionIntent
    runtime_binding: str | None
    flash_binding: FlashBinding | None = None
    capture_principal: str | None = None
    # DISPATCH: the validator and lease id read before admission's earlier callbacks.
    valid: PrincipalValid | None = None
    queued_lease: str | None = None

    @classmethod
    def admitted(cls, run: AdmittedJob) -> Gate:
        return cls(
            store=run.store,
            job_id=run.job_id,
            plan_hash=run.job["plan_hash"],
            plan=run.plan,
            lease=run.lease,
            intent=run.queued_intent,
            runtime_binding=run.current_binding,
            flash_binding=run.flash_binding,
            capture_principal=run.capture_principal,
        )


@dataclass(frozen=True)
class Rules:
    """One checkpoint: its checks in order, and how its principal callback behaves."""

    steps: tuple[str, ...]
    # The callback's principal: the lease's, or (flash) the principal captured at admission.
    capture_principal: bool = False
    # The answer when that principal is None (the callback is then not made).
    without_principal: bool = False
    # Call the validator read now, not the snapshot (DISPATCH).
    reread_validator: bool = False
    # A callback exception is a refusal; otherwise it propagates.
    callback_errors_refuse: bool = True
    # Any exception in any check is a refusal (RUNNING).
    errors_refuse: bool = False
    committing: bool = False
    # The refusal code, and per-step overrides.
    refused: str = INTENT_NOT_AVAILABLE
    codes: Mapping[str, str] = field(default_factory=dict)


@dataclass(eq=False)
class _Pass:
    """State of one evaluation: snapshots taken around the callback, and its answer."""

    rules: Rules
    valid: PrincipalValid | None
    runtime_binding: str | None = None
    authorized: bool = False
    job: dict[str, Any] | None = None
    stored_plan: dict[str, Any] | None = None


Step = Callable[["Broker", Gate, _Pass], bool]
STEPS: dict[str, Step] = {}


def _step(name: str) -> Callable[[Step], Step]:
    def register(function: Step) -> Step:
        STEPS[name] = function
        return function

    return register


# --- snapshots and the principal callback ---------------------------------------------


@_step("snapshot_validator")
def _snapshot_validator(b: Broker, g: Gate, s: _Pass) -> bool:
    s.valid = b.principal_valid
    return True


@_step("snapshot_runtime_binding")
def _snapshot_runtime_binding(b: Broker, g: Gate, s: _Pass) -> bool:
    s.runtime_binding = b._runtime_binding()
    return True


@_step("principal_callback")
def _principal_callback(b: Broker, g: Gate, s: _Pass) -> bool:
    """The only callback; it may change any state. Its answer is the `authorized` step."""
    rules = s.rules
    principal = g.capture_principal if rules.capture_principal else g.lease.principal_id
    if principal is None:
        s.authorized = rules.without_principal
        return True
    valid = b.principal_valid if rules.reread_validator else s.valid
    if valid is None:
        s.authorized = False
    elif rules.callback_errors_refuse:
        try:
            s.authorized = valid(principal, g.lease.owner, SCOPES) is True
        except Exception:
            s.authorized = False
    else:
        s.authorized = valid(principal, g.lease.owner, SCOPES) is True
    return True


@_step("read_job_rows")
def _read_job_rows(b: Broker, g: Gate, s: _Pass) -> bool:
    s.job = g.store.job(g.job_id)
    s.stored_plan = g.store.plan(g.plan_hash)
    return True


@_step("authorized")
def _authorized(b: Broker, g: Gate, s: _Pass) -> bool:
    return s.authorized


# --- principal, runtime and provenance bindings ---------------------------------------


@_step("validator_present")
def _validator_present(b: Broker, g: Gate, s: _Pass) -> bool:
    return b.principal_valid is not None


@_step("validator_unchanged")
def _validator_unchanged(b: Broker, g: Gate, s: _Pass) -> bool:
    return b.principal_valid is s.valid


@_step("runtime_binding_unchanged_across_callback")
def _runtime_binding_across_callback(b: Broker, g: Gate, s: _Pass) -> bool:
    return b._runtime_binding() == s.runtime_binding


@_step("runtime_binding_unchanged")
def _runtime_binding_unchanged(b: Broker, g: Gate, s: _Pass) -> bool:
    return b._runtime_binding() == g.runtime_binding


@_step("flash_binding_live_if_bound")
def _flash_binding_live_if_bound(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.flash_binding is None or live_binding(b, g.flash_binding, g.plan)


@_step("flash_binding_bound")
def _flash_binding_bound(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.flash_binding is not None


@_step("flash_binding_live")
def _flash_binding_live(b: Broker, g: Gate, s: _Pass) -> bool:
    assert g.flash_binding is not None  # flash_binding_bound runs first
    return live_binding(b, g.flash_binding, g.plan)


@_step("collector_live")
def _collector_live(b: Broker, g: Gate, s: _Pass) -> bool:
    return collector_live(b, g.plan, g.lease.principal_id or "")


@_step("registration_allows")
def _registration_allows(b: Broker, g: Gate, s: _Pass) -> bool:
    registration = b.settings.flash_devices.get(g.plan["device_id"])
    return not (
        registration is None
        or getattr(registration, "allow_" + g.plan["flash_operation"]) is not True
        or g.plan["registration_sha256"] not in flash_digests(registration)
    )


# --- lease and intent -----------------------------------------------------------------


@_step("not_closing")
def _not_closing(b: Broker, g: Gate, s: _Pass) -> bool:
    return not b.closing


@_step("session_present")
def _session_present(b: Broker, g: Gate, s: _Pass) -> bool:
    return b.session is not None


@_step("capture_principal_present")
def _capture_principal_present(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.capture_principal is not None


@_step("session_is_lease")
def _session_is_lease(b: Broker, g: Gate, s: _Pass) -> bool:
    return b.session is g.lease


@_step("lease_principal_is_capture")
def _lease_principal_is_capture(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.lease.principal_id == g.capture_principal


@_step("lease_not_revoked")
def _lease_not_revoked(b: Broker, g: Gate, s: _Pass) -> bool:
    # A running job keeps its lease past the deadline; only revocation stops it
    # (Broker._execution_lease_binding_live).
    return not g.lease.revoked


@_step("lease_is_queued_lease")
def _lease_is_queued_lease(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.lease.id == g.queued_lease


@_step("lease_deadline_finite")
def _lease_deadline_finite(b: Broker, g: Gate, s: _Pass) -> bool:
    return math.isfinite(g.lease.deadline)


@_step("clock_finite")
def _clock_finite(b: Broker, g: Gate, s: _Pass) -> bool:
    return math.isfinite(b.clock())


@_step("lease_deadline_ahead")
def _lease_deadline_ahead(b: Broker, g: Gate, s: _Pass) -> bool:
    return not b.clock() >= g.lease.deadline


@_step("intent_binding_live")
def _intent_binding_live(b: Broker, g: Gate, s: _Pass) -> bool:
    return b._execution_intent_binding_live(g.intent)


@_step("intent_live")
def _intent_live(b: Broker, g: Gate, s: _Pass) -> bool:
    return b._execution_intent_live(g.intent)


@_step("running_lease_binding_live")
def _running_lease_binding_live(b: Broker, g: Gate, s: _Pass) -> bool:
    return b._execution_lease_binding_live(g.intent, running=True)


@_step("running_job_live")
def _running_job_live(b: Broker, g: Gate, s: _Pass) -> bool:
    # Uses the weaker _execution_uncertain(allow_profile_cleanup=True); with
    # committing=True it requires store.db.in_transaction and also accepts the
    # job's exact succeeded row.
    return b._running_job_binding_live(g.plan, g.intent, committing=s.rules.committing)


# --- resources ------------------------------------------------------------------------


@_step("not_uncertain")
def _not_uncertain(b: Broker, g: Gate, s: _Pass) -> bool:
    return not b._execution_uncertain()


@_step("not_physically_busy")
def _not_physically_busy(b: Broker, g: Gate, s: _Pass) -> bool:
    return not b._physical_busy()


@_step("hardware_not_connected")
def _hardware_not_connected(b: Broker, g: Gate, s: _Pass) -> bool:
    return not b.backend.hardware_connected


@_step("queue_has_room")
def _queue_has_room(b: Broker, g: Gate, s: _Pass) -> bool:
    return not b._execution_capacity_full()


@_step("history_has_room")
def _history_has_room(b: Broker, g: Gate, s: _Pass) -> bool:
    return not g.store.record_count("jobs") >= 10000


@_step("device_lock_held")
def _device_lock_held(b: Broker, g: Gate, s: _Pass) -> bool:
    return bool(b._device_lock.is_locked)


@_step("state_lock_held")
def _state_lock_held(b: Broker, g: Gate, s: _Pass) -> bool:
    return bool(b._state_lock.is_locked)


@_step("active_job_is_this")
def _active_job_is_this(b: Broker, g: Gate, s: _Pass) -> bool:
    return b.active_job == g.job_id


# --- the job row and the stored plan ---------------------------------------------------


@_step("job_row_absent")
def _job_row_absent(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.store.job(g.job_id) is None


@_step("job_row_present")
def _job_row_present(b: Broker, g: Gate, s: _Pass) -> bool:
    return s.job is not None


@_step("job_row_queued")
def _job_row_queued(b: Broker, g: Gate, s: _Pass) -> bool:
    assert s.job is not None  # job_row_present runs first
    return bool(s.job["state"] == "queued")


@_step("job_row_plan_hash")
def _job_row_plan_hash(b: Broker, g: Gate, s: _Pass) -> bool:
    assert s.job is not None
    return bool(s.job["plan_hash"] == g.plan_hash)


@_step("read_plan_unchanged")
def _read_plan_unchanged(b: Broker, g: Gate, s: _Pass) -> bool:
    return s.stored_plan == g.plan


@_step("stored_plan_unchanged")
def _stored_plan_unchanged(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.store.plan(g.plan_hash) == g.plan


@_step("plan_digest")
def _plan_digest(b: Broker, g: Gate, s: _Pass) -> bool:
    from .broker import digest_plan

    return digest_plan(g.plan) == g.plan_hash


@_step("plan_epoch")
def _plan_epoch(b: Broker, g: Gate, s: _Pass) -> bool:
    return g.plan.get("broker_epoch") == b.epoch


# --- the checkpoints ------------------------------------------------------------------

_FLASH_STEPS = (
    # Before the callback: lease identity, registration, locks, a validator.
    "not_closing",
    "session_present",
    "capture_principal_present",
    "session_is_lease",
    "lease_principal_is_capture",
    "clock_finite",
    "lease_not_revoked",
    "registration_allows",
    "device_lock_held",
    "state_lock_held",
    "validator_present",
    "snapshot_validator",
    "snapshot_runtime_binding",
    "principal_callback",
    # After the callback. No collector_live here.
    "authorized",
    "validator_unchanged",
    "runtime_binding_unchanged_across_callback",
    "runtime_binding_unchanged",
    "flash_binding_bound",
    "flash_binding_live",
    "session_is_lease",
    "running_lease_binding_live",
    "device_lock_held",
    "state_lock_held",
    "running_job_live",
)

RULES: Mapping[Checkpoint, Rules] = MappingProxyType(
    {
        Checkpoint.ENQUEUE: Rules(
            steps=(
                "snapshot_validator",
                "principal_callback",
                "authorized",
                # No callbacks after this point.
                "validator_unchanged",
                "runtime_binding_unchanged",
                "flash_binding_live_if_bound",
                "collector_live",
                "session_is_lease",
                "intent_binding_live",
                "not_closing",
                "not_uncertain",
                "queue_has_room",
                "history_has_room",
                "job_row_absent",
                "stored_plan_unchanged",
                "state_lock_held",
                "device_lock_held",
            ),
            codes=MappingProxyType({"authorized": "principal_not_authorized"}),
        ),
        Checkpoint.DISPATCH: Rules(
            steps=(
                "principal_callback",
                "read_job_rows",
                "job_row_present",
                "authorized",
                "validator_unchanged",
                "runtime_binding_unchanged",
                "flash_binding_live_if_bound",
                "collector_live",
                "state_lock_held",
                "job_row_queued",
                "job_row_plan_hash",
                "read_plan_unchanged",
                "plan_digest",
                "plan_epoch",
                "session_is_lease",
                "lease_is_queued_lease",
                "lease_deadline_finite",
                "clock_finite",
                "lease_deadline_ahead",
                "not_closing",
                "not_uncertain",
                "not_physically_busy",
                "hardware_not_connected",
                "device_lock_held",
                "intent_live",
            ),
            without_principal=True,
            reread_validator=True,
        ),
        Checkpoint.RUNNING: Rules(
            steps=(
                "snapshot_validator",
                "principal_callback",
                "authorized",
                "validator_unchanged",
                "runtime_binding_unchanged",
                "collector_live",
                "session_is_lease",
                "running_lease_binding_live",
                "device_lock_held",
                "state_lock_held",
                "active_job_is_this",
                "stored_plan_unchanged",
                "plan_digest",
                "running_job_live",
            ),
            errors_refuse=True,
        ),
        Checkpoint.FLASH_IO: Rules(
            steps=_FLASH_STEPS, capture_principal=True, callback_errors_refuse=False
        ),
        Checkpoint.FLASH_COMMIT: Rules(
            steps=_FLASH_STEPS,
            capture_principal=True,
            callback_errors_refuse=False,
            committing=True,
        ),
    }
)


class ExecutionAuthority:
    """Evaluates one checkpoint of ``RULES`` against the broker's current state."""

    def __init__(self, broker: Broker) -> None:
        self.broker = broker

    @sync_section
    def check(self, checkpoint: Checkpoint, gate: Gate) -> str | None:
        """None when every check passes; otherwise the refusal code of the first failure."""
        rules = RULES[checkpoint]
        state = _Pass(rules, gate.valid)
        if not rules.errors_refuse:
            return self._run(rules, gate, state)
        try:
            return self._run(rules, gate, state)
        except Exception:
            return rules.refused

    @sync_section
    def allows(self, checkpoint: Checkpoint, gate: Gate) -> bool:
        return self.check(checkpoint, gate) is None

    @sync_section
    def _run(self, rules: Rules, gate: Gate, state: _Pass) -> str | None:
        for name in rules.steps:
            if not STEPS[name](self.broker, gate, state):
                return rules.codes.get(name, rules.refused)
        return None
