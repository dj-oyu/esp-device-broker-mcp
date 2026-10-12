"""Local-operator, capture-only grants. No credential, pixel, or serial handling.

Call every method on the broker's event loop. The operator UI must display the
subject, target, effects and expiry before its explicit create/update/arm action.
An authorization is a revocable queue-time snapshot, not permission to interrupt
an already running USB operation. The backend still performs its fresh HOME
handshake and verifies the physical port immediately before each capture.
"""

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from .broker import Broker, Session

MAX_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_ARM_SECONDS = 3600
MAX_POLICIES = 256
MAX_AUDIT_EVENTS = 512
HOME_ATTESTATION = "DEVICE HOME AND FIRMWARE VERIFIED"
EFFECTS = (
    "Capture may send Esc and exit an app or editor, losing unsaved state",
    "Opening USB serial may reset the device due to driver control-line behavior",
    "Each capture requires a fresh HOME handshake; arming does not assert it remains HOME",
    "Image delivery transmits rendered pixels, which may contain private information",
)
_KEY_PRINCIPAL = re.compile(r"key:cpk_[a-f0-9]{32}\Z")
_POLICY_ID = re.compile(r"capture_policy_[a-f0-9]{32}\Z")
_ORIGINS = frozenset({"local_cli", "remote_mcp"})
_READ = frozenset({"read"})
_CONTROL = frozenset({"read", "control"})
_AUDIT_EVENTS = frozenset(
    {
        "policy_created",
        "policy_updated",
        "policy_revoked",
        "lease_armed",
        "session_invalidated",
        "capture_authorized",
        "capture_denied",
        "image_authorized",
        "image_denied",
    }
)


class CapturePolicyError(ValueError):
    """Only fixed-category public errors; never echo input or callback failures."""


@dataclass(frozen=True)
class CaptureAuthorization:
    policy_id: str
    revision: int
    principal_id: str
    origin: str
    target: str
    broker_epoch: str
    lease_id: str
    plan_digest: str
    arm_id: str | None


@dataclass(frozen=True)
class _Policy:
    policy_id: str
    revision: int
    principal_id: str
    origin: str
    target: str
    mode: Literal["session", "persistent"]
    capture_home: bool
    image_delivery: bool
    created_at: float
    expires_at: float
    revoked: bool = False
    broker_epoch: str | None = None
    lease_id: str | None = None
    deadline: float | None = None


@dataclass(frozen=True)
class _Arm:
    arm_id: str
    broker_epoch: str
    lease_id: str
    principal_id: str
    target: str
    deadline: float


def _field(value: object, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0  # type: ignore[arg-type,operator]


def _digest(value: dict[str, Any]) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


class CapturePolicies:
    def __init__(
        self,
        broker: "Broker",
        list_principals: Callable[[], Iterable[object]],
        *,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.broker = broker
        self.list_principals = list_principals
        self.wall_clock = wall_clock
        self._session_policies: dict[str, _Policy] = {}
        self._arm: _Arm | None = None
        self._observed_binding: tuple[str, str | None, str] | None = None
        self._schema_db: object | None = None

    def _db(self) -> Any:
        db = self.broker._store().db
        if self._schema_db is not db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS capture_policies (
                    policy_id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS capture_policy_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL, at REAL NOT NULL, policy_id TEXT
                );
            """)
            db.commit()
            self._schema_db = db
        return db

    def _now(self) -> float:
        now = self.wall_clock()
        if not _finite(now):
            raise CapturePolicyError("invalid_policy_clock")
        return float(now)

    def target(self) -> str:
        """Stable configured identity: never a dynamic COM/tty name."""
        serial = self.broker.settings.serial
        return _digest(
            {
                "backend": self.broker.settings.backend,
                "device": self.broker.backend.device,
                "selector": serial.selector.model_dump() if serial is not None else None,
                "expected_firmware_sha256": serial.expected_firmware_sha256 if serial else None,
            }
        )

    def _append_audit(
        self,
        db: sqlite3.Connection,
        event: str,
        policy_id: str | None = None,
    ) -> None:
        """Append within the caller's transaction; never commit permission early."""
        if event not in _AUDIT_EVENTS:
            raise CapturePolicyError("invalid_policy_audit")
        db.execute(
            "INSERT INTO capture_policy_audit(event,at,policy_id) VALUES (?,?,?)",
            (event, self._now(), policy_id),
        )
        db.execute(
            "DELETE FROM capture_policy_audit WHERE id NOT IN "
            "(SELECT id FROM capture_policy_audit ORDER BY id DESC LIMIT ?)",
            (MAX_AUDIT_EVENTS,),
        )

    def _audit(self, event: str, policy_id: str | None = None) -> None:
        db = self._db()
        try:
            with db:
                self._append_audit(db, event, policy_id)
        except sqlite3.Error:
            raise CapturePolicyError("capture_policy_storage_failed") from None

    def invalidate_session(self) -> None:
        """Call on release, restart, or entering needs_review; never touch USB."""
        changed = bool(self._session_policies or self._arm)
        self._session_policies.clear()
        self._arm = None
        if changed:
            self._audit("session_invalidated")

    def _sync(self) -> None:
        session = self.broker.session
        binding = (self.broker.epoch, session.id if session else None, self.target())
        if (
            self._observed_binding is not None
            and binding != self._observed_binding
            or session is None
            or session.deadline <= self.broker.clock()
            or self.broker.closing
            or self.broker._store().needs_review()
        ):
            self.invalidate_session()
        self._observed_binding = binding
        self._session_policies = {
            identifier: policy
            for identifier, policy in self._session_policies.items()
            if policy.deadline is not None and self.broker.clock() < policy.deadline
        }
        if self._arm is not None and self.broker.clock() >= self._arm.deadline:
            self._arm = None

    def _registry(self) -> dict[str, dict[str, Any]]:
        """Use actual current registry metadata, never a prefix as authority."""
        try:
            result: dict[str, dict[str, Any]] = {}
            for count, item in enumerate(self.list_principals()):
                if count >= 1000:
                    return {}
                identifier = _field(item, "principal_id")
                origin = _field(item, "origin")
                expires = _field(item, "expires_at")
                issued = _field(item, "issued_at", 0.0)
                scopes = _field(item, "scopes")
                key_id = _field(item, "key_id")
                if (
                    type(identifier) is not str
                    or not _KEY_PRINCIPAL.fullmatch(identifier)
                    or _field(item, "kind", "issued") != "issued"
                    or origin not in _ORIGINS
                    or not _finite(expires)
                    or not _finite(issued)
                    or _field(item, "revoked_at") is not None
                    or not issued <= self._now() < expires
                    or not isinstance(scopes, (list, tuple, set, frozenset))
                    or any(scope not in _CONTROL for scope in scopes)
                    or key_id is not None
                    and identifier != f"key:{key_id}"
                ):
                    continue
                if identifier in result:
                    return {}  # Ambiguous registry identity is never permission.
                label = _field(item, "label", "Issued key")
                if type(label) is not str or len(label) > 64 or not label.isprintable():
                    label = "Issued key"
                result[identifier] = {
                    "principal_id": identifier,
                    "origin": str(origin),
                    "scopes": sorted(scopes),
                    "expires_at": float(expires),
                    "label": label,
                    "kind": "issued",
                }
            return result
        except Exception:
            return {}

    def _principal_expiry(
        self,
        identifier: str,
        origin: str,
        required: frozenset[str],
    ) -> float | None:
        callback = self.broker.principal_valid
        if callback is None or origin not in _ORIGINS:
            return None
        try:
            if callback(identifier, origin, required) is not True:
                return None
        except Exception:
            return None
        if identifier == f"ephemeral:{origin}":
            return math.inf
        metadata = self._registry().get(identifier)
        if metadata and metadata["origin"] == origin and required <= set(metadata["scopes"]):
            return float(metadata["expires_at"])
        return None

    def _principal_live(self, identifier: str, origin: str, required: frozenset[str]) -> bool:
        expiry = self._principal_expiry(identifier, origin, required)
        return expiry is not None and self._now() < expiry

    def _lease(self) -> "Session":
        self._sync()
        session = self.broker.session
        if (
            session is None
            or session.principal_id is None
            or session.deadline <= self.broker.clock()
            or self.broker.closing
            or self.broker._store().needs_review()
        ):
            raise CapturePolicyError("policy_live_lease_required")
        return session

    def _policies(self) -> list[_Policy]:
        rows = self._db().execute(
            "SELECT payload FROM capture_policies LIMIT ?",
            (MAX_POLICIES + 1,),
        )
        result = [_Policy(**json.loads(row[0])) for row in rows]
        if len(result) > MAX_POLICIES:
            raise CapturePolicyError("capture_policy_limit")
        return result + list(self._session_policies.values())

    def _find(self, policy_id: str) -> _Policy:
        if type(policy_id) is not str or not _POLICY_ID.fullmatch(policy_id):
            raise CapturePolicyError("invalid_capture_policy")
        for policy in self._policies():
            if policy.policy_id == policy_id:
                return policy
        raise CapturePolicyError("capture_policy_not_found")

    def _save(self, policy: _Policy) -> None:
        """Restriction-only write: revoke remains effective if later audit fails."""
        if policy.mode == "session":
            self._session_policies[policy.policy_id] = policy
        else:
            db = self._db()
            try:
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO capture_policies VALUES (?,?)",
                        (
                            policy.policy_id,
                            json.dumps(asdict(policy), sort_keys=True),
                        ),
                    )
            except sqlite3.Error:
                raise CapturePolicyError("capture_policy_storage_failed") from None

    def _save_with_audit(self, policy: _Policy, event: str) -> None:
        """Publish new/edited permissions only with a committed audit record."""
        db = self._db()
        try:
            with db:
                if policy.mode == "persistent":
                    db.execute(
                        "INSERT OR REPLACE INTO capture_policies VALUES (?,?)",
                        (
                            policy.policy_id,
                            json.dumps(asdict(policy), sort_keys=True),
                        ),
                    )
                self._append_audit(db, event, policy.policy_id)
        except sqlite3.Error:
            raise CapturePolicyError("capture_policy_storage_failed") from None
        if policy.mode == "session":
            self._session_policies[policy.policy_id] = policy

    def _summary(self, policy: _Policy) -> dict[str, Any]:
        summary = asdict(policy)
        summary.pop("deadline")
        summary["active"] = self._live(policy, _CONTROL if policy.capture_home else _READ)
        summary["remaining_seconds"] = max(0.0, policy.expires_at - self._now())
        return summary

    def _arm_summary(self) -> dict[str, Any] | None:
        arm = self._arm
        if arm is None:
            return None
        return {
            "lease_id": arm.lease_id,
            "principal_id": arm.principal_id,
            "target": arm.target,
            "remaining_seconds": max(0.0, arm.deadline - self.broker.clock()),
        }

    def options(self) -> dict[str, Any]:
        self._sync()
        principals = list(self._registry().values())
        session = self.broker.session
        if session and session.principal_id == f"ephemeral:{session.owner}":
            if self._principal_live(session.principal_id, session.owner, _READ):
                principals.append(
                    {
                        "principal_id": session.principal_id,
                        "origin": session.owner,
                        "scopes": sorted(_CONTROL),
                        "label": "Shared ephemeral identity (session only)",
                        "expires_at": None,
                        "kind": "ephemeral",
                    }
                )
        serial = self.broker.settings.serial
        return {
            "target": self.target(),
            "backend": self.broker.settings.backend,
            "device": self.broker.backend.device,
            "principals": principals,
            "broker_epoch": self.broker.epoch,
            "server_now": self._now(),
            "expected_firmware_sha256": serial.expected_firmware_sha256 if serial else None,
            "selector": serial.selector.model_dump() if serial else None,
            "active_lease": {
                "lease_id": session.id,
                "principal_id": session.principal_id,
                "origin": session.owner,
                "remaining_seconds": max(0.0, session.deadline - self.broker.clock()),
            }
            if session and session.deadline > self.broker.clock()
            else None,
            "max_ttl_seconds": MAX_TTL_SECONDS,
            "max_arm_seconds": MAX_ARM_SECONDS,
            "effects": list(EFFECTS),
            "arming": self._arm_summary(),
            "attestation": HOME_ATTESTATION,
        }

    def list(self) -> dict[str, Any]:
        self._sync()
        return {
            "policies": [self._summary(policy) for policy in self._policies()],
            "target": self.target(),
            "arming": self._arm_summary(),
            "broker_epoch": self.broker.epoch,
            "server_now": self._now(),
            "audit": [
                dict(row)
                for row in self._db().execute(
                    "SELECT event,at,policy_id FROM capture_policy_audit ORDER BY id DESC LIMIT ?",
                    (MAX_AUDIT_EVENTS,),
                )
            ],
        }

    def _validate_request(
        self,
        capture_home: bool,
        image_delivery: bool,
        ttl_seconds: int,
        expected_target: str,
    ) -> None:
        if type(capture_home) is not bool or type(image_delivery) is not bool:
            raise CapturePolicyError("invalid_capture_permissions")
        if not capture_home and not image_delivery:
            raise CapturePolicyError("empty_capture_permissions")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
            raise CapturePolicyError("invalid_capture_policy_expiry")
        if type(expected_target) is not str or expected_target != self.target():
            raise CapturePolicyError("capture_policy_target_changed")
        if capture_home and self.broker.settings.backend == "serial":
            serial = self.broker.settings.serial
            if serial is None or serial.expected_firmware_sha256 is None:
                raise CapturePolicyError("capture_policy_firmware_required")

    def create(
        self,
        *,
        principal_id: str,
        mode: Literal["session", "persistent"],
        capture_home: bool,
        image_delivery: bool,
        ttl_seconds: int,
        expected_target: str,
        expected_lease_id: str | None = None,
    ) -> dict[str, Any]:
        self._sync()
        self._validate_request(capture_home, image_delivery, ttl_seconds, expected_target)
        if mode not in ("session", "persistent"):
            raise CapturePolicyError("invalid_capture_policy_mode")
        if type(principal_id) is not str:
            raise CapturePolicyError("invalid_capture_policy_principal")
        metadata = self._registry().get(principal_id)
        session = self._lease() if mode == "session" else None
        if session is not None:
            if expected_lease_id != session.id:
                raise CapturePolicyError("capture_policy_lease_changed")
            if session.principal_id != principal_id:
                raise CapturePolicyError("capture_policy_principal_mismatch")
            origin = session.owner
        elif metadata:
            origin = str(metadata["origin"])
        else:
            raise CapturePolicyError("persistent_policy_issued_key_required")
        if not self._principal_live(principal_id, origin, _CONTROL if capture_home else _READ):
            raise CapturePolicyError("capture_policy_principal_not_authorized")
        if len(self._policies()) >= MAX_POLICIES:
            raise CapturePolicyError("capture_policy_limit")
        now = self._now()
        expiry = min(now + ttl_seconds, metadata["expires_at"] if metadata else now + ttl_seconds)
        policy = _Policy(
            policy_id=f"capture_policy_{uuid4().hex}",
            revision=1,
            principal_id=principal_id,
            origin=origin,
            target=self.target(),
            mode=mode,
            capture_home=capture_home,
            image_delivery=image_delivery,
            created_at=now,
            expires_at=expiry,
            broker_epoch=self.broker.epoch if session else None,
            lease_id=session.id if session else None,
            deadline=self.broker.clock() + (expiry - now) if session else None,
        )
        self._save_with_audit(policy, "policy_created")
        return self._summary(policy)

    def update(
        self,
        policy_id: str,
        *,
        expected_revision: int,
        capture_home: bool,
        image_delivery: bool,
        ttl_seconds: int,
        expected_target: str,
    ) -> dict[str, Any]:
        self._sync()
        policy = self._find(policy_id)
        self._revision(policy, expected_revision)
        self._validate_request(capture_home, image_delivery, ttl_seconds, expected_target)
        if policy.revoked or policy.target != self.target():
            raise CapturePolicyError("capture_policy_not_active")
        if not self._principal_live(
            policy.principal_id,
            policy.origin,
            _CONTROL if capture_home else _READ,
        ):
            raise CapturePolicyError("capture_policy_principal_not_authorized")
        if policy.mode == "session":
            session = self._lease()
            if session.id != policy.lease_id or session.principal_id != policy.principal_id:
                raise CapturePolicyError("capture_policy_not_active")
        metadata = self._registry().get(policy.principal_id)
        now = self._now()
        expiry = min(now + ttl_seconds, metadata["expires_at"] if metadata else now + ttl_seconds)
        values = asdict(policy)
        values.update(
            revision=policy.revision + 1,
            capture_home=capture_home,
            image_delivery=image_delivery,
            expires_at=expiry,
            deadline=self.broker.clock() + (expiry - now) if policy.mode == "session" else None,
        )
        updated = _Policy(**values)
        self._save_with_audit(updated, "policy_updated")
        return self._summary(updated)

    @staticmethod
    def _revision(policy: _Policy, expected_revision: int) -> None:
        if type(expected_revision) is not int or policy.revision != expected_revision:
            raise CapturePolicyError("stale_capture_policy_revision")

    def revoke(self, policy_id: str, *, expected_revision: int) -> dict[str, Any]:
        self._sync()
        policy = self._find(policy_id)
        self._revision(policy, expected_revision)
        values = asdict(policy)
        values.update(revision=policy.revision + 1, revoked=True)
        revoked = _Policy(**values)
        self._save(revoked)
        try:
            self._audit("policy_revoked", policy_id)
        except CapturePolicyError:
            # Do not roll back a completed restriction to make its audit atomic.
            raise CapturePolicyError("capture_policy_revoked_audit_failed") from None
        return self._summary(revoked)

    def arm(
        self,
        *,
        lease_id: str,
        expected_target: str,
        ttl_seconds: int,
        attestation: str,
        acknowledge_effects: bool,
    ) -> dict[str, Any]:
        session = self._lease()
        if self.broker.settings.backend != "serial":
            raise CapturePolicyError("capture_arming_serial_only")
        self._validate_request(True, False, ttl_seconds, expected_target)
        if ttl_seconds > MAX_ARM_SECONDS:
            raise CapturePolicyError("invalid_capture_arm_expiry")
        if lease_id != session.id:
            raise CapturePolicyError("capture_arm_lease_changed")
        if attestation != HOME_ATTESTATION or acknowledge_effects is not True:
            raise CapturePolicyError("capture_arm_attestation_required")
        assert session.principal_id is not None
        if not self._principal_live(session.principal_id, session.owner, _CONTROL):
            raise CapturePolicyError("capture_policy_principal_not_authorized")
        arm = _Arm(
            uuid4().hex,
            self.broker.epoch,
            session.id,
            session.principal_id,
            self.target(),
            self.broker.clock() + ttl_seconds,
        )
        self._audit("lease_armed")
        self._arm = arm
        return self._arm_summary() or {}

    def _bounds(self, policy: _Policy, principal_expiry: float, *, capture: bool = False) -> bool:
        """Final pure state/time checks, after registry callbacks and audit writes."""
        target = self.target()
        now, monotonic = self._now(), self.broker.clock()
        if (
            self.broker.closing
            or policy.revoked
            or policy.target != target
            or not policy.created_at <= now < min(policy.expires_at, principal_expiry)
        ):
            return False
        session = self.broker.session
        if capture and (
            session is None
            or session.deadline <= monotonic
            or self.broker.settings.backend == "serial"
            and (self._arm is None or self._arm.deadline <= monotonic)
        ):
            return False
        if policy.mode == "persistent":
            return _KEY_PRINCIPAL.fullmatch(policy.principal_id) is not None
        return bool(
            session
            and session.principal_id == policy.principal_id
            and session.owner == policy.origin
            and session.id == policy.lease_id
            and self.broker.epoch == policy.broker_epoch
            and monotonic < session.deadline
            and policy.deadline is not None
            and monotonic < policy.deadline
        )

    def _eligible(self, policy: _Policy, scopes: frozenset[str]) -> float | None:
        expiry = self._principal_expiry(policy.principal_id, policy.origin, scopes)
        return expiry if expiry is not None and self._bounds(policy, expiry) else None

    def _live(self, policy: _Policy, scopes: frozenset[str]) -> bool:
        return self._eligible(policy, scopes) is not None

    def _capture_context(self, plan: Mapping[str, Any]) -> bool:
        self._sync()
        session = self.broker.session
        return bool(
            session
            and session.principal_id
            and self.broker.clock() < session.deadline
            and not self.broker.closing
            and not self.broker._store().needs_review()
            and plan.get("test") == "capture_home"
            and plan.get("broker_epoch") == self.broker.epoch
            and plan.get("session_id") == session.id
            and plan.get("backend") == self.broker.settings.backend
            and plan.get("device") == self.broker.backend.device
            and (
                self.broker.settings.backend != "serial"
                or (
                    self.broker.settings.serial is not None
                    and self.broker.settings.serial.expected_firmware_sha256 is not None
                    and plan.get("expected_firmware_sha256")
                    == self.broker.settings.serial.expected_firmware_sha256
                    and self._arm is not None
                    and self._arm.principal_id == session.principal_id
                    and self._arm.lease_id == session.id
                    and self._arm.broker_epoch == self.broker.epoch
                    and self._arm.target == self.target()
                )
            )
        )

    def authorize_capture(
        self,
        plan: dict[str, Any],
        *,
        record_use: bool = True,
    ) -> CaptureAuthorization | None:
        if self._capture_context(plan):
            session = self.broker.session
            assert session is not None and session.principal_id is not None
            for policy in self._policies():
                if policy.capture_home and policy.principal_id == session.principal_id:
                    expiry = self._eligible(policy, _CONTROL)
                    if expiry is None or policy.origin != session.owner:
                        continue
                    record = CaptureAuthorization(
                        policy.policy_id,
                        policy.revision,
                        session.principal_id,
                        session.owner,
                        self.target(),
                        self.broker.epoch,
                        session.id,
                        _digest(plan),
                        self._arm.arm_id if self._arm else None,
                    )
                    if record_use:
                        self._audit("capture_authorized", policy.policy_id)
                    if self._final_capture(policy, record, plan, expiry):
                        return record
        if record_use:
            self._audit("capture_denied")
        return None

    def validate_capture(self, record: CaptureAuthorization, plan: dict[str, Any]) -> bool:
        if not isinstance(record, CaptureAuthorization) or not self._capture_context(plan):
            return False
        try:
            policy = self._find(record.policy_id)
        except CapturePolicyError:
            return False
        expiry = self._eligible(policy, _CONTROL)
        return expiry is not None and self._final_capture(policy, record, plan, expiry)

    def _final_capture(
        self,
        policy: _Policy,
        record: CaptureAuthorization,
        plan: dict[str, Any],
        expiry: float,
    ) -> bool:
        # Registry callbacks and sqlite audit writes can take longer than a TTL.
        # Do not invoke either after these final checks. A revised grant cannot
        # inherit a previously evaluated identity or queue authorization.
        try:
            if self._find(policy.policy_id) != policy:
                return False
        except CapturePolicyError:
            return False
        if not self._capture_context(plan):
            return False
        session = self.broker.session
        assert session is not None
        return bool(
            policy.revision == record.revision
            and policy.capture_home
            and policy.principal_id == record.principal_id == session.principal_id
            and policy.origin == record.origin == session.owner
            and record.broker_epoch == self.broker.epoch
            and record.lease_id == session.id
            and record.target == self.target()
            and record.plan_digest == _digest(plan)
            and record.arm_id == (self._arm.arm_id if self._arm else None)
            and self._bounds(policy, expiry, capture=True)
        )

    def authorize_image(
        self,
        job_id: str,
        retained_metadata: object,
        *,
        record_use: bool = True,
    ) -> bool:
        self._sync()
        principal = _field(retained_metadata, "principal_id")
        origin = _field(retained_metadata, "origin")
        deadline = _field(retained_metadata, "deadline")
        job = self.broker._store().job(job_id)
        plan = self.broker._store().plan(job["plan_hash"]) if job else None
        if (
            self.broker.closing
            or type(principal) is not str
            or origin not in _ORIGINS
            or not _finite(deadline)
            or self.broker.clock() >= deadline
            or _field(retained_metadata, "delivery_state") == "rejected"
            or _field(retained_metadata, "policy_target") != self.target()
            or not job
            or job["state"] != "succeeded"
            or not plan
            or plan.get("test") != "capture_home"
            or plan.get("backend") != self.broker.settings.backend
            or plan.get("device") != self.broker.backend.device
            or plan.get("broker_epoch") != self.broker.epoch
        ):
            if record_use:
                self._audit("image_denied")
            return False
        for policy in self._policies():
            if (
                policy.image_delivery
                and policy.principal_id == principal
                and policy.origin == origin
                and (policy.mode == "persistent" or policy.lease_id == plan.get("session_id"))
            ):
                expiry = self._eligible(policy, _READ)
                if expiry is None:
                    continue
                if record_use:
                    self._audit("image_authorized", policy.policy_id)
                try:
                    current = self._find(policy.policy_id)
                except CapturePolicyError:
                    return False
                return bool(
                    current == policy
                    and self._bounds(current, expiry)
                    and self.broker.clock() < deadline
                    and _field(retained_metadata, "delivery_state") != "rejected"
                    and _field(retained_metadata, "policy_target") == self.target()
                )
        if record_use:
            self._audit("image_denied")
        return False
