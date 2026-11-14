"""Receipt rotation failures use fabricated private caches, never live broker state."""

import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_source_lifecycle import CacheFixture

from cardputer_mcp.source_snapshot import LIMITS, SourceError


class RotationTransactionTests(CacheFixture, unittest.TestCase):
    def setUp(self):
        self.setup_cache()
        self.enterContext(patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1))
        for module in ("source_snapshot", "source_payloads"):
            self.enterContext(patch(f"cardputer_mcp.{module}.EVIDENCE_SNAPSHOTS", 3))
        self.first = self.fill(3)

    def receipts(self):
        return {
            path.parent.name: path.read_bytes()
            for path in (self.root / "cache").glob("*/receipt.json")
        }

    def test_low_disk_with_full_payload_capacity_preserves_receipts(self):
        before = self.receipts()
        with (
            patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)),
            self.assertRaisesRegex(SourceError, "source_disk_reserve_required"),
        ):
            self.fetch(4)
        self.assertEqual(self.receipts(), before)

    def test_later_low_disk_with_available_payload_capacity_preserves_receipts(self):
        before = self.receipts()
        with (
            patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2),
            patch("shutil.disk_usage", return_value=SimpleNamespace(free=0)),
            self.assertRaisesRegex(SourceError, "source_disk_reserve_required"),
        ):
            self.fetch(4)
        self.assertEqual(self.receipts(), before)

    def test_protected_payload_capacity_preserves_receipts(self):
        self.add(
            {
                "job_id": "protected-build",
                "test": "firmware_build",
                "broker_epoch": "epoch",
                "project_id": "fixture",
                "build_principal": "owner",
                "source_receipt": self.first[-1]["source_receipt"],
            },
            "running",
        )
        before = self.receipts()
        with self.assertRaisesRegex(SourceError, "source_cache_protected"):
            self.fetch(4)
        self.assertEqual(self.receipts(), before)

    def test_disk_drop_at_second_check_does_not_start_rotation(self):
        before = self.receipts()
        with (
            patch(
                "shutil.disk_usage",
                side_effect=[SimpleNamespace(free=10**12), SimpleNamespace(free=0)],
            ),
            patch("cardputer_mcp.source_payloads.rotate_evidence") as rotate,
            self.assertRaisesRegex(SourceError, "source_disk_reserve_required"),
        ):
            self.fetch(4)
        rotate.assert_not_called()
        self.assertEqual(self.receipts(), before)

    def test_reservation_failure_does_not_start_rotation(self):
        before = self.receipts()
        original = Path.open

        def fail_reservation(path, *args, **kwargs):
            if path.name == "reservation.json" and args == ("xb",):
                raise OSError("synthetic reservation failure")
            return original(path, *args, **kwargs)

        with (
            patch.object(Path, "open", fail_reservation),
            patch("cardputer_mcp.source_payloads.rotate_evidence") as rotate,
            self.assertRaisesRegex(SourceError, "source_fetch_failed"),
        ):
            self.fetch(4)
        rotate.assert_not_called()
        self.assertEqual(self.receipts(), before)

    def test_download_failure_restores_exact_receipts(self):
        before = self.receipts()
        with (
            patch(
                "cardputer_mcp.source_snapshot.RepositoryArchives.fetch",
                side_effect=SourceError("source_fetch_interrupted"),
            ),
            self.assertRaisesRegex(SourceError, "source_fetch_interrupted"),
        ):
            self.fetch(4)
        self.assertEqual(self.receipts(), before)
        self.assertFalse(self.snapshots.uncertain)

    def test_partial_second_removal_restores_all_receipts_and_marks_uncertain(self):
        before = self.receipts()
        original = shutil.rmtree
        removed = []

        def fail_second(path, *args, **kwargs):
            if path.name in before and self.snapshots._evidence_only(path):
                removed.append(path.name)
                if len(removed) == 2:
                    (path / "receipt.json").unlink()
                    raise OSError("synthetic partial removal")
            return original(path, *args, **kwargs)

        with (
            patch("cardputer_mcp.source_payloads.EVIDENCE_SNAPSHOTS", 2),
            patch("cardputer_mcp.source_snapshot.EVIDENCE_SNAPSHOTS", 2),
            patch("shutil.rmtree", fail_second),
            self.assertRaisesRegex(SourceError, "source_cleanup_unconfirmed"),
        ):
            self.fetch(4)
        self.assertEqual(len(removed), 2)
        self.assertEqual(self.receipts(), before)
        self.assertTrue(self.snapshots.uncertain)

    def test_cancellation_between_removals_restores_receipts(self):
        before = self.receipts()
        original = shutil.rmtree
        cancelled = False

        def cancel_after_removal(path, *args, **kwargs):
            nonlocal cancelled
            original(path, *args, **kwargs)
            if path.name in before:
                cancelled = True

        with (
            patch("cardputer_mcp.source_payloads.EVIDENCE_SNAPSHOTS", 2),
            patch("cardputer_mcp.source_snapshot.EVIDENCE_SNAPSHOTS", 2),
            patch("shutil.rmtree", cancel_after_removal),
            self.assertRaisesRegex(SourceError, "source_fetch_interrupted"),
        ):
            self.fetch(4, live=lambda: not cancelled)
        self.assertTrue(cancelled)
        self.assertEqual(self.receipts(), before)

    def test_failed_restore_is_uncertain_and_never_overwrites_changed_receipt(self):
        before = self.receipts()
        first = self.first[0]["source_receipt"]
        path = self.root / "cache" / first

        def replace_and_fail(*args, **kwargs):
            path.mkdir(mode=0o700)
            (path / "receipt.json").write_bytes(b"unexpected replacement")
            raise SourceError("source_fetch_interrupted")

        with (
            patch("cardputer_mcp.source_snapshot.RepositoryArchives.fetch", replace_and_fail),
            self.assertRaisesRegex(SourceError, "source_cleanup_unconfirmed"),
        ):
            self.fetch(4)
        self.assertEqual((path / "receipt.json").read_bytes(), b"unexpected replacement")
        self.assertTrue(self.snapshots.uncertain)
        self.assertNotEqual(self.receipts(), before)

    def test_projected_receipt_bytes_avoid_reclaiming_protected_payload(self):
        self.add(
            {
                "job_id": "protected-build",
                "test": "firmware_build",
                "broker_epoch": "epoch",
                "project_id": "fixture",
                "build_principal": "owner",
                "source_receipt": self.first[-1]["source_receipt"],
            },
            "running",
        )
        usage = self.snapshots.usage("fixture")
        budget = usage["bytes"] + LIMITS["expanded_bytes"] + 32 * 1048576 - 1
        with patch.dict(LIMITS, cache_snapshots=2, cache_bytes=budget):
            self.fetch(4)
        self.assertTrue(self.payload(self.first[-1]).exists())
        self.assertNotIn(self.first[0]["source_receipt"], self.receipts())

    def test_new_protection_between_rotations_restores_receipts(self):
        before = self.receipts()
        original = shutil.rmtree
        protected = set()
        evidence = self.snapshots.retention

        def protect_after_removal(path, *args, **kwargs):
            original(path, *args, **kwargs)
            if path.name in before:
                protected.update(before)

        with (
            patch("cardputer_mcp.source_payloads.EVIDENCE_SNAPSHOTS", 2),
            patch("cardputer_mcp.source_snapshot.EVIDENCE_SNAPSHOTS", 2),
            patch("shutil.rmtree", protect_after_removal),
            patch.object(
                self.snapshots, "retention", lambda: evidence() | {"protected": protected}
            ),
            self.assertRaisesRegex(SourceError, "source_cache_protected"),
        ):
            self.fetch(4)
        self.assertEqual(self.receipts(), before)
