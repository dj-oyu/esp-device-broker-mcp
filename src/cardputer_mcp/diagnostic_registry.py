"""Closed diagnostic source policy; adding entries requires reviewed code changes.

Entry digests describe admission policy, not source bytes or execution authority.
SourceSnapshots and the durable build/flash chain still verify source identity.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DiagnosticEntry:
    repository: str
    commit: str
    contract: str = "j1-fixed-v1"
    modes: tuple[str, ...] = ("ON", "OFF")
    dma_modes: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        descriptor = {
            "schema_version": 1,
            "repository": self.repository,
            "commit": self.commit,
            "contract": self.contract,
            "modes": self.modes,
            "idf_version": "6.0.1",
            "target": "esp32s3",
            "detail_option": "POCKET_DERBY_DETAIL_MEASURE",
        }
        if self.dma_modes:
            descriptor["dma_modes"] = self.dma_modes
            descriptor["dma_option"] = "KASANE_DMA_OVERLAP"
        raw = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


J1_COMMIT = "b561aea1c0798feb28055a00d36681fae81ac58a"
J1_REPOSITORY = "dj-oyu/cardputer-adv-pocketjs"
_J1_ENTRY = DiagnosticEntry(J1_REPOSITORY, J1_COMMIT)
_ENTRIES = (_J1_ENTRY,)


def diagnostic_entry(provenance: dict[str, Any]) -> DiagnosticEntry:
    """Resolve only an exact approved public source, never a branch or descendant."""
    if not isinstance(provenance, dict):
        raise ValueError("diagnostic_mode_requires_allowlisted_source")
    repository = provenance.get("repository")
    if not isinstance(repository, str):
        raise ValueError("diagnostic_mode_requires_allowlisted_source")
    matches = [
        entry
        for entry in _ENTRIES
        if repository.casefold() == entry.repository and provenance.get("commit") == entry.commit
    ]
    if len(matches) != 1 or not (
        matches[0].modes == ("ON", "OFF")
        and (
            (matches[0].contract == "j1-fixed-v1" and not matches[0].dma_modes)
            or (
                matches[0].contract == "derby-dma-diagnostic-v1"
                and matches[0].dma_modes == ("ON", "OFF")
            )
        )
    ):
        raise ValueError("diagnostic_mode_requires_allowlisted_source")
    return matches[0]


def validate_entry_binding(provenance: dict[str, Any], record: dict[str, Any]) -> str:
    """Legacy J1 evidence may omit the digest; a present digest must match exactly."""
    entry = diagnostic_entry(provenance)
    if "diagnostic_entry_sha256" in record:
        if record["diagnostic_entry_sha256"] != entry.sha256:
            raise ValueError("diagnostic_entry_binding_invalid")
    elif entry != _J1_ENTRY:
        raise ValueError("diagnostic_entry_binding_required")
    return entry.sha256


def same_entry_binding(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Keep absent legacy fields distinct from corrupt explicit null fields."""
    key = "diagnostic_entry_sha256"
    if key not in left and key not in right:
        return True
    value = left.get(key)
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-f0-9]{64}", value) is not None
        and key in right
        and value == right[key]
    )


def validate_diagnostic_selection(
    provenance: dict[str, Any], detail_mode: Any, dma_mode: Any
) -> DiagnosticEntry | None:
    """Require both explicit axes for DMA entries; never infer or downgrade a lane."""
    try:
        entry = diagnostic_entry(provenance)
    except ValueError:
        if detail_mode is not None or dma_mode is not None or any(
            isinstance(provenance, dict)
            and isinstance(provenance.get("repository"), str)
            and provenance["repository"].casefold() == candidate.repository
            and provenance.get("commit") == candidate.commit
            for candidate in _ENTRIES
        ):
            raise
        return None
    if entry.contract == "derby-dma-diagnostic-v1":
        if (
            not isinstance(detail_mode, str)
            or detail_mode not in entry.modes
            or not isinstance(dma_mode, str)
            or dma_mode not in entry.dma_modes
        ):
            raise ValueError("diagnostic_axes_required")
    elif dma_mode is not None or (detail_mode is not None and detail_mode not in entry.modes):
        raise ValueError("diagnostic_mode_requires_allowlisted_source")
    return entry if detail_mode is not None else None


def same_diagnostic_selection(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Require matching axis presence and value, keeping explicit null invalid."""
    for key in ("derby_detail_mode", "derby_dma_mode"):
        if key not in left and key not in right:
            continue
        value = left.get(key)
        if not isinstance(value, str) or value not in ("ON", "OFF") or right.get(key) != value:
            return False
    return same_entry_binding(left, right)


def validate_diagnostic_record(
    provenance: dict[str, Any], record: dict[str, Any]
) -> DiagnosticEntry | None:
    """Validate raw optional-field presence before resolving the selected contract."""
    if any(
        key in record and record[key] is None
        for key in ("derby_detail_mode", "derby_dma_mode")
    ):
        raise ValueError("diagnostic_axes_required")
    return validate_diagnostic_selection(
        provenance, record.get("derby_detail_mode"), record.get("derby_dma_mode")
    )
