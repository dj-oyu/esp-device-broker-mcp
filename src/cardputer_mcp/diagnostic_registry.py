"""Compatibility facade for diagnostic recipe selection and historical hashes.

There is no production source-commit admission list. The existing wire digest
binds source identity and selected contract; it does not certify FW quality.
Historical descriptor encoding lives in diagnostic_legacy, outside inner rules.

Supported built-in recipe contracts, not a verified-firmware catalogue.
U1/U3/U4, I5/I9/I10: selection is explicit; actual image/proof correspondence
is checked by the recipe adapter. Firmware CI owns quality. No CI API or
dynamic code loader participates in runtime selection.
"""

import re
from dataclasses import dataclass
from typing import Any

from .diagnostic_legacy import J1_COMMIT as J1_COMMIT
from .diagnostic_legacy import J1_REPOSITORY as J1_REPOSITORY
from .diagnostic_legacy import LegacyDiagnosticDescriptor as DiagnosticEntry
from .diagnostic_legacy import historical_binding_digest


@dataclass(frozen=True)
class DiagnosticContract:
    identifier: str
    modes: tuple[str, ...] = ("ON", "OFF")
    dma_modes: tuple[str, ...] = ()


J1 = DiagnosticContract("j1-fixed-v1")
DMA = DiagnosticContract("derby-dma-diagnostic-v1", dma_modes=("ON", "OFF"))


def known_contract(identifier: str) -> DiagnosticContract:
    if identifier == J1.identifier:
        return J1
    if identifier == DMA.identifier:
        return DMA
    raise ValueError("diagnostic_contract_unsupported")


def select_contract(detail_mode: Any, dma_mode: Any) -> DiagnosticContract | None:
    if detail_mode is None and dma_mode is None:
        return None
    if type(detail_mode) is not str or detail_mode not in J1.modes:
        raise ValueError("diagnostic_axes_required")
    if dma_mode is None:
        return J1
    if type(dma_mode) is not str or dma_mode not in DMA.dma_modes:
        raise ValueError("diagnostic_axes_required")
    return DMA


def validate_source_identity(provenance: dict[str, Any]) -> tuple[str, str]:
    """Check identity syntax; registration and source bytes are checked outside.

    Neither a repository string nor a full commit is an execution grant.
    """
    if not isinstance(provenance, dict):
        raise ValueError("diagnostic_source_identity_required")
    repository, commit = provenance.get("repository"), provenance.get("commit")
    if (
        type(repository) is not str
        or re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository) is None
        or any(part in (".", "..") for part in repository.split("/"))
        or type(commit) is not str
        or re.fullmatch(r"[a-f0-9]{40}", commit) is None
    ):
        raise ValueError("diagnostic_source_identity_required")
    return repository.casefold(), commit


def diagnostic_entry(
    provenance: dict[str, Any], *, contract: str = "j1-fixed-v1"
) -> DiagnosticEntry:
    """Reconstruct an identity descriptor for a supported recipe, without membership checks."""
    recipe = known_contract(contract)
    repository, commit = validate_source_identity(provenance)
    return DiagnosticEntry(repository, commit, recipe.identifier, recipe.modes, recipe.dma_modes)


def validate_entry_binding(provenance: dict[str, Any], record: dict[str, Any]) -> str:
    """Keep old digest bytes and omitted-J1 compatibility; accept no corrupt fallback."""
    if any(
        key in record and record[key] is None for key in ("derby_detail_mode", "derby_dma_mode")
    ):
        raise ValueError("diagnostic_axes_required")
    recipe = select_contract(record.get("derby_detail_mode"), record.get("derby_dma_mode"))
    entry = diagnostic_entry(provenance, contract=(recipe or J1).identifier)
    return historical_binding_digest(entry, record)


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
    """Ordinary builds are independent of diagnostic recipes and CI status."""
    recipe = select_contract(detail_mode, dma_mode)
    return diagnostic_entry(provenance, contract=recipe.identifier) if recipe else None


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
        key in record and record[key] is None for key in ("derby_detail_mode", "derby_dma_mode")
    ):
        raise ValueError("diagnostic_axes_required")
    return validate_diagnostic_selection(
        provenance, record.get("derby_detail_mode"), record.get("derby_dma_mode")
    )
