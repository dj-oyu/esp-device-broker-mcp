"""Bounded DERBY request provenance, never an installed-firmware identity claim.

The loop-owned database contains manifest *references*, not the manifest and
snapshot proofs needed to verify a full source/build/flash receipt chain. This
first slice therefore reports that association as missing or ambiguous. It never
chooses a receipt from an app hash, reads artifact bytes, or invokes authority.
"""

import hashlib
import json
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from .broker import Broker

MAX_SCOPE_BYTES = 8192
_MAX_PLAN_BYTES = 16 * 1024
_ID = r"^[A-Za-z0-9_-]{1,64}$"
_PRINCIPAL = r"^[A-Za-z0-9_:.@+-]{1,128}$"
_HEX = r"^[a-f0-9]{64}$"
_EPOCH = r"^[a-f0-9]{32}$"
Identifier = Annotated[str, Field(pattern=_ID, max_length=64)]
Principal = Annotated[str, Field(pattern=_PRINCIPAL, max_length=128)]
Digest = Annotated[str, Field(pattern=_HEX, max_length=64)]
Epoch = Annotated[str, Field(pattern=_EPOCH, max_length=32)]
Reason = Literal[
    "core_binding_unavailable",
    "history_unavailable",
    "configured_firmware_mismatch",
    "registered_device_not_bound",
    "registered_selector_ambiguous",
    "durable_receipt_chain_unavailable",
    "provenance_changed",
]


class DerbyAssociation(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    status: Literal["missing", "ambiguous"] = "missing"
    reason: Reason

    @model_validator(mode="after")
    def consistent_status(self) -> "DerbyAssociation":
        if (self.status == "ambiguous") != (self.reason == "registered_selector_ambiguous"):
            raise ValueError("invalid_derby_provenance")
        return self


class DerbyProvenance(BaseModel):
    """Only fixed status text, bounded public IDs and digests may be persisted."""

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )
    schema_version: int = Field(default=1, strict=True, ge=1, le=1)
    binding_status: Literal["captured", "missing", "changed"] = "missing"
    job_id: Identifier | None = None
    broker_epoch: Epoch | None = None
    plan_hash: Digest | None = None
    principal_id: Principal | None = None
    device_fingerprint: Digest | None = None
    configured_expected_firmware_sha256: Digest | None = None
    serial_configuration_sha256: Digest | None = None
    association: DerbyAssociation

    @model_validator(mode="after")
    def bounded_scope(self) -> "DerbyProvenance":
        if self.binding_status == "captured" and any(
            value is None
            for value in (
                self.job_id,
                self.broker_epoch,
                self.plan_hash,
                self.principal_id,
                self.device_fingerprint,
                self.configured_expected_firmware_sha256,
                self.serial_configuration_sha256,
            )
        ):
            raise ValueError("invalid_derby_provenance")
        if (self.binding_status == "changed") != (self.association.reason == "provenance_changed"):
            raise ValueError("invalid_derby_provenance")
        if len(self.model_dump_json(exclude_none=True).encode()) > MAX_SCOPE_BYTES:
            raise ValueError("invalid_derby_provenance")
        return self


def _missing(reason: Reason) -> DerbyProvenance:
    return DerbyProvenance(association=DerbyAssociation(reason=reason))


def _matches(value: Any, pattern: str) -> bool:
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _digest(plan: dict[str, Any]) -> str:
    # This verifies bounded durable JSON identity, never firmware/artifact bytes.
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    if len(canonical) > _MAX_PLAN_BYTES:
        raise ValueError("invalid_derby_provenance")
    return hashlib.sha256(canonical).hexdigest()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_derby_provenance")
        result[key] = value
    return result


def capture_provenance(
    broker: "Broker", plan: dict[str, Any], job_id: str, principal: str
) -> DerbyProvenance:
    """Capture immutable request bounds on the broker loop; failure is diagnostic only.

    A persisted explicit request must bind this principal to this exact job/plan.
    No current auth grant is inferred or checked here. The caller retains all
    existing admission, dispatch and final authority checks independently.
    """
    try:
        if (
            type(plan) is not dict
            or not _matches(job_id, _ID)
            or not _matches(principal, _PRINCIPAL)
            or plan.get("job_id") != job_id
            or plan.get("test") != "derby_smoke"
            or plan.get("backend") != "serial"
            or not _matches(plan.get("broker_epoch"), _EPOCH)
            or plan["broker_epoch"] != broker.epoch
            or not _matches(plan.get("device_fingerprint"), _HEX)
            or not _matches(plan.get("expected_firmware_sha256"), _HEX)
            or plan.get("principal_id", principal) != principal
        ):
            return _missing("core_binding_unavailable")
        digest = _digest(plan)
    except Exception:
        return _missing("core_binding_unavailable")
    try:
        db = broker._store().db
        job = db.execute(
            "SELECT j.plan_hash,r.plan_hash,r.principal_id FROM jobs j "
            "JOIN execution_requests r ON r.job_id=j.id WHERE j.id=? "
            "AND length(j.plan_hash)=64 AND length(r.plan_hash)=64 "
            "AND length(r.principal_id)<=128 LIMIT 1",
            (job_id,),
        ).fetchone()
        saved = db.execute(
            "SELECT payload FROM plans WHERE hash=? AND length(CAST(payload AS BLOB))<=? LIMIT 1",
            (digest, _MAX_PLAN_BYTES),
        ).fetchone()
        if job is None or saved is None or tuple(job) != (digest, digest, principal):
            return _missing("core_binding_unavailable")
        durable = json.loads(saved[0], object_pairs_hook=_unique)
        if type(durable) is not dict or durable != plan or _digest(durable) != digest:
            return _missing("core_binding_unavailable")
    except Exception:
        return _missing("history_unavailable")
    try:
        serial = broker.settings.serial
        if serial is None or serial.expected_firmware_sha256 != plan["expected_firmware_sha256"]:
            return _missing("configured_firmware_mismatch")
        candidates = [
            registration
            for registration in broker.settings.flash_devices.values()
            if registration.selector == serial.selector
        ]
        if not candidates:
            association = DerbyAssociation(reason="registered_device_not_bound")
        elif len(candidates) > 1:
            association = DerbyAssociation(
                status="ambiguous", reason="registered_selector_ambiguous"
            )
        else:
            # Exact selector equality is only a candidate registration. Without
            # the filesystem manifest/snapshot proofs even a matching configured
            # app hash and a current LKG row cannot establish the receipt chain.
            association = DerbyAssociation(reason="durable_receipt_chain_unavailable")
        return DerbyProvenance(
            binding_status="captured",
            job_id=job_id,
            broker_epoch=plan["broker_epoch"],
            plan_hash=digest,
            principal_id=principal,
            device_fingerprint=plan["device_fingerprint"],
            configured_expected_firmware_sha256=serial.expected_firmware_sha256,
            serial_configuration_sha256=hashlib.sha256(
                json.dumps(
                    serial.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
            association=association,
        )
    except Exception:
        return _missing("core_binding_unavailable")


def finish_provenance(
    before: DerbyProvenance | dict[str, Any], after: DerbyProvenance | dict[str, Any]
) -> DerbyProvenance:
    """Freeze the dispatch bounds; never relink an observation to later receipts."""
    try:
        initial = DerbyProvenance.model_validate(before)
    except Exception:
        return _missing("core_binding_unavailable")
    try:
        final = DerbyProvenance.model_validate(after)
    except Exception:
        final = None
    if initial == final:
        return initial
    return DerbyProvenance.model_validate(
        {
            **initial.model_dump(mode="json"),
            "binding_status": "changed",
            "association": {"status": "missing", "reason": "provenance_changed"},
        }
    )
