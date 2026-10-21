"""Compatibility facade for diagnostic recipe selection and historical hashes.

There is no production source-commit admission list. The existing wire digest
binds source identity and selected contract; it does not certify FW quality.
Historical descriptor encoding lives in diagnostic_legacy, outside inner rules.
"""

import re
from typing import Any

from .diagnostic_contracts import J1, known_contract, select_contract, validate_source_identity
from .diagnostic_legacy import J1_COMMIT as J1_COMMIT
from .diagnostic_legacy import J1_REPOSITORY as J1_REPOSITORY
from .diagnostic_legacy import LegacyDiagnosticDescriptor as DiagnosticEntry
from .diagnostic_legacy import historical_binding_digest


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
