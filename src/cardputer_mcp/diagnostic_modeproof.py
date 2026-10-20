"""Bounded J1 mode evidence, independently checked against linked image bytes."""

import hashlib
import json
import os
import stat
import struct
from pathlib import Path
from typing import Any

from .diagnostic_registry import J1_COMMIT as J1_COMMIT
from .diagnostic_registry import diagnostic_entry
_PROOF = "diagnostic-mode-proof.json"
_BIN = "cardputer_pocketjs.bin"
_ELF = "cardputer_pocketjs.elf"
_LIMITS = {_BIN: 32 * 1024 * 1024, _ELF: 64 * 1024 * 1024}
_ERROR = "diagnostic_modeproof_invalid"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_ERROR)
        result[key] = value
    return result


def _read(root: Path, name: str, limit: int) -> bytes:
    path = root
    for part in (None, *Path(name).parts):
        if part is not None:
            path /= part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(_ERROR)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError(_ERROR)
        raw = stream.read(limit + 1)
    if len(raw) != info.st_size:
        raise ValueError(_ERROR)
    return raw


def _symbol_u32(elf: bytes, wanted: str) -> int:
    """Read a unique initialized ELF32 little-endian STT_OBJECT, without SDK tools."""
    if len(elf) < 52 or elf[:6] != b"\x7fELF\x01\x01":
        raise ValueError(_ERROR)
    shoff = struct.unpack_from("<I", elf, 32)[0]
    entsize, count = struct.unpack_from("<HH", elf, 46)
    if entsize < 40 or not 0 < count <= 4096 or shoff + entsize * count > len(elf):
        raise ValueError(_ERROR)
    sections = [struct.unpack_from("<10I", elf, shoff + i * entsize) for i in range(count)]

    def area(offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > len(elf):
            raise ValueError(_ERROR)
        return elf[offset : offset + size]

    found: list[int] = []
    total = 0
    wanted_name = wanted.encode() + b"\0"
    for section in sections:
        if section[1] != 2:
            continue
        link, stride = section[6], section[9]
        if link >= count or stride < 16 or section[5] % stride:
            raise ValueError(_ERROR)
        total += section[5] // stride
        if total > 262144:
            raise ValueError(_ERROR)
        names_section = sections[link]
        if names_section[1] != 3:
            raise ValueError(_ERROR)
        names = area(names_section[4], names_section[5])
        symbols = area(section[4], section[5])
        for pos in range(0, len(symbols), stride):
            name, value, size, info, _, index = struct.unpack_from("<IIIBBH", symbols, pos)
            if name >= len(names):
                raise ValueError(_ERROR)
            # Compare only the bounded target name; malformed unrelated string
            # offsets must not cause a whole string-table scan for every symbol.
            if names[name : name + len(wanted_name)] != wanted_name:
                continue
            if info & 15 != 1 or size != 4 or not 0 < index < count:
                raise ValueError(_ERROR)
            target = sections[index]
            delta = value - target[3]
            if target[1] == 8 or delta < 0 or delta + 4 > target[5]:
                raise ValueError(_ERROR)
            found.append(struct.unpack("<I", area(target[4] + delta, 4))[0])
    if len(found) != 1:
        raise ValueError(_ERROR)
    return found[0]


def verify_modeproof_bytes(
    envelope: dict[str, Any],
    provenance: dict[str, Any],
    artifacts: list[dict[str, Any]],
    files: dict[str, bytes],
) -> None:
    """Recheck durable evidence against retained exact BIN/ELF bytes; grant no authority."""
    try:
        entry = diagnostic_entry(provenance)
        if (
            set(envelope) != {"requested_mode", "proof", "proof_sha256"}
            or envelope["requested_mode"] not in entry.modes
        ):
            raise ValueError(_ERROR)
        proof = envelope["proof"]
        canonical = _canonical(proof)
        if (
            len(canonical) > 16384
            or hashlib.sha256(canonical).hexdigest() != envelope["proof_sha256"]
            or set(proof)
            != {
                "schema_version",
                "build_mode",
                "build_measure",
                "fixed_workload",
                "detail_measurement",
                "comparison_seed",
                "hardware_tested",
                "artifacts",
            }
            or type(proof["schema_version"]) is not int
            or proof["schema_version"] != 1
            or proof["hardware_tested"] is not False
            or proof["build_mode"]["telemetry_mode"] != "ON"
            or proof["build_measure"]["measure_mode"] != "ON"
            or proof["fixed_workload"]["mode"] != "ON"
            or proof["detail_measurement"]["mode"] != envelope["requested_mode"]
            or proof["fixed_workload"]["owner_completion"]
            != {
                "contract": "fixed-owner-v1",
                "record": "DERBY_DETAIL_EXIT",
                "detail_mode": envelope["requested_mode"],
            }
            or proof["comparison_seed"]["mode"] != "fixed"
            or proof["comparison_seed"]["hardware_seed"] != "2545F491"
            or proof["comparison_seed"]["elf_seed_symbol_verified"] is not True
            or proof["comparison_seed"]["elf_embedded_view_verified"] is not True
            or set(proof["artifacts"]) != set(_LIMITS)
        ):
            raise ValueError(_ERROR)
        for name, limit in _LIMITS.items():
            raw = files[name]
            receipt = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            matching = [item for item in artifacts if item.get("name") == name]
            if (
                not 0 < len(raw) <= limit
                or proof["artifacts"][name] != receipt
                or len(matching) != 1
                or any(matching[0].get(key) != value for key, value in receipt.items())
            ):
                raise ValueError(_ERROR)
        image, elf = files[_BIN], files[_ELF]
        if (
            _symbol_u32(elf, "df_detail_enabled") != int(envelope["requested_mode"] == "ON")
            or _symbol_u32(elf, "pocket_derby_comparison_hw_seed") != 0x2545F491
            or b"DERBY_DETAIL_EXIT v=1 session=%u clean=1\n\0" not in elf
            or len(image) < 288
            or image[0] != 0xE9
            or struct.unpack_from("<I", image, 32)[0] != 0xABCD5432
            or image[144:176].split(b"\0")[0] != b"v6.0.1"
            or image[176:208] != hashlib.sha256(elf).digest()
        ):
            raise ValueError(_ERROR)
    except (
        KeyError,
        TypeError,
        AttributeError,
        ValueError,
        struct.error,
        OverflowError,
        RecursionError,
    ):
        raise ValueError(_ERROR) from None


def collect_modeproof(
    root: Path, requested_mode: str, provenance: dict[str, Any], artifacts: list[dict[str, Any]]
) -> dict[str, Any]:
    """Read fresh mandatory FW output and retain bounded JSON plus its canonical hash."""
    try:
        raw = _read(root, _PROOF, 16384)
        proof = json.loads(raw, object_pairs_hook=_unique)
        envelope = {
            "requested_mode": requested_mode,
            "proof": proof,
            "proof_sha256": hashlib.sha256(_canonical(proof)).hexdigest(),
        }
        files = {name: _read(root, name, limit) for name, limit in _LIMITS.items()}
        verify_modeproof_bytes(envelope, provenance, artifacts, files)
        view = _read(root, "generated/derby_fixed_workload/derby_view.js", 1024 * 1024)
        if (
            hashlib.sha256(view).hexdigest() != proof["comparison_seed"]["embedded_view_sha256"]
            or view + b"\0" not in files[_ELF]
        ):
            raise ValueError(_ERROR)
        return envelope
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise ValueError(_ERROR) from None


def verify_modeproof(
    root: Path,
    envelope: dict[str, Any],
    provenance: dict[str, Any],
    artifacts: list[dict[str, Any]],
) -> None:
    """Revalidate a retained manifest, including proof-file removal or modification."""
    current = collect_modeproof(root, envelope.get("requested_mode", ""), provenance, artifacts)
    if current != envelope:
        raise ValueError(_ERROR)
