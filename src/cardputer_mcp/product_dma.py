"""Ordinary copied-DMA build evidence, independent of diagnostic proof producers."""

import hashlib
import struct
from pathlib import Path
from typing import Any

from .diagnostic_dma_modeproof import _dma_functions
from .diagnostic_modeproof import _BIN, _ELF, _LIMITS, _canonical, _read
from .diagnostic_registry import validate_source_identity

_ERROR = "ordinary_dma_output_invalid"
CONTRACT = "kasane-dma-overlap-build-v1"


def validate_dma_record(provenance: dict[str, Any], record: dict[str, Any]) -> None:
    """Explicit ordinary selection requires immutable source identity and no recipe axes."""
    if "dma_overlap_mode" not in record:
        if "dma_overlap_evidence" in record:
            raise ValueError(_ERROR)
        return
    mode = record["dma_overlap_mode"]
    if (
        type(mode) is not str
        or mode not in ("OFF", "ON")
        or any(
            key in record
            for key in ("derby_detail_mode", "derby_dma_mode", "diagnostic_entry_sha256")
        )
    ):
        raise ValueError("ordinary_dma_requires_source_build_without_diagnostics")
    validate_source_identity(provenance)
    if not provenance.get("source_receipt"):
        raise ValueError("ordinary_dma_requires_source_build_without_diagnostics")


def same_dma_selection(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if "dma_overlap_mode" not in left and "dma_overlap_mode" not in right:
        return "dma_overlap_evidence" not in left and "dma_overlap_evidence" not in right
    mode = left.get("dma_overlap_mode")
    return type(mode) is str and mode in ("OFF", "ON") and right.get("dma_overlap_mode") == mode


def collect_dma_evidence(
    root: Path, mode: str, provenance: dict[str, Any], artifacts: list[dict[str, Any]]
) -> dict[str, Any]:
    """Check actual CMake output and linked BIN/ELF; retain hashes, never private cache text.

    This establishes build correspondence only. It makes no hardware, telemetry,
    fixed workload, seed or performance claim and produces no diagnostic proof.
    """
    try:
        validate_dma_record(provenance, {"dma_overlap_mode": mode})
        cache = _read(root, "CMakeCache.txt", 128 * 1024)
        text = cache.decode("utf-8")
        lines = [
            line
            for line in text.splitlines()
            if line.startswith("KASANE_DMA_OVERLAP:") or line.startswith("KASANE_DMA_OVERLAP=")
        ]
        if "\0" in text or lines != [f"KASANE_DMA_OVERLAP:BOOL={mode}"]:
            raise ValueError(_ERROR)
        files = {name: _read(root, name, limit) for name, limit in _LIMITS.items()}
        receipts = {}
        for name, raw in files.items():
            receipt = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            matching = [item for item in artifacts if item.get("name") == name]
            if len(matching) != 1 or any(matching[0].get(k) != v for k, v in receipt.items()):
                raise ValueError(_ERROR)
            receipts[name] = receipt
        image, elf = files[_BIN], files[_ELF]
        symbols = {"board_present_submit", "board_present_finish"} if mode == "ON" else set()
        if (
            _dma_functions(elf) != symbols
            or len(image) < 288
            or image[0] != 0xE9
            or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432
            or image[144:176].split(b"\0")[0] != b"v6.0.1"
            or image[176:208] != hashlib.sha256(elf).digest()
        ):
            raise ValueError(_ERROR)
        return {
            "contract": CONTRACT,
            "mode": mode,
            "source_provenance_sha256": hashlib.sha256(_canonical(provenance)).hexdigest(),
            "cmake_cache_sha256": hashlib.sha256(cache).hexdigest(),
            "native_symbols": sorted(symbols),
            "artifacts": receipts,
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError, struct.error, OverflowError):
        raise ValueError(_ERROR) from None
