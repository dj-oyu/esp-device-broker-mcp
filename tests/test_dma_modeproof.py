"""Synthetic two-axis linked fixtures; production allowlist is never expanded."""

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_diagnostic_modeproof import image_fixture

from cardputer_mcp.diagnostic_dma_modeproof import (
    CONTRACT,
    MAX_CACHE_BYTES,
    PROOF_NAME,
    collect_dma_modeproof,
    verify_dma_modeproof,
    verify_dma_modeproof_bytes,
)
from cardputer_mcp.diagnostic_modeproof import collect_modeproof, verify_modeproof_bytes
from cardputer_mcp.diagnostic_registry import DiagnosticEntry


DMA_ENTRY = DiagnosticEntry(
    "dj-oyu/cardputer-adv-pocketjs", "ac7cce7d20eb34fdd54659e6678d672d23e5d2d1",
    contract=CONTRACT, dma_modes=("ON", "OFF"),
)


def dma_fixture(root, detail_mode="ON", dma_mode="ON", *, native_symbols=None, symbol_type=2):
    """Return source/artifact/file inputs with producer-shaped proof and native ELF functions."""
    provenance, _, _ = image_fixture(root, detail_mode)
    provenance["commit"] = DMA_ENTRY.commit
    view = (root / "generated/derby_fixed_workload/derby_view.js").read_bytes()
    data = struct.pack("<II", detail_mode == "ON", 0x2545F491)
    data += b"DERBY_DETAIL_EXIT v=1 session=%u clean=1\n\0" + view + b"\0"
    selected = (
        ("board_present_finish", "board_present_submit") if dma_mode == "ON" else ()
    ) if native_symbols is None else native_symbols
    names = bytearray(b"\0")
    rows = []
    for name, value, size, kind, section in [
        ("df_detail_enabled", 0x1000, 4, 1, 1),
        ("pocket_derby_comparison_hw_seed", 0x1004, 4, 1, 1),
        *[(name, 0x2000 + 4 * i, 4, symbol_type, 2) for i, name in enumerate(selected)],
    ]:
        rows.append(struct.pack("<IIIBBH", len(names), value, size, kind, 0, section))
        names.extend(name.encode() + b"\0")
    elf = bytearray(52)
    elf[:6] = b"\x7fELF\x01\x01"
    data_offset = len(elf)
    elf.extend(data)
    code_offset = len(elf)
    code = bytes(4 * len(selected))
    elf.extend(code)
    names_offset = len(elf)
    elf.extend(names)
    symbols_offset = len(elf)
    elf.extend(b"".join(rows))
    shoff = len(elf)
    elf.extend(bytes(40))
    elf.extend(struct.pack("<10I", 0, 1, 0, 0x1000, data_offset, len(data), 0, 0, 4, 0))
    elf.extend(struct.pack("<10I", 0, 1, 4, 0x2000, code_offset, len(code), 0, 0, 4, 0))
    elf.extend(struct.pack("<10I", 0, 3, 0, 0, names_offset, len(names), 0, 0, 1, 0))
    elf.extend(struct.pack("<10I", 0, 2, 0, 0, symbols_offset, 16 * len(rows), 3, 0, 4, 16))
    struct.pack_into("<I", elf, 32, shoff)
    struct.pack_into("<HH", elf, 46, 40, 5)
    image = bytearray((root / "cardputer_pocketjs.bin").read_bytes())
    image[176:208] = hashlib.sha256(elf).digest()
    files = {"cardputer_pocketjs.bin": bytes(image), "cardputer_pocketjs.elf": bytes(elf)}
    artifacts = [
        {"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in files.items()
    ]
    cache = (
        f"POCKET_DERBY_DETAIL_MEASURE:BOOL={detail_mode}\n"
        f"KASANE_DMA_OVERLAP:BOOL={dma_mode}\n"
    )
    cache_hash = hashlib.sha256(cache.encode()).hexdigest()
    diagnostic = json.loads((root / "diagnostic-mode-proof.json").read_text())
    diagnostic["artifacts"] = {
        item["name"]: {key: value for key, value in item.items() if key != "name"}
        for item in artifacts
    }
    for key in ("build_mode", "build_measure"):
        diagnostic[key]["cmake_cache_sha256"] = cache_hash
    axes = {"detail_measurement": detail_mode, "dma_overlap": dma_mode}
    proof = {
        "schema_version": 1, "proof_contract": CONTRACT,
        "requested_axes": axes, "verified_axes": dict(axes),
        "dma_overlap": {
            "mode": dma_mode, "symbol_contract": "board-present-submit-finish-v1",
            "native_symbols": (
                ["board_present_finish", "board_present_submit"] if dma_mode == "ON" else []
            ),
            "cmake_cache_sha256": cache_hash,
        },
        "diagnostic": diagnostic, "artifacts": diagnostic["artifacts"], "hardware_tested": False,
    }
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    (root / "CMakeCache.txt").write_bytes(cache.encode())
    (root / PROOF_NAME).write_text(json.dumps(proof))
    return provenance, artifacts, files


def resign(envelope):
    envelope["proof_sha256"] = hashlib.sha256(
        json.dumps(envelope["proof"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


dma_image_fixture = dma_fixture


class DMAModeproofTests(unittest.TestCase):
    def setUp(self):
        self.registry = patch("cardputer_mcp.diagnostic_registry._ENTRIES", (DMA_ENTRY,))
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_four_axes_and_retained_cache_evidence(self):
        for detail in ("OFF", "ON"):
            for dma in ("OFF", "ON"):
                with (
                    self.subTest(detail=detail, dma=dma),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    source, artifacts, files = dma_fixture(root, detail, dma)
                    envelope = collect_dma_modeproof(root, detail, dma, source, artifacts)
                    verify_dma_modeproof(root, envelope, source, artifacts)
                    (root / "CMakeCache.txt").unlink()
                    (root / PROOF_NAME).unlink()
                    verify_dma_modeproof_bytes(envelope, source, artifacts, files)

    def test_cache_axes_missing_duplicate_stale_or_wrong_type_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, artifacts, _ = dma_fixture(root)
            good = (root / "CMakeCache.txt").read_text()
            variants = [
                good.replace("KASANE_DMA_OVERLAP:BOOL=ON\n", ""),
                good + "KASANE_DMA_OVERLAP:BOOL=ON\n",
                good.replace("KASANE_DMA_OVERLAP:BOOL=ON", "KASANE_DMA_OVERLAP:BOOL=OFF"),
                good.replace("KASANE_DMA_OVERLAP:BOOL=ON", "KASANE_DMA_OVERLAP:STRING=ON"),
                good + "x" * MAX_CACHE_BYTES,
            ]
            for cache in variants:
                (root / "CMakeCache.txt").write_text(cache)
                with self.assertRaises(ValueError):
                    collect_dma_modeproof(root, "ON", "ON", source, artifacts)

    def test_partial_wrong_type_or_duplicate_native_symbols_rejected(self):
        for symbols, kind in [
            (("board_present_submit",), 2),
            (("board_present_finish", "board_present_submit"), 1),
            (("board_present_submit", "board_present_submit"), 2),
        ]:
            with (
                self.subTest(symbols=symbols, kind=kind),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                source, artifacts, _ = dma_fixture(root, native_symbols=symbols, symbol_type=kind)
                with self.assertRaises(ValueError):
                    collect_dma_modeproof(root, "ON", "ON", source, artifacts)

    def test_dma_off_rejects_function_presence_and_has_no_detail_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, artifacts, _ = dma_fixture(
                root, "OFF", "OFF", native_symbols=("board_present_finish", "board_present_submit")
            )
            with self.assertRaises(ValueError):
                collect_dma_modeproof(root, "OFF", "OFF", source, artifacts)
            # A valid detail-only producer output is insufficient for this contract.
            (root / PROOF_NAME).unlink()
            with self.assertRaises(ValueError):
                collect_dma_modeproof(root, "OFF", "OFF", source, artifacts)
            with self.assertRaises(ValueError):
                collect_modeproof(root, "OFF", source, artifacts)

    def test_retained_tamper_source_image_and_contract_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, artifacts, files = dma_fixture(root)
            envelope = collect_dma_modeproof(root, "ON", "ON", source, artifacts)
            for mutated in (
                {**envelope, "requested_dma_mode": "OFF"},
                {**envelope, "cmake_cache": envelope["cmake_cache"] + "#changed\n"},
                {**envelope, "proof_sha256": "0" * 64},
            ):
                with self.assertRaises(ValueError):
                    verify_dma_modeproof_bytes(mutated, source, artifacts, files)
            forged = copy.deepcopy(envelope)
            forged["proof"]["verified_axes"]["dma_overlap"] = "OFF"
            resign(forged)
            with self.assertRaises(ValueError):
                verify_dma_modeproof_bytes(forged, source, artifacts, files)
            with self.assertRaises(ValueError):
                verify_dma_modeproof_bytes(
                    envelope, {**source, "commit": "e" * 40}, artifacts, files
                )
            wrong_files = {
                **files, "cardputer_pocketjs.bin": files["cardputer_pocketjs.bin"] + b"x"
            }
            with self.assertRaises(ValueError):
                verify_dma_modeproof_bytes(envelope, source, artifacts, wrong_files)
            nested = envelope["proof"]["diagnostic"]
            detail_envelope = {
                "requested_mode": "ON", "proof": nested,
                "proof_sha256": hashlib.sha256(
                    json.dumps(nested, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
            with self.assertRaises(ValueError):
                verify_modeproof_bytes(detail_envelope, source, artifacts, files)

    def test_missing_duplicate_and_tampered_proof_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, artifacts, _ = dma_fixture(root)
            envelope = collect_dma_modeproof(root, "ON", "ON", source, artifacts)
            proof_path = root / PROOF_NAME
            proof_path.unlink()
            with self.assertRaises(ValueError):
                verify_dma_modeproof(root, envelope, source, artifacts)
            proof_path.write_text('{"proof_contract":"one","proof_contract":"two"}')
            with self.assertRaises(ValueError):
                collect_dma_modeproof(root, "ON", "ON", source, artifacts)
            proof_path.write_text(json.dumps({**envelope["proof"], "hardware_tested": True}))
            with self.assertRaises(ValueError):
                verify_dma_modeproof(root, envelope, source, artifacts)


if __name__ == "__main__":
    unittest.main()
