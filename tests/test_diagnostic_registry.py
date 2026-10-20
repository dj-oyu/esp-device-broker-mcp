"""Closed production admission and additive-policy isolation; no SDK or devices."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_diagnostic_modeproof import image_fixture

from cardputer_mcp import diagnostic_registry as registry
from cardputer_mcp.diagnostic_modeproof import collect_modeproof, verify_modeproof_bytes


class DiagnosticRegistryTests(unittest.TestCase):
    def test_production_registry_remains_j1_only(self):
        self.assertEqual(len(registry._ENTRIES), 1)
        entry = registry._ENTRIES[0]
        self.assertEqual(entry.repository, "dj-oyu/cardputer-adv-pocketjs")
        self.assertEqual(entry.commit, registry.J1_COMMIT)
        self.assertEqual(entry.modes, ("ON", "OFF"))
        for commit in ("b7d257adf8326c41bc982852c072acf64e3fbee5", "main", "f" * 40):
            with self.assertRaises(ValueError):
                registry.diagnostic_entry({"repository": entry.repository, "commit": commit})

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
