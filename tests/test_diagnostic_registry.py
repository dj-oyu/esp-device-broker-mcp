"""U1/U3/U4 and I5/I9/I10/I11: contracts, identity and historical hashes."""

import unittest

from cardputer_mcp import diagnostic_registry as registry
from cardputer_mcp.diagnostic_contracts import select_contract


class DiagnosticRegistryTests(unittest.TestCase):
    def test_source_identity_is_full_and_contract_must_be_supported(self):
        entry = registry.diagnostic_entry({"repository": "fixture/project", "commit": "f" * 40})
        self.assertEqual(entry.commit, "f" * 40)
        self.assertFalse(hasattr(registry, "_ENTRIES"))
        for commit in ("main", "f" * 39, "F" * 40, None, True):
            with self.assertRaises(ValueError):
                registry.diagnostic_entry({"repository": "fixture/project", "commit": commit})
        with self.assertRaisesRegex(ValueError, "diagnostic_contract_unsupported"):
            registry.diagnostic_entry(
                {"repository": "fixture/project", "commit": "f" * 40}, contract="unknown-v9"
            )

    def test_ordinary_build_is_independent_of_optional_diagnostics(self):
        for source in ({}, {"repository": registry.J1_REPOSITORY, "commit": "f" * 40}):
            self.assertIsNone(registry.validate_diagnostic_selection(source, None, None))
        self.assertEqual(select_contract("ON", None).identifier, "j1-fixed-v1")
        self.assertEqual(select_contract("OFF", "ON").identifier, "derby-dma-diagnostic-v1")
        for detail, dma in ((None, "OFF"), (True, "ON"), ("ON", False)):
            with self.assertRaisesRegex(ValueError, "diagnostic_axes_required"):
                select_contract(detail, dma)

    def test_legacy_descriptor_hash_and_exact_omission_boundary_are_preserved(self):
        source = {"repository": registry.J1_REPOSITORY, "commit": registry.J1_COMMIT}
        old = "aa5e7e6fdc4fca289743fe0d720fcaedfdc19562f20f4ec125da175d677bf9be"
        self.assertEqual(registry.validate_entry_binding(source, {}), old)
        for other in (
            {**source, "repository": "other/project"},
            {**source, "commit": "f" * 40},
        ):
            with self.assertRaisesRegex(ValueError, "diagnostic_entry_binding_required"):
                registry.validate_entry_binding(other, {"derby_detail_mode": "OFF"})
        with self.assertRaisesRegex(ValueError, "diagnostic_entry_binding_required"):
            registry.validate_entry_binding(
                source, {"derby_detail_mode": "OFF", "derby_dma_mode": "ON"}
            )
        for value in (None, "e" * 64):
            with self.assertRaisesRegex(ValueError, "diagnostic_entry_binding_invalid"):
                registry.validate_entry_binding(source, {"diagnostic_entry_sha256": value})

    def test_binding_null_and_mode_downgrade_never_select_legacy(self):
        source = {"repository": "fixture/project", "commit": "f" * 40}
        entry = registry.diagnostic_entry(source, contract="derby-dma-diagnostic-v1")
        record = dict(
            derby_detail_mode="OFF", derby_dma_mode="ON", diagnostic_entry_sha256=entry.sha256
        )
        self.assertEqual(registry.validate_entry_binding(source, record), entry.sha256)
        for mutation in ({"derby_dma_mode": None}, {"derby_dma_mode": "OFF"}):
            changed = {**record, **mutation}
            if mutation["derby_dma_mode"] is None:
                with self.assertRaises(ValueError):
                    registry.validate_entry_binding(source, changed)
            else:
                self.assertFalse(registry.same_diagnostic_selection(record, changed))
        self.assertFalse(registry.same_entry_binding({}, {"diagnostic_entry_sha256": None}))
