"""Explicit local capture permissions, with bounded reviews and no automatic writes.

This panel only calls the existing loopback operator API. It never creates keys,
opens hardware, or infers permission from a selection, refresh, or dismissed dialog.
"""

from __future__ import annotations

import json
import math
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

if TYPE_CHECKING:
    import tkinter as tk


MAX_POLICIES = 256
MAX_RESPONSE_BYTES = 262144
REVIEW_SECONDS = 30
HOME_ATTESTATION = "DEVICE HOME AND FIRMWARE VERIFIED"
MANUAL_NOTICE = (
    "Capture is manual unless you explicitly create a bounded permission below. "
    "Existing read and log access is unchanged."
)
SESSION_NOTICE = (
    "Session permissions end with this lease or broker run. Shared process tokens "
    "identify a group of clients, not an individual; they cannot receive persistent permissions. "
    "Persistent permissions require an existing issued key."
)
CAPTURE_EFFECTS = (
    "Capture sends one Esc to verify HOME, then one s key and receives rendered pixels. "
    "Esc may exit an app/editor and lose unsaved state. Opening or closing the serial port "
    "may reset the device. HOME must be checked freshly for every capture. "
    "Rendered pixels do not prove LCD transfer or frame coherence."
)
DELIVERY_EFFECTS = (
    "Image delivery permits this principal to retrieve captured screen pixels and transmit "
    "them to its AI service without a separate local preview for each matching image."
)
ARM_NOTICE = (
    "Arming confirms only this current session and configured device/firmware. "
    "Release, broker restart, failure, target change, or expiry invalidates the arm."
)
PENDING_CLOSE = (
    "A permissions request is still pending. Closing does not cancel it. "
    "Wait for its result, then close again."
)
REQUEST_FAILED = "Permissions could not be refreshed. Check the broker, then refresh again."
WRITE_UNCERTAIN = (
    "The change was not confirmed. It may have reached the broker. "
    "Refresh and inspect before deciding again; no write was retried."
)


class PolicyAPI(Protocol):
    def policy_options(self) -> dict[str, Any]: ...
    def policies(self) -> dict[str, Any]: ...
    def create_policy(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def update_policy(self, data: dict[str, Any]) -> dict[str, Any]: ...
    def revoke_policy(self, policy_id: str, expected_revision: int) -> dict[str, Any]: ...
    def arm_capture(self, data: dict[str, Any]) -> dict[str, Any]: ...


def _bounded(data: Any) -> None:
    """Reject oversize/deep responses before producing any rendered text."""
    pending = [(data, 0)]
    count = size = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > 8 or count > 16384:
            raise ValueError("invalid_policy_response")
        if isinstance(value, dict):
            if len(value) > 64:
                raise ValueError("invalid_policy_response")
            for key, child in value.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ValueError("invalid_policy_response")
                size += len(key)
                pending.append((child, depth + 1))
        elif isinstance(value, list):
            if len(value) > 1000:
                raise ValueError("invalid_policy_response")
            pending.extend((child, depth + 1) for child in value)
        elif isinstance(value, str):
            size += len(value)
        elif value is not None and type(value) not in (bool, int, float):
            raise ValueError("invalid_policy_response")
        if size > MAX_RESPONSE_BYTES:
            raise ValueError("invalid_policy_response")


def _text(value: Any, limit: int = 256) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        raise ValueError("invalid_policy_response")
    # Escape controls and bidi characters instead of letting them impersonate fields.
    return json.dumps(value, ensure_ascii=True)[1:-1]


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_:.-]{1,160}", value) is None:
        raise ValueError("invalid_policy_response")
    return value


def _hex(value: Any, length: int) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value) is None:
        raise ValueError("invalid_policy_response")
    return value


def _integer(value: Any, minimum: int = 0, maximum: int = 2**53) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid_policy_response")
    return value


def _number(value: Any, maximum: float = 253402300799) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= maximum:
        raise ValueError("invalid_policy_response")
    return float(value)


def _flag(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("invalid_policy_response")
    return value


def _origin(value: Any) -> str:
    if value not in ("local_cli", "remote_mcp"):
        raise ValueError("invalid_policy_response")
    return str(value)


@dataclass(frozen=True)
class Principal:
    principal_id: str
    origin: str
    label: str
    scopes: tuple[str, ...]
    kind: str
    expires_at: float | None
    deadline: float | None

    @classmethod
    def parse(cls, data: dict[str, Any], now: float, started: float) -> Principal:
        identifier, origin = _identifier(data["principal_id"]), _origin(data["origin"])
        kind, scopes, expiry = data["kind"], data["scopes"], data["expires_at"]
        if (
            not isinstance(scopes, list)
            or len(scopes) > 2
            or any(scope not in ("read", "control") for scope in scopes)
            or len(set(scopes)) != len(scopes)
            or "read" not in scopes
            or kind not in ("issued", "ephemeral")
        ):
            raise ValueError("invalid_policy_response")
        if kind == "issued":
            if re.fullmatch(r"key:cpk_[a-f0-9]{32}", identifier) is None:
                raise ValueError("invalid_policy_response")
            expiry = _number(expiry)
        elif identifier != f"ephemeral:{origin}" or expiry is not None:
            raise ValueError("invalid_policy_response")
        return cls(
            identifier,
            origin,
            _text(data["label"], 64),
            tuple(sorted(scopes)),
            kind,
            expiry,
            started + max(0, expiry - now) if expiry is not None else None,
        )

    def permits(self, capture: bool, now: float) -> bool:
        return (
            "read" in self.scopes
            and (not capture or "control" in self.scopes)
            and (self.deadline is None or now < self.deadline)
        )


@dataclass(frozen=True)
class Lease:
    lease_id: str
    principal_id: str | None
    origin: str
    deadline: float


@dataclass(frozen=True)
class PolicyOptions:
    target: str
    broker_epoch: str
    backend: str
    device: str
    firmware: str | None
    selector_text: str
    principals: tuple[Principal, ...]
    lease: Lease | None
    max_ttl: int
    max_arm: int
    server_now: float
    deadline: float
    arm_text: str

    @classmethod
    def parse(cls, data: dict[str, Any], started: float) -> PolicyOptions:
        _bounded(data)
        if data["ok"] is not True or data["backend"] not in ("mock", "serial"):
            raise ValueError("invalid_policy_response")
        now = _number(data["server_now"])
        target, epoch = _hex(data["target"], 64), _hex(data["broker_epoch"], 32)
        raw = data["principals"]
        if not isinstance(raw, list) or len(raw) > 1000:
            raise ValueError("invalid_policy_response")
        principals = tuple(Principal.parse(row, now, started) for row in raw)
        if len({row.principal_id for row in principals}) != len(principals):
            raise ValueError("invalid_policy_response")
        lease = None
        if data["active_lease"] is not None:
            item = data["active_lease"]
            lease = Lease(
                _identifier(item["lease_id"]),
                _identifier(item["principal_id"]) if item["principal_id"] is not None else None,
                _origin(item["origin"]),
                started + _number(item["remaining_seconds"], 300),
            )
        for principal in principals:
            if principal.kind == "ephemeral" and (
                lease is None
                or principal.principal_id != lease.principal_id
                or principal.origin != lease.origin
            ):
                raise ValueError("invalid_policy_response")
        firmware, selector = data["expected_firmware_sha256"], data["selector"]
        if firmware is not None:
            firmware = _hex(firmware, 64)
        if selector is not None:
            if not isinstance(selector, dict) or set(selector) != {
                "vid",
                "pid",
                "serial_number",
                "location",
            }:
                raise ValueError("invalid_policy_response")
            _integer(selector["vid"], maximum=65535)
            _integer(selector["pid"], maximum=65535)
            for key in ("serial_number", "location"):
                if selector[key] is not None:
                    _text(selector[key])
            if not selector["serial_number"] and not selector["location"]:
                raise ValueError("invalid_policy_response")
        if data["backend"] == "serial" and selector is None:
            raise ValueError("invalid_policy_response")
        if data["attestation"] != HOME_ATTESTATION:
            raise ValueError("invalid_policy_response")
        # Effects are explained by fixed local text; never display a server's prose.
        if not isinstance(data["effects"], list) or not 1 <= len(data["effects"]) <= 16:
            raise ValueError("invalid_policy_response")
        for effect in data["effects"]:
            _text(effect, 1024)
        arm_text = "Current session is not armed."
        if data["arming"] is not None:
            arm = data["arming"]
            if (
                lease is None
                or arm["lease_id"] != lease.lease_id
                or arm["principal_id"] != lease.principal_id
                or arm["target"] != target
            ):
                raise ValueError("invalid_policy_response")
            seconds = _number(arm["remaining_seconds"], 3600)
            arm_text = f"Armed session {lease.lease_id}; {math.floor(seconds)} s at refresh."
        return cls(
            target,
            epoch,
            data["backend"],
            _text(data["device"]),
            firmware,
            json.dumps(selector, ensure_ascii=True, sort_keys=True),
            principals,
            lease,
            _integer(data["max_ttl_seconds"], 1, 604800),
            _integer(data["max_arm_seconds"], 1, 3600),
            now,
            started + REVIEW_SECONDS,
            arm_text,
        )

    def principal(self, identifier: str) -> Principal | None:
        return next((item for item in self.principals if item.principal_id == identifier), None)


@dataclass(frozen=True)
class PolicyRow:
    policy_id: str
    revision: int
    principal_id: str
    origin: str
    target: str
    mode: str
    capture_home: bool
    image_delivery: bool
    created_at: float
    expires_at: float
    deadline: float
    revoked: bool
    active: bool
    broker_epoch: str | None
    lease_id: str | None

    @classmethod
    def parse(cls, data: dict[str, Any], now: float, started: float) -> PolicyRow:
        identifier = _identifier(data["policy_id"])
        if re.fullmatch(r"capture_policy_[a-f0-9]{32}", identifier) is None:
            raise ValueError("invalid_policy_response")
        mode = data["mode"]
        epoch, lease = data["broker_epoch"], data["lease_id"]
        if mode == "session":
            epoch, lease = _hex(epoch, 32), _identifier(lease)
        elif mode != "persistent" or epoch is not None or lease is not None:
            raise ValueError("invalid_policy_response")
        capture, delivery = _flag(data["capture_home"]), _flag(data["image_delivery"])
        if not capture and not delivery:
            raise ValueError("invalid_policy_response")
        created, expiry = _number(data["created_at"]), _number(data["expires_at"])
        if created >= expiry:
            raise ValueError("invalid_policy_response")
        return cls(
            identifier,
            _integer(data["revision"], 1),
            _identifier(data["principal_id"]),
            _origin(data["origin"]),
            _hex(data["target"], 64),
            mode,
            capture,
            delivery,
            created,
            expiry,
            started + max(0, expiry - now),
            _flag(data["revoked"]),
            _flag(data["active"]),
            epoch,
            lease,
        )


@dataclass(frozen=True)
class PolicySnapshot:
    options: PolicyOptions
    policies: tuple[PolicyRow, ...]
    audit_text: str

    @classmethod
    def parse(
        cls, options_data: dict[str, Any], listing: dict[str, Any], started: float
    ) -> PolicySnapshot:
        options = PolicyOptions.parse(options_data, started)
        _bounded(listing)
        if (
            listing["ok"] is not True
            or listing["target"] != options.target
            or listing["broker_epoch"] != options.broker_epoch
        ):
            raise ValueError("invalid_policy_response")
        now = _number(listing["server_now"])
        raw = listing["policies"]
        if not isinstance(raw, list) or len(raw) > MAX_POLICIES:
            raise ValueError("invalid_policy_response")
        rows = tuple(PolicyRow.parse(row, now, started) for row in raw)
        if len({row.policy_id for row in rows}) != len(rows):
            raise ValueError("invalid_policy_response")
        audit = listing["audit"]
        if not isinstance(audit, list) or len(audit) > 512:
            raise ValueError("invalid_policy_response")
        events = {
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
        lines = []
        for event in audit:
            if event["event"] not in events:
                raise ValueError("invalid_policy_response")
            identifier = event["policy_id"]
            identifier = _identifier(identifier) if identifier is not None else "-"
            lines.append(f"{utc_time(_number(event['at']))} | {event['event']} | {identifier}")
        return cls(options, rows, "\n".join(lines) or "No capture permission audit events.")


Action = Literal["refresh", "create", "update", "revoke", "arm"]


@dataclass(frozen=True)
class PolicyReview:
    action: Action
    payload: str
    explanation: str
    form_revision: int
    snapshot: PolicySnapshot


@dataclass(frozen=True)
class PolicyOperation:
    sequence: int
    action: Action
    started_at: float
    payload: str = "{}"


@dataclass(frozen=True)
class PolicyOutcome:
    operation: PolicyOperation
    snapshot: PolicySnapshot | None = None
    failed: bool = False


def perform_policy(api: PolicyAPI, operation: PolicyOperation) -> PolicyOutcome:
    try:
        if operation.action == "refresh":
            return PolicyOutcome(
                operation,
                PolicySnapshot.parse(api.policy_options(), api.policies(), operation.started_at),
            )
        payload = json.loads(operation.payload)
        if operation.action == "create":
            response = api.create_policy(payload)
        elif operation.action == "update":
            response = api.update_policy(payload)
        elif operation.action == "revoke":
            response = api.revoke_policy(payload["policy_id"], payload["expected_revision"])
        else:
            response = api.arm_capture(payload)
        _bounded(response)
        if response.get("ok") is not True:
            raise ValueError("invalid_policy_response")
        if operation.action == "arm":
            arm = response["arming"]
            if (
                arm["lease_id"] != payload["lease_id"]
                or arm["target"] != payload["expected_target"]
            ):
                raise ValueError("invalid_policy_response")
            _identifier(arm["principal_id"])
            _number(arm["remaining_seconds"], payload["ttl_seconds"])
        else:
            row = PolicyRow.parse(response["policy"], 0, 0)
            if operation.action == "create":
                if (
                    row.revision != 1
                    or row.principal_id != payload["principal_id"]
                    or row.mode != payload["mode"]
                    or row.lease_id != payload.get("expected_lease_id")
                ):
                    raise ValueError("invalid_policy_response")
            elif (
                row.policy_id != payload["policy_id"]
                or row.revision != payload["expected_revision"] + 1
            ):
                raise ValueError("invalid_policy_response")
            if operation.action == "revoke":
                if not row.revoked:
                    raise ValueError("invalid_policy_response")
            elif (
                row.target != payload["expected_target"]
                or row.revoked
                or row.capture_home != payload["capture_home"]
                or row.image_delivery != payload["image_delivery"]
            ):
                raise ValueError("invalid_policy_response")
        # The next explicit refresh provides authoritative state after every write.
        return PolicyOutcome(operation)
    except Exception:
        # No raw exception, token, response body, or server string reaches Tk.
        return PolicyOutcome(operation, failed=True)


class PolicyController:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.snapshot: PolicySnapshot | None = None
        self.selected = ""
        self.principal_id = ""
        self.form_revision = 0
        self.sequence = 0
        self.pending: PolicyOperation | None = None
        self.fresh = False
        self.closed = False
        self.message = "Refresh permissions, then explicitly select a principal."

    @property
    def busy(self) -> bool:
        return self.pending is not None

    def changed(self) -> None:
        self.form_revision += 1

    def select(self, policy_id: str) -> None:
        if self.selected != policy_id:
            self.selected = policy_id
            self.changed()

    def select_principal(self, principal_id: str) -> None:
        if self.principal_id != principal_id:
            self.principal_id = principal_id
            self.changed()

    def selected_row(self) -> PolicyRow | None:
        return (
            next((row for row in self.snapshot.policies if row.policy_id == self.selected), None)
            if self.snapshot
            else None
        )

    def ready(self) -> bool:
        return bool(
            not self.closed
            and not self.busy
            and self.fresh
            and self.snapshot
            and self.clock() < self.snapshot.options.deadline
        )

    def _begin(self, action: Action, payload: str = "{}") -> PolicyOperation | None:
        if self.closed or self.busy:
            return None
        self.sequence += 1
        self.pending = PolicyOperation(self.sequence, action, self.clock(), payload)
        self.fresh = False
        self.message = (
            "Loading permissions..."
            if action == "refresh"
            else ("Permissions request pending. Closing does not cancel this request.")
        )
        return self.pending

    def refresh(self) -> PolicyOperation | None:
        return self._begin("refresh")

    def review(
        self,
        action: Action,
        *,
        mode: str = "session",
        capture_home: bool = False,
        image_delivery: bool = False,
        ttl_text: str = "",
        attestation: str = "",
        acknowledge_effects: bool = False,
    ) -> PolicyReview | None:
        if not self.ready() or self.snapshot is None or action == "refresh":
            return None
        options, now = self.snapshot.options, self.clock()
        row, principal = self.selected_row(), options.principal(self.principal_id)
        payload: dict[str, Any] = {}
        if action == "revoke":
            if row is None or row.revoked:
                return None
            payload = {"policy_id": row.policy_id, "expected_revision": row.revision}
            explanation = (
                f"Revoke {row.policy_id}, revision {row.revision}?\n"
                f"Principal: {row.principal_id}\nTarget: {row.target}\n"
                "Already running USB work will safely finish. Future authorization is revoked."
            )
        else:
            if re.fullmatch(r"[0-9]{1,7}", ttl_text) is None:
                return None
            ttl = int(ttl_text)
            limit = options.max_arm if action == "arm" else options.max_ttl
            if not 1 <= ttl <= limit or principal is None:
                return None
            if not principal.permits(action == "arm" or capture_home, now):
                return None
            lease = options.lease
            if action == "arm":
                if (
                    options.backend != "serial"
                    or options.firmware is None
                    or lease is None
                    or lease.principal_id != principal.principal_id
                    or now >= lease.deadline
                    or attestation != HOME_ATTESTATION
                    or acknowledge_effects is not True
                ):
                    return None
                payload = {
                    "lease_id": lease.lease_id,
                    "expected_target": options.target,
                    "ttl_seconds": ttl,
                    "attestation": HOME_ATTESTATION,
                    "acknowledge_effects": True,
                }
            else:
                if (
                    type(capture_home) is not bool
                    or type(image_delivery) is not bool
                    or not (capture_home or image_delivery)
                    or mode not in ("session", "persistent")
                    or capture_home
                    and options.backend == "serial"
                    and options.firmware is None
                ):
                    return None
                if mode == "persistent" and principal.kind != "issued":
                    return None
                if mode == "session" and (
                    lease is None
                    or lease.principal_id != principal.principal_id
                    or lease.origin != principal.origin
                    or now >= lease.deadline
                ):
                    return None
                payload = {
                    "capture_home": capture_home,
                    "image_delivery": image_delivery,
                    "ttl_seconds": ttl,
                    "expected_target": options.target,
                }
                if action == "create":
                    if self.selected:
                        return None
                    payload.update(principal_id=principal.principal_id, mode=mode)
                    if mode == "session" and lease:
                        payload["expected_lease_id"] = lease.lease_id
                elif action == "update":
                    if (
                        row is None
                        or row.revoked
                        or not row.active
                        or now >= row.deadline
                        or row.target != options.target
                        or row.principal_id != principal.principal_id
                        or row.mode != mode
                        or mode == "session"
                        and (
                            lease is None
                            or lease.lease_id != row.lease_id
                            or row.broker_epoch != options.broker_epoch
                        )
                    ):
                        return None
                    payload.update(policy_id=row.policy_id, expected_revision=row.revision)
                else:
                    return None
            permissions = []
            if capture_home or action == "arm":
                permissions.append("capture_home")
            if image_delivery and action != "arm":
                permissions.append("image_delivery")
            explanation = (
                f"{action.upper()} for {principal.label}\nPrincipal: {principal.principal_id}\n"
                f"Origin: {principal.origin}; existing scopes: {', '.join(principal.scopes)}\n"
                f"Backend/device: {options.backend} / {options.device}\n"
                f"Target SHA-256: {options.target}\n"
                f"Firmware SHA-256: {options.firmware or 'none'}\n"
                f"USB selector: {options.selector_text}\n"
                f"Permission: {', '.join(permissions)}\n"
                f"Mode: {'current session arm' if action == 'arm' else mode}; "
                f"maximum {ttl} seconds\n"
                "Key expiry and session lifetime can shorten this duration.\n"
            )
            if row and action == "update":
                explanation += f"Rule: {row.policy_id}; expected revision: {row.revision}\n"
            if lease and (mode == "session" or action == "arm"):
                explanation += f"Lease: {lease.lease_id}\n"
            if capture_home or action == "arm":
                explanation += "\n" + CAPTURE_EFFECTS
            if image_delivery and action != "arm":
                explanation += "\n\n" + DELIVERY_EFFECTS
            if action == "arm" or capture_home and options.backend == "serial":
                explanation += "\n\n" + ARM_NOTICE
            explanation += "\n\n" + SESSION_NOTICE
        return PolicyReview(
            action,
            json.dumps(payload, sort_keys=True),
            explanation,
            self.form_revision,
            self.snapshot,
        )

    def commit(self, review: PolicyReview) -> PolicyOperation | None:
        if (
            not self.ready()
            or self.snapshot is not review.snapshot
            or self.form_revision != review.form_revision
        ):
            return None
        # Expiring keys, leases and policies are checked again after the modal loop.
        payload = json.loads(review.payload)
        if review.action == "revoke":
            current = self.review("revoke")
            if current is None or current.payload != review.payload:
                return None
        else:
            row = self.selected_row()
            fresh = self.review(
                review.action,
                mode=payload.get("mode", row.mode if row else "session"),
                capture_home=payload.get("capture_home", False),
                image_delivery=payload.get("image_delivery", False),
                ttl_text=str(payload["ttl_seconds"]),
                attestation=payload.get("attestation", ""),
                acknowledge_effects=payload.get("acknowledge_effects", False),
            )
            if fresh is None or fresh.payload != review.payload:
                return None
        if review.action in ("create", "update"):
            payload["acknowledge_effects"] = True
        return self._begin(review.action, json.dumps(payload, sort_keys=True))

    def accept(self, outcome: PolicyOutcome) -> bool:
        if self.closed or self.pending != outcome.operation:
            return False
        self.pending = None
        self.fresh = False
        if outcome.failed:
            self.message = (
                REQUEST_FAILED if outcome.operation.action == "refresh" else WRITE_UNCERTAIN
            )
        elif outcome.snapshot is not None:
            self.snapshot = outcome.snapshot
            self.fresh = True
            self.changed()
            self.message = (
                "Select a principal and bounded permissions, or inspect an existing rule."
            )
        else:
            self.changed()
            self.message = (
                "Change accepted. Refresh to inspect current revisions, expiry and audit."
            )
        return True

    def close(self) -> bool:
        if self.busy:
            self.message = PENDING_CLOSE
            return False
        self.closed, self.fresh = True, False
        return True


class PolicyWorker:
    def __init__(self, api: PolicyAPI) -> None:
        self.api = api
        self.commands: queue.Queue[PolicyOperation | None] = queue.Queue()
        self.results: queue.Queue[PolicyOutcome] = queue.Queue()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="cardputer-permissions", daemon=True)
        self.thread.start()

    def run(self) -> None:
        while not self.stopped.is_set():
            operation = self.commands.get()
            if operation is None or self.stopped.is_set():
                return
            self.results.put(perform_policy(self.api, operation))

    def stop(self) -> None:
        self.stopped.set()
        self.commands.put(None)


def utc_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "current session only"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


class PermissionsPanel:
    def __init__(self, root: tk.Tk, api: PolicyAPI) -> None:
        self.root = root
        self.controller = PolicyController()
        self.worker = PolicyWorker(api)
        self.dialog: tk.Toplevel | None = None
        self.updating = False
        self.rendered_snapshot: PolicySnapshot | None = None
        self.principal_choices: dict[str, str] = {}
        root.after(100, self._drain)

    def show(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        if self.controller.closed:
            return
        if self.dialog is not None:
            self.dialog.deiconify()
            self.dialog.lift()
            return
        self.dialog = dialog = tk.Toplevel(self.root)
        dialog.title("Cardputer Capture Permissions")
        dialog.transient(self.root)
        dialog.geometry("1100x950")
        dialog.minsize(860, 820)
        dialog.protocol("WM_DELETE_WINDOW", self._dismiss)
        outer = ttk.Frame(dialog, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(6, weight=1)
        ttk.Label(outer, text=MANUAL_NOTICE + "\n" + SESSION_NOTICE, wraplength=1040).grid(
            row=0, column=0, sticky="w"
        )
        bar = ttk.Frame(outer)
        bar.grid(row=1, sticky="ew", pady=8)
        self.refresh_button = ttk.Button(bar, text="Refresh permissions", command=self._refresh)
        self.refresh_button.pack(side="left")
        self.new_button = ttk.Button(bar, text="New permission", command=self._new)
        self.new_button.pack(side="left", padx=8)
        self.target_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.target_var).pack(side="left", padx=8)
        tree_box = ttk.Frame(outer)
        tree_box.grid(row=2, sticky="ew")
        tree_box.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_box,
            columns=("revision", "principal", "mode", "permissions", "expiry", "state"),
            show="headings",
            height=4,
            selectmode="browse",
        )
        for name, label, width in (
            ("revision", "Revision", 65),
            ("principal", "Principal", 285),
            ("mode", "Mode", 90),
            ("permissions", "Permissions", 190),
            ("expiry", "Expires (UTC)", 175),
            ("state", "State", 85),
        ):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=50)
        self.tree.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", self._selection)
        form = ttk.LabelFrame(outer, text="Explicitly selected permission", padding=8)
        form.grid(row=3, sticky="ew", pady=8)
        form.columnconfigure(1, weight=1)
        self.principal_var, self.mode_var, self.ttl_var = (
            tk.StringVar(),
            tk.StringVar(value="session"),
            tk.StringVar(),
        )
        self.capture_var, self.delivery_var = tk.BooleanVar(), tk.BooleanVar()
        ttk.Label(form, text="Existing principal").grid(row=0, column=0, sticky="w")
        self.principal_combo = ttk.Combobox(
            form, textvariable=self.principal_var, state="readonly", width=90
        )
        self.principal_combo.grid(row=0, column=1, columnspan=3, sticky="ew", padx=8)
        ttk.Label(form, text="Mode").grid(row=1, column=0, sticky="w", pady=5)
        self.mode_combo = ttk.Combobox(
            form,
            textvariable=self.mode_var,
            values=("session", "persistent"),
            state="readonly",
            width=15,
        )
        self.mode_combo.grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(form, text="Duration (seconds)").grid(row=1, column=2)
        self.ttl_entry = ttk.Entry(form, textvariable=self.ttl_var, width=12)
        self.ttl_entry.grid(row=1, column=3, padx=8)
        self.capture_check = ttk.Checkbutton(
            form,
            text="capture_home (existing read + control scopes required)",
            variable=self.capture_var,
        )
        self.capture_check.grid(row=2, column=0, columnspan=4, sticky="w")
        self.delivery_check = ttk.Checkbutton(
            form,
            text="image_delivery (screen pixels may be sent to the client's AI service)",
            variable=self.delivery_var,
        )
        self.delivery_check.grid(row=3, column=0, columnspan=4, sticky="w")
        self.principal_info_var = tk.StringVar()
        ttk.Label(form, textvariable=self.principal_info_var, wraplength=1010).grid(
            row=4, column=0, columnspan=4, sticky="w", pady=5
        )
        actions = ttk.Frame(form)
        actions.grid(row=5, column=0, columnspan=4, sticky="e")
        self.create_button = ttk.Button(
            actions, text="Create permission...", command=lambda: self._act("create")
        )
        self.update_button = ttk.Button(
            actions, text="Update selected rule...", command=lambda: self._act("update")
        )
        self.revoke_button = ttk.Button(
            actions, text="Revoke selected rule...", command=lambda: self._act("revoke")
        )
        for button in (self.create_button, self.update_button, self.revoke_button):
            button.pack(side="left", padx=4)
        arm = ttk.LabelFrame(outer, text="Serial: arm the current session separately", padding=8)
        arm.grid(row=4, sticky="ew")
        arm.columnconfigure(0, weight=1)
        ttk.Label(arm, text=CAPTURE_EFFECTS + "\n" + ARM_NOTICE, wraplength=1020).grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        self.arm_var = tk.StringVar()
        ttk.Label(arm, textvariable=self.arm_var).grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Label(arm, text="Type exactly: " + HOME_ATTESTATION).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 2)
        )
        self.attestation_var, self.arm_ttl_var = tk.StringVar(), tk.StringVar(value="300")
        self.ack_var = tk.BooleanVar()
        self.attestation_entry = ttk.Entry(arm, textvariable=self.attestation_var)
        self.attestation_entry.grid(row=3, column=0, sticky="ew")
        ttk.Label(arm, text="Seconds").grid(row=3, column=1, padx=8)
        self.arm_ttl_entry = ttk.Entry(arm, textvariable=self.arm_ttl_var, width=10)
        self.arm_ttl_entry.grid(row=3, column=2)
        self.ack_check = ttk.Checkbutton(
            arm,
            text="I acknowledge Esc, app exit, unsaved-state loss and serial reset effects",
            variable=self.ack_var,
        )
        self.ack_check.grid(row=4, column=0, columnspan=2, sticky="w", pady=6)
        self.arm_button = ttk.Button(
            arm, text="Arm current session...", command=lambda: self._act("arm")
        )
        self.arm_button.grid(row=4, column=2)
        ttk.Label(outer, text="Selected rule / target details and recent audit (UTC)").grid(
            row=5, sticky="w", pady=(8, 3)
        )
        detail_box = ttk.Frame(outer)
        detail_box.grid(row=6, sticky="nsew")
        detail_box.columnconfigure(0, weight=1)
        detail_box.rowconfigure(0, weight=1)
        self.details = tk.Text(detail_box, height=7, wrap="word", state="disabled")
        self.details.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(detail_box, orient="vertical", command=self.details.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=detail_scroll.set)
        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var, wraplength=1030).grid(
            row=7, sticky="w", pady=8
        )
        ttk.Button(outer, text="Close", command=self._dismiss).grid(row=8, sticky="e")
        for variable in (
            self.principal_var,
            self.mode_var,
            self.ttl_var,
            self.capture_var,
            self.delivery_var,
            self.attestation_var,
            self.arm_ttl_var,
            self.ack_var,
        ):
            variable.trace_add("write", self._changed)
        self.rendered_snapshot = None
        self._new()
        self._refresh()

    def _submit(self, operation: PolicyOperation | None) -> None:
        if operation is not None:
            self.worker.commands.put(operation)
        self._render()

    def _refresh(self) -> None:
        self._submit(self.controller.refresh())

    def _changed(self, *_args: str) -> None:
        if self.updating:
            return
        self.controller.select_principal(self.principal_choices.get(self.principal_var.get(), ""))
        self.controller.changed()
        self._controls()

    def _new(self) -> None:
        if self.controller.busy:
            return
        self.updating = True
        try:
            self.tree.selection_remove(*self.tree.selection())
            self.controller.select("")
            self.controller.select_principal("")
            self.principal_var.set("")
            self.mode_var.set("session")
            self.ttl_var.set("")
            self.capture_var.set(False)
            self.delivery_var.set(False)
            self.attestation_var.set("")
            self.ack_var.set(False)
            self.controller.changed()
        finally:
            self.updating = False
        self._render()

    def _selection(self, _event: Any = None) -> None:
        if self.updating:
            return
        selected = self.tree.selection()
        self.controller.select(selected[0] if selected else "")
        row = self.controller.selected_row()
        if row:
            self.updating = True
            try:
                self.controller.select_principal(row.principal_id)
                self.principal_var.set(
                    next(
                        (
                            label
                            for label, identifier in self.principal_choices.items()
                            if identifier == row.principal_id
                        ),
                        "",
                    )
                )
                self.mode_var.set(row.mode)
                self.capture_var.set(row.capture_home)
                self.delivery_var.set(row.image_delivery)
                self.ttl_var.set("")
                self.attestation_var.set("")
                self.ack_var.set(False)
                self.controller.changed()
            finally:
                self.updating = False
        self._render()

    def _review(self, action: Action) -> PolicyReview | None:
        return self.controller.review(
            action,
            mode=self.mode_var.get(),
            capture_home=self.capture_var.get(),
            image_delivery=self.delivery_var.get(),
            ttl_text=self.arm_ttl_var.get() if action == "arm" else self.ttl_var.get(),
            attestation=self.attestation_var.get(),
            acknowledge_effects=self.ack_var.get(),
        )

    def _act(self, action: Action) -> None:
        from tkinter import messagebox

        review = self._review(action)
        if review is not None and messagebox.askyesno(
            "Confirm capture permission change",
            review.explanation,
            parent=self.dialog or self.root,
            default="no",
        ):
            self._submit(self.controller.commit(review))

    def _controls(self) -> None:
        if self.dialog is None:
            return
        model = self.controller
        self.refresh_button.configure(state="disabled" if model.busy else "normal")
        self.new_button.configure(state="disabled" if model.busy else "normal")
        for button, action in (
            (self.create_button, "create"),
            (self.update_button, "update"),
            (self.revoke_button, "revoke"),
            (self.arm_button, "arm"),
        ):
            button.configure(
                state="normal" if self._review(cast(Action, action)) is not None else "disabled"
            )
        self.principal_combo.configure(
            state="disabled" if model.busy or model.selected else "readonly"
        )
        self.mode_combo.configure(state="disabled" if model.busy or model.selected else "readonly")
        for widget in (self.ttl_entry, self.capture_check, self.delivery_check):
            widget.configure(state="disabled" if model.busy else "normal")
        principal = model.snapshot.options.principal(model.principal_id) if model.snapshot else None
        self.principal_info_var.set(
            f"{principal.kind}; scopes: {', '.join(principal.scopes)}; "
            f"key expires: {utc_time(principal.expires_at)}"
            if principal
            else "Select an existing principal. Opening this window creates no keys or permissions."
        )
        self.status_var.set(
            "Permission review expired. Refresh before making a change."
            if model.fresh and not model.ready() and not model.busy
            else model.message
        )

    def _render(self) -> None:
        if self.dialog is None:
            return
        model = self.controller
        snapshot = model.snapshot
        self.updating = True
        try:
            if snapshot is not self.rendered_snapshot:
                self.rendered_snapshot = snapshot
                children = self.tree.get_children()
                if children:
                    self.tree.delete(*children)
                self.principal_choices = (
                    {
                        f"{principal.label} | {principal.principal_id}": principal.principal_id
                        for principal in snapshot.options.principals
                    }
                    if snapshot
                    else {}
                )
                self.principal_combo.configure(values=tuple(self.principal_choices))
                for row in snapshot.policies if snapshot else ():
                    permissions = ", ".join(
                        name for name in ("capture_home", "image_delivery") if getattr(row, name)
                    )
                    state = (
                        "revoked"
                        if row.revoked
                        else "active"
                        if row.active and model.clock() < row.deadline
                        else "inactive"
                    )
                    self.tree.insert(
                        "",
                        "end",
                        iid=row.policy_id,
                        values=(
                            row.revision,
                            row.principal_id,
                            row.mode,
                            permissions,
                            utc_time(row.expires_at),
                            state,
                        ),
                    )
                if model.selected and self.tree.exists(model.selected):
                    self.tree.selection_set(model.selected)
                self.attestation_var.set("")
                self.ack_var.set(False)
            if snapshot:
                options = snapshot.options
                self.target_var.set(f"{options.backend} | {options.device}")
                self.arm_var.set(options.arm_text)
                text = (
                    f"Target SHA-256: {options.target}\n"
                    f"Firmware SHA-256: {options.firmware or 'none'}\n"
                    f"USB selector: {options.selector_text}\n"
                    f"Maximum permission duration: {options.max_ttl} s; "
                    f"maximum arm: {options.max_arm} s\n"
                )
                selected_row = model.selected_row()
                if selected_row:
                    fields = asdict(selected_row)
                    fields.pop("deadline")
                    text += "\nSelected rule:\n" + json.dumps(fields, ensure_ascii=True, indent=2)
                text += "\n\nRecent audit:\n" + snapshot.audit_text
            else:
                text = "Refresh to view current permission options and audit."
            self.details.configure(state="normal")
            self.details.delete("1.0", "end")
            self.details.insert("1.0", text)
            self.details.configure(state="disabled")
        finally:
            self.updating = False
        self._controls()

    def _drain(self) -> None:
        if self.worker.stopped.is_set():
            return
        try:
            while True:
                if self.controller.accept(self.worker.results.get_nowait()):
                    self._render()
        except queue.Empty:
            pass
        self._controls()
        self.root.after(100, self._drain)

    def can_close_window(self) -> bool:
        if self.controller.busy:
            self.controller.message = PENDING_CLOSE
            self.show()
            self._render()
            return False
        return True

    def _dismiss(self) -> None:
        if self.dialog is not None and self.can_close_window():
            self.controller.fresh = False
            self.dialog.destroy()
            self.dialog = None

    def stop(self) -> bool:
        if not self.controller.close():
            self._render()
            return False
        self.worker.stop()
        return True
