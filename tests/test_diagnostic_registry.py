"""Closed production admission and additive-policy isolation; no SDK or devices."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_diagnostic_modeproof import image_fixture
from test_dma_modeproof import dma_fixture

from cardputer_mcp import diagnostic_registry as registry
from cardputer_mcp.diagnostic_dma_modeproof import (
    PROOF_NAME,
    collect_dma_modeproof,
    verify_dma_modeproof_bytes,
)
from cardputer_mcp.diagnostic_modeproof import collect_modeproof, verify_modeproof_bytes


class DiagnosticRegistryTests(unittest.TestCase):
    def test_production_registry_has_only_j1_and_exact_runtime_resource_source(self):
        self.assertEqual(
            [(entry.repository, entry.commit) for entry in registry._ENTRIES],
            [
                ("dj-oyu/cardputer-adv-pocketjs", "b561aea1c0798feb28055a00d36681fae81ac58a"),
                ("dj-oyu/cardputer-adv-pocketjs", "d6609bbf153c261f8d2e682793035e00f72e6ccc"),
            ],
        )
        entry = registry._ENTRIES[0]
        self.assertEqual(entry.repository, "dj-oyu/cardputer-adv-pocketjs")
        self.assertEqual(entry.commit, registry.J1_COMMIT)
        self.assertEqual(entry.modes, ("ON", "OFF"))
        self.assertEqual(entry.contract, "j1-fixed-v1")
        self.assertEqual(entry.dma_modes, ())
        self.assertEqual(
            entry.sha256, "aa5e7e6fdc4fca289743fe0d720fcaedfdc19562f20f4ec125da175d677bf9be"
        )
        for commit in (
            "b7d257adf8326c41bc982852c072acf64e3fbee5",
            "ac7cce7d20eb34fdd54659e6678d672d23e5d2d1",
            "263d6d898257e58b3be75d59a15ce0321428f77d",
            "df63fbaa8cec6dce4a0f9d2d05d2d41a929baaf6",
            "d0ba90919647ba99a8677bbe758094b3c3216551",
            "d6609bbf",
            "d6609bbf153c261f8d2e682793035e00f72e6ccc".upper(),
            "main",
            "refs/heads/feat/derby-fixed-runtime-resource-proof-20261020",
            "f" * 40,
        ):
            with self.subTest(commit=commit), self.assertRaises(ValueError):
                registry.diagnostic_entry({"repository": entry.repository, "commit": commit})

    def test_runtime_resource_entry_requires_both_explicit_axes_and_own_digest(self):
        source = {
            "repository": "dj-oyu/cardputer-adv-pocketjs",
            "commit": "d6609bbf153c261f8d2e682793035e00f72e6ccc",
        }
        entry = registry.diagnostic_entry(source)
        self.assertEqual(entry.contract, "derby-dma-diagnostic-v1")
        self.assertEqual(entry.modes, ("ON", "OFF"))
        self.assertEqual(entry.dma_modes, ("ON", "OFF"))
        self.assertEqual(
            entry.sha256, "774d8a7b018e1c825c38f28959e61068c0d10a6315b50d81d1958dd1d4fa3deb"
        )
        for detail in entry.modes:
            for dma in entry.dma_modes:
                self.assertEqual(registry.validate_diagnostic_selection(source, detail, dma), entry)
                record = {
                    "derby_detail_mode": detail,
                    "derby_dma_mode": dma,
                    "diagnostic_entry_sha256": entry.sha256,
                }
                self.assertEqual(registry.validate_diagnostic_record(source, record), entry)
                self.assertEqual(registry.validate_entry_binding(source, record), entry.sha256)
        for bad in (None, True, False, 1, "on", "AUTO", "-DANY=ON"):
            for detail, dma in ((bad, "ON"), ("OFF", bad)):
                with self.subTest(detail=detail, dma=dma), self.assertRaises(ValueError):
                    registry.validate_diagnostic_selection(source, detail, dma)
        with self.assertRaises(ValueError):
            registry.validate_diagnostic_selection(source, None, None)
        with self.assertRaisesRegex(ValueError, "diagnostic_entry_binding_required"):
            registry.validate_entry_binding(source, {})
        for bad in (None, True, "0" * 64, registry._J1_ENTRY.sha256):
            with self.subTest(binding=bad), self.assertRaises(ValueError):
                registry.validate_entry_binding(source, {"diagnostic_entry_sha256": bad})
        for repository in ("fixture/project", "dj-oyu/cardputer-adv-pocketjs-extra"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                registry.diagnostic_entry({**source, "repository": repository})
        with self.assertRaises(ValueError):
            registry.validate_diagnostic_selection(
                {**source, "commit": registry.J1_COMMIT}, "ON", "OFF"
            )

    def test_runtime_source_four_linked_fixtures_without_registry_patch(self):
        for detail in ("ON", "OFF"):
            for dma in ("ON", "OFF"):
                with (
                    self.subTest(detail=detail, dma=dma),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    source, artifacts, files = dma_fixture(root, detail, dma)
                    source["commit"] = "d6609bbf153c261f8d2e682793035e00f72e6ccc"
                    envelope = collect_dma_modeproof(root, detail, dma, source, artifacts)
                    self.assertEqual(
                        (envelope["requested_mode"], envelope["requested_dma_mode"]),
                        (detail, dma),
                    )
                    (root / "CMakeCache.txt").unlink()
                    (root / PROOF_NAME).unlink()
                    verify_dma_modeproof_bytes(envelope, source, artifacts, files)
                    for changed in (
                        {**source, "commit": registry.J1_COMMIT},
                        {**source, "commit": "d0ba90919647ba99a8677bbe758094b3c3216551"},
                        {**source, "repository": "fixture/project"},
                    ):
                        with self.assertRaises(ValueError):
                            verify_dma_modeproof_bytes(envelope, changed, artifacts, files)

    def test_other_entry_cannot_change_j1_digest_or_legacy_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provenance, artifacts, files = image_fixture(root, "OFF")
            original = registry.diagnostic_entry(provenance).sha256
            proof = collect_modeproof(root, "OFF", provenance, artifacts)
            unrelated = registry.DiagnosticEntry("fixture/project", "f" * 40)
            with patch.object(registry, "_ENTRIES", (*registry._ENTRIES, unrelated)):
                self.assertEqual(registry.diagnostic_entry(provenance).sha256, original)
                self.assertEqual(collect_modeproof(root, "OFF", provenance, artifacts), proof)
                verify_modeproof_bytes(proof, provenance, artifacts, files)
                self.assertEqual(registry.validate_entry_binding(provenance, {}), original)
                registry.validate_entry_binding(provenance, {"diagnostic_entry_sha256": original})
                with self.assertRaises(ValueError):
                    registry.validate_entry_binding(
                        {"repository": unrelated.repository, "commit": unrelated.commit}, {}
                    )
            with self.assertRaises(ValueError):
                registry.validate_entry_binding(
                    provenance, {"diagnostic_entry_sha256": unrelated.sha256}
                )
            with self.assertRaises(ValueError):
                registry.validate_entry_binding(provenance, {"diagnostic_entry_sha256": None})

    def test_duplicate_entry_and_unknown_contract_reject(self):
        provenance = {"repository": registry._ENTRIES[0].repository, "commit": registry.J1_COMMIT}
        for entries in (
            (*registry._ENTRIES, registry._ENTRIES[0]),
            (registry.DiagnosticEntry(provenance["repository"], registry.J1_COMMIT, "unknown"),),
        ):
            with patch.object(registry, "_ENTRIES", entries), self.assertRaises(ValueError):
                registry.diagnostic_entry(provenance)

    def test_binding_presence_and_null_are_not_legacy_omission(self):
        self.assertTrue(registry.same_entry_binding({}, {}))
        for bad in (None, "bad", "e" * 64):
            record = {"diagnostic_entry_sha256": bad}
            self.assertFalse(registry.same_entry_binding(record, {}))
            self.assertFalse(registry.same_entry_binding({}, record))
        self.assertFalse(
            registry.same_entry_binding(
                {"diagnostic_entry_sha256": None}, {"diagnostic_entry_sha256": None}
            )
        )
