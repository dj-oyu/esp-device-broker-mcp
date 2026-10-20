"""Offline export schema with synthetic linked DMA images and private sentinels."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_dma_modeproof import DMA_ENTRY, dma_fixture, resign

from cardputer_mcp.build_artifacts import manifest_digest
from cardputer_mcp.diagnostic_dma_modeproof import (
    collect_dma_modeproof,
    verify_dma_modeproof_bytes,
)
from cardputer_mcp.manifest_export import (
    MAX_CACHE_BYTES,
    MAX_EXPORT_BYTES,
    canonical_json,
    export_response,
    make_manifest_export,
    manifest_export_sha256,
    parse_manifest_export,
    validate_manifest_export,
    validate_plan_export,
)


class ManifestExportTests(unittest.TestCase):
    def manifest(self, detail="OFF", dma="ON"):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, artifacts, files = dma_fixture(root, detail, dma)
            with patch("cardputer_mcp.diagnostic_registry._ENTRIES", (DMA_ENTRY,)):
                envelope = collect_dma_modeproof(root, detail, dma, source, artifacts)
                envelope["cmake_cache"] += (
                    "PRIVATE_PATH:PATH=C:/SYNTHETIC_PRIVATE\n"
                    "TOKEN:STRING=SYNTHETIC_SECRET\n"
                )
                cache_hash = hashlib.sha256(envelope["cmake_cache"].encode()).hexdigest()
                proof = envelope["proof"]
                proof["dma_overlap"]["cmake_cache_sha256"] = cache_hash
                for key in ("build_mode", "build_measure"):
                    proof["diagnostic"][key]["cmake_cache_sha256"] = cache_hash
                resign(envelope)
                verify_dma_modeproof_bytes(envelope, source, artifacts, files)
            return {
                "schema_version": 1,
                "build_job_id": "synthetic-build",
                "build_plan_hash": "a" * 64,
                "diagnostic_modeproof": envelope,
                "derby_detail_mode": detail,
                "derby_dma_mode": dma,
            }

    def test_four_axes_export_keeps_original_digest_without_private_cache(self):
        for detail in ("ON", "OFF"):
            for dma in ("ON", "OFF"):
                with self.subTest(detail=detail, dma=dma):
                    original = self.manifest(detail, dma)
                    before = copy.deepcopy(original)
                    result = export_response(original)
                    export = result["manifest_export"]
                    self.assertEqual(
                        set(result), {"manifest", "manifest_export", "manifest_export_sha256"}
                    )
                    self.assertEqual(export["original_manifest_sha256"], manifest_digest(original))
                    self.assertNotEqual(
                        manifest_digest(result["manifest"]), manifest_digest(original)
                    )
                    self.assertEqual(
                        result["manifest_export_sha256"], manifest_export_sha256(export)
                    )
                    self.assertEqual(parse_manifest_export(canonical_json(export)), export)
                    self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(result))
                    self.assertNotIn("SYNTHETIC_SECRET", json.dumps(result))
                    self.assertNotIn("cmake_cache", export["manifest"]["diagnostic_modeproof"])
                    self.assertEqual(original, before)

    def test_canonical_json_shared_vector(self):
        value = {"z": "é", "a": [True, None, 1], "line": "\n"}
        self.assertEqual(canonical_json(value), b'{"a":[true,null,1],"line":"\\n","z":"\\u00e9"}')

    def test_closed_schema_and_strict_cache_evidence(self):
        export = make_manifest_export(self.manifest())
        cases = []
        for field, values in {
            "schema_version": [True, None, "1", 0, 2],
            "contract": [None, "unknown"],
            "original_manifest_sha256": [None, True, "A" * 64, "a" * 63],
            "manifest": [None, [], True],
        }.items():
            for value in values:
                bad = copy.deepcopy(export)
                bad[field] = value
                cases.append(bad)
        extra = copy.deepcopy(export)
        extra["extra"] = "rejected"
        cases.append(extra)
        for field, values in {
            "retained_private": [False, 1, None],
            "bytes": [True, 0, -1, MAX_CACHE_BYTES + 1, 1.0, None],
            "sha256": [None, "A" * 64, "0" * 64],
            "extra": ["rejected"],
        }.items():
            for value in values:
                bad = copy.deepcopy(export)
                bad["manifest"]["diagnostic_modeproof"]["cache_evidence"][field] = value
                cases.append(bad)
        for key in ("build_mode", "build_measure"):
            bad = copy.deepcopy(export)
            bad["manifest"]["diagnostic_modeproof"]["proof"]["diagnostic"][key][
                "cmake_cache_sha256"
            ] = "0" * 64
            cases.append(bad)
        for index, bad in enumerate(cases):
            with (
                self.subTest(case=index),
                self.assertRaisesRegex(ValueError, "invalid_manifest_export"),
            ):
                validate_manifest_export(bad)

    def test_duplicate_keys_at_every_level_and_size_nonfinite_reject(self):
        raw = canonical_json(make_manifest_export(self.manifest()))
        for bad in (
            b'{"schema_version":1,' + raw[1:],
            raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1', 1),
            raw.replace(
                b'"retained_private":true', b'"retained_private":true,"retained_private":true'
            ),
            raw.replace(b'"schema_version":1', b'"schema_version":NaN', 1),
            b"x" * (MAX_EXPORT_BYTES + 1),
        ):
            with self.assertRaisesRegex(ValueError, "invalid_manifest_export"):
                parse_manifest_export(bad)

    def test_fixed_plan_commitment_rejects_rehashed_other_job_or_manifest(self):
        original = self.manifest()
        export = make_manifest_export(original)
        plan = {
            "manifest_sha256": manifest_digest(original),
            "manifest_export_sha256": manifest_export_sha256(export),
        }
        validate_plan_export(plan, original)
        for key in ("build_job_id", "build_plan_hash"):
            changed = copy.deepcopy(original)
            changed[key] = "b" * 64
            rehashed = make_manifest_export(changed)
            self.assertNotEqual(manifest_export_sha256(rehashed), plan["manifest_export_sha256"])
            with self.assertRaises(ValueError):
                validate_plan_export(plan, changed)
        for value in (None, True, "0" * 64, "A" * 64):
            with self.assertRaises(ValueError):
                validate_plan_export(plan | {"manifest_export_sha256": value}, original)
        legacy = {"manifest_sha256": manifest_digest(original)}
        before = copy.deepcopy(legacy)
        validate_plan_export(legacy, original)
        self.assertEqual(before, legacy)  # Runtime legacy receipts gain no offline commitment.


if __name__ == "__main__":
    unittest.main()
