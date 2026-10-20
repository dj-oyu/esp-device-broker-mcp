"""Separate two-axis DMA proof contract; no source admission or execution authority."""

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

from .diagnostic_modeproof import _ELF, _LIMITS, _canonical, _read, _unique
from .diagnostic_modeproof import _verify_modeproof_bytes as _verify_detail
from .diagnostic_registry import diagnostic_entry

CONTRACT = "derby-dma-diagnostic-v1"
PROOF_NAME = "dma-diagnostic-mode-proof.json"
MAX_CACHE_BYTES = 128 * 1024
MAX_PROOF_BYTES = 32768
_SYMBOLS = frozenset({"board_present_submit", "board_present_finish"})
_ERROR = "diagnostic_dma_modeproof_invalid"


def _dma_functions(elf: bytes) -> set[str]:
    """Read bounded ELF32 defined STT_FUNC entries, rejecting ambiguous/malformed targets."""
    if not 52 <= len(elf) <= _LIMITS[_ELF] or elf[:6] != b"\x7fELF\x01\x01":
        raise ValueError(_ERROR)
    shoff = struct.unpack_from("<I", elf, 32)[0]
    stride, count = struct.unpack_from("<HH", elf, 46)
    if stride < 40 or not 0 < count <= 4096 or shoff + stride * count > len(elf):
        raise ValueError(_ERROR)
    sections = [struct.unpack_from("<10I", elf, shoff + i * stride) for i in range(count)]

    def area(offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > len(elf):
            raise ValueError(_ERROR)
        return elf[offset : offset + size]

    found: set[str] = set()
    total = 0
    for section in sections:
        if section[1] != 2:
            continue
        link, symbol_stride = section[6], section[9]
        if link >= count or symbol_stride < 16 or section[5] % symbol_stride:
            raise ValueError(_ERROR)
        total += section[5] // symbol_stride
        if total > 262144:
            raise ValueError(_ERROR)
        names_section = sections[link]
        if names_section[1] != 3:
            raise ValueError(_ERROR)
        names = area(names_section[4], names_section[5])
        symbols = area(section[4], section[5])
        for pos in range(0, len(symbols), symbol_stride):
            name, value, size, info, _, index = struct.unpack_from("<IIIBBH", symbols, pos)
            if name >= len(names):
                raise ValueError(_ERROR)
            for wanted in _SYMBOLS:
                target_name = wanted.encode() + b"\0"
                if names[name : name + len(target_name)] != target_name:
                    continue
                if wanted in found or info & 15 != 2 or not 0 < index < count or size == 0:
                    raise ValueError(_ERROR)
                target = sections[index]
                delta = value - target[3]
                # Native linked functions must occupy executable initialized image bytes.
                if target[1] != 1 or target[2] & 4 == 0 or delta < 0 or delta + size > target[5]:
                    raise ValueError(_ERROR)
                area(target[4] + delta, size)
                found.add(wanted)
    return found


def _cache_axes(cache: str, detail_mode: str, dma_mode: str) -> bytes:
    if type(cache) is not str or "\0" in cache:
        raise ValueError(_ERROR)
    raw = cache.encode("utf-8")
    if not 0 < len(raw) <= MAX_CACHE_BYTES:
        raise ValueError(_ERROR)
    for key, requested in (
        ("POCKET_DERBY_DETAIL_MEASURE", detail_mode),
        ("KASANE_DMA_OVERLAP", dma_mode),
    ):
        lines = [
            line
            for line in cache.splitlines()
            if line.startswith(key + ":") or line.startswith(key + "=")
        ]
        if lines != [f"{key}:BOOL={requested}"]:
            raise ValueError(_ERROR)
    return raw


def verify_dma_modeproof_bytes(
    envelope: dict[str, Any],
    provenance: dict[str, Any],
    artifacts: list[dict[str, Any]],
    files: dict[str, bytes],
) -> None:
    """Verify retained exact image bytes and cache evidence after build-cache eviction."""
    try:
        entry = diagnostic_entry(provenance)
        if (
            entry.contract != CONTRACT
            or set(envelope)
            != {"requested_mode", "requested_dma_mode", "proof", "proof_sha256", "cmake_cache"}
            or type(envelope["requested_mode"]) is not str
            or type(envelope["requested_dma_mode"]) is not str
            or envelope["requested_mode"] not in entry.modes
            or envelope["requested_dma_mode"] not in entry.dma_modes
        ):
            raise ValueError(_ERROR)
        detail_mode, dma_mode = envelope["requested_mode"], envelope["requested_dma_mode"]
        cache = _cache_axes(envelope["cmake_cache"], detail_mode, dma_mode)
        proof = envelope["proof"]
        canonical = _canonical(proof)
        axes = {"detail_measurement": detail_mode, "dma_overlap": dma_mode}
        symbols = sorted(_SYMBOLS) if dma_mode == "ON" else []
        if (
            len(canonical) > MAX_PROOF_BYTES
            or hashlib.sha256(canonical).hexdigest() != envelope["proof_sha256"]
            or set(proof)
            != {
                "schema_version",
                "proof_contract",
                "requested_axes",
                "verified_axes",
                "dma_overlap",
                "diagnostic",
                "artifacts",
                "hardware_tested",
            }
            or type(proof["schema_version"]) is not int
            or proof["schema_version"] != 1
            or proof["proof_contract"] != CONTRACT
            or proof["hardware_tested"] is not False
            or proof["requested_axes"] != axes
            or proof["verified_axes"] != axes
            or proof["dma_overlap"]
            != {
                "mode": dma_mode,
                "symbol_contract": "board-present-submit-finish-v1",
                "native_symbols": symbols,
                "cmake_cache_sha256": hashlib.sha256(cache).hexdigest(),
            }
            or proof["artifacts"] != proof["diagnostic"]["artifacts"]
            or proof["diagnostic"]["build_mode"]["cmake_cache_sha256"]
            != hashlib.sha256(cache).hexdigest()
            or proof["diagnostic"]["build_measure"]["cmake_cache_sha256"]
            != hashlib.sha256(cache).hexdigest()
            or _dma_functions(files[_ELF]) != set(symbols)
        ):
            raise ValueError(_ERROR)
        nested = proof["diagnostic"]
        _verify_detail(
            {
                "requested_mode": detail_mode,
                "proof": nested,
                "proof_sha256": hashlib.sha256(_canonical(nested)).hexdigest(),
            },
            provenance,
            artifacts,
            files,
            contract=CONTRACT,
        )
    except (
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
        struct.error,
        OverflowError,
        RecursionError,
        UnicodeError,
    ):
        raise ValueError(_ERROR) from None


def collect_dma_modeproof(
    root: Path,
    detail_mode: str,
    dma_mode: str,
    provenance: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Collect only the separate producer proof; never fall back to J1 detail evidence."""
    try:
        proof = json.loads(_read(root, PROOF_NAME, MAX_PROOF_BYTES), object_pairs_hook=_unique)
        envelope = {
            "requested_mode": detail_mode,
            "requested_dma_mode": dma_mode,
            "proof": proof,
            "proof_sha256": hashlib.sha256(_canonical(proof)).hexdigest(),
            "cmake_cache": _read(root, "CMakeCache.txt", MAX_CACHE_BYTES).decode("utf-8"),
        }
        files = {name: _read(root, name, limit) for name, limit in _LIMITS.items()}
        verify_dma_modeproof_bytes(envelope, provenance, artifacts, files)
        view = _read(root, "generated/derby_fixed_workload/derby_view.js", 1024 * 1024)
        if (
            hashlib.sha256(view).hexdigest()
            != proof["diagnostic"]["comparison_seed"]["embedded_view_sha256"]
            or view + b"\0" not in files[_ELF]
        ):
            raise ValueError(_ERROR)
        return envelope
    except (OSError, ValueError, KeyError, TypeError, RecursionError, UnicodeError):
        raise ValueError(_ERROR) from None


def verify_dma_modeproof(
    root: Path,
    envelope: dict[str, Any],
    provenance: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> None:
    current = collect_dma_modeproof(
        root,
        envelope.get("requested_mode", ""),
        envelope.get("requested_dma_mode", ""),
        provenance,
        artifacts,
    )
    if current != envelope:
        raise ValueError(_ERROR)
