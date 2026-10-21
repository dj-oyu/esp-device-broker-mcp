"""Supported built-in recipe contracts, not a verified-firmware catalogue.

U1/U3/U4, I5/I9/I10: selection is explicit; actual image/proof correspondence
is checked by the recipe adapter. Firmware CI owns quality. No CI API or
dynamic code loader participates in runtime selection.
"""

import re
from dataclasses import dataclass
from typing import Any


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
