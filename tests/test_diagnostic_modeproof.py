"""Synthetic linked image tests; never invoke the SDK or a device."""

import copy
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from cardputer_mcp.diagnostic_modeproof import (
    J1_COMMIT,
    collect_modeproof,
    verify_modeproof,
    verify_modeproof_bytes,
)


def image_fixture(root: Path, mode: str):
    view = b"function synthetic_fixed_view(){}"
    data = struct.pack("<II", mode == "ON", 0x2545F491)
    data += b"DERBY_DETAIL_EXIT v=1 session=%u clean=1\n\0" + view + b"\0"
    names = b"\0df_detail_enabled\0pocket_derby_comparison_hw_seed\0"
    elf = bytearray(52)
    elf[:6] = b"\x7fELF\x01\x01"
    data_offset = len(elf)
    elf.extend(data)
    names_offset = len(elf)
    elf.extend(names)
    symbols_offset = len(elf)
    elf.extend(struct.pack("<IIIBBH", 1, 0x1000, 4, 1, 0, 1))
    elf.extend(struct.pack("<IIIBBH", 19, 0x1004, 4, 1, 0, 1))
    shoff = len(elf)
    elf.extend(bytes(40))
    elf.extend(struct.pack("<10I", 0, 1, 0, 0x1000, data_offset, len(data), 0, 0, 4, 0))
    elf.extend(struct.pack("<10I", 0, 3, 0, 0, names_offset, len(names), 0, 0, 1, 0))
    elf.extend(struct.pack("<10I", 0, 2, 0, 0, symbols_offset, 32, 2, 0, 4, 16))
    struct.pack_into("<I", elf, 32, shoff)
    struct.pack_into("<HH", elf, 46, 40, 4)
    image = bytearray(288)
    image[0] = 0xE9
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[144:151] = b"v6.0.1\0"
    image[176:208] = hashlib.sha256(elf).digest()
    files = {"cardputer_pocketjs.bin": bytes(image), "cardputer_pocketjs.elf": bytes(elf)}
    receipts = [
        {"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for name, raw in files.items()
    ]
    proof = {
        "schema_version": 1,
        "build_mode": {"telemetry_mode": "ON"},
        "build_measure": {"measure_mode": "ON"},
        "fixed_workload": {
            "mode": "ON",
            "owner_completion": {
                "contract": "fixed-owner-v1",
                "record": "DERBY_DETAIL_EXIT",
                "detail_mode": mode,
            },
        },
        "detail_measurement": {"mode": mode},
        "comparison_seed": {
            "mode": "fixed",
            "hardware_seed": "2545F491",
            "elf_seed_symbol_verified": True,
            "elf_embedded_view_verified": True,
            "embedded_view_sha256": hashlib.sha256(view).hexdigest(),
        },
        "hardware_tested": False,
        "artifacts": {
            item["name"]: {key: value for key, value in item.items() if key != "name"}
            for item in receipts
        },
    }
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    generated = root / "generated/derby_fixed_workload"
    generated.mkdir(parents=True, exist_ok=True)
    (generated / "derby_view.js").write_bytes(view)
    (root / "diagnostic-mode-proof.json").write_text(json.dumps(proof))
    provenance = {"repository": "dj-oyu/cardputer-adv-pocketjs", "commit": J1_COMMIT}
    return provenance, receipts, files


class DiagnosticModeproofTests(unittest.TestCase):
    def test_same_source_on_off_proof_and_retained_bytes(self):
        for mode in ("ON", "OFF"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                provenance, artifacts, files = image_fixture(root, mode)
                envelope = collect_modeproof(root, mode, provenance, artifacts)
                verify_modeproof(root, envelope, provenance, artifacts)
                # Retained flash snapshots can qualify evidence after build-cache eviction.
                verify_modeproof_bytes(envelope, provenance, artifacts, files)
                with self.assertRaisesRegex(ValueError, "diagnostic_modeproof_invalid"):
                    collect_modeproof(root, "OFF" if mode == "ON" else "ON", provenance, artifacts)

    def test_proof_removal_duplicates_oversize_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provenance, artifacts, _ = image_fixture(root, "ON")
            envelope = collect_modeproof(root, "ON", provenance, artifacts)
            proof = root / "diagnostic-mode-proof.json"
            proof.unlink()
            with self.assertRaises(ValueError):
                verify_modeproof(root, envelope, provenance, artifacts)
            for raw in ('{"schema_version":1,"schema_version":1}', " " * 16385):
                proof.write_text(raw)
                with self.assertRaises(ValueError):
                    collect_modeproof(root, "ON", provenance, artifacts)
            proof.unlink()
            target = root / "other.json"
            target.write_text(json.dumps(envelope["proof"]))
            try:
                proof.symlink_to(target)
            except OSError:
                return  # Windows runners may not grant symlink creation.
            with self.assertRaises(ValueError):
                collect_modeproof(root, "ON", provenance, artifacts)

    def test_relabelled_proof_cannot_override_linked_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provenance, artifacts, files = image_fixture(root, "OFF")
            envelope = collect_modeproof(root, "OFF", provenance, artifacts)
            forged = copy.deepcopy(envelope)
            forged["requested_mode"] = "ON"
            forged["proof"]["detail_measurement"]["mode"] = "ON"
            forged["proof"]["fixed_workload"]["owner_completion"]["detail_mode"] = "ON"
            forged["proof_sha256"] = hashlib.sha256(
                json.dumps(forged["proof"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            with self.assertRaises(ValueError):
                verify_modeproof_bytes(forged, provenance, artifacts, files)

    def test_other_image_source_manifest_and_view_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provenance, artifacts, files = image_fixture(root, "ON")
            envelope = collect_modeproof(root, "ON", provenance, artifacts)
            other_source = {**provenance, "commit": "main"}
            with self.assertRaises(ValueError):
                verify_modeproof_bytes(envelope, other_source, artifacts, files)
            with self.assertRaises(ValueError):
                verify_modeproof_bytes(envelope, provenance, artifacts[:-1], files)
            altered = {**files, "cardputer_pocketjs.bin": files["cardputer_pocketjs.bin"] + b"x"}
            with self.assertRaises(ValueError):
                verify_modeproof_bytes(envelope, provenance, artifacts, altered)
            (root / "generated/derby_fixed_workload/derby_view.js").write_bytes(b"stale")
            with self.assertRaises(ValueError):
                collect_modeproof(root, "ON", provenance, artifacts)


if __name__ == "__main__":
    unittest.main()
