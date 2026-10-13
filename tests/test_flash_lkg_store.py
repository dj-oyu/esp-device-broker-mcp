"""Private hash retention and atomic successful-job selection, no device IO."""

import tempfile
import unittest
from pathlib import Path

from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.flash_lkg_store import FirmwareSnapshots
from cardputer_mcp.store import Store


class PersistenceTests(unittest.TestCase):
    def test_hash_objects_are_shared_and_survive_source_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshots = FirmwareSnapshots(root / "private", windows_private_acl_confirmed=True)
            first = snapshots.stage({"device": "synthetic", "job": "one"}, {"app.bin": b"image"})
            second = snapshots.stage({"device": "synthetic", "job": "two"}, {"app.bin": b"image"})
            self.assertNotEqual(first, second)
            self.assertEqual(len(list(snapshots.root.iterdir())), 3)
            self.assertEqual(snapshots.load(first)[1], {"app.bin": b"image"})

    def test_missing_immutable_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            snapshots = FirmwareSnapshots(Path(temp), windows_private_acl_confirmed=True)
            receipt = snapshots.stage({"device": "synthetic"}, {"app.bin": b"image"})
            image = snapshots.put(b"image")
            (snapshots.root / image).unlink()
            with self.assertRaises(FlashFailure):
                snapshots.load(receipt)

    def test_crash_before_commit_preserves_previous_successful_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state.sqlite3")
            promotion = dict(
                device_id="fixture", principal="owner", receipt="a" * 64,
                kind="artifact_bundle", baseline="initial-backup",
            )
            store.put_plan("a" * 64, {})
            store.add_job("one", "a" * 64)
            store.set_job("one", "running")
            store.finish_firmware("one", "verified", promotion, lambda: True)
            store.add_job("two", "a" * 64)
            store.set_job("two", "running")
            calls = 0

            def crash():
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise KeyboardInterrupt
                return True

            with self.assertRaises(KeyboardInterrupt):
                store.finish_firmware("two", "verified", promotion | {"receipt": "b" * 64}, crash)
            store.db.close()
            restored = Store(Path(temp) / "state.sqlite3")
            restored.recover()
            self.assertEqual(restored.firmware_current("fixture", "owner")["receipt"], "a" * 64)
            self.assertIsNone(restored.firmware_known("fixture", "owner", "b" * 64))
            self.assertEqual(restored.job("two")["state"], "needs_review")
            restored.db.close()

    def test_failed_final_authority_check_keeps_old_pointer_and_running_job(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp) / "state.sqlite3")
            promotion = dict(
                device_id="fixture",
                principal="owner",
                receipt="a" * 64,
                kind="artifact_bundle",
                baseline="initial-backup",
            )
            store.put_plan("a" * 64, {})
            store.add_job("one", "a" * 64)
            store.set_job("one", "running")
            store.finish_firmware("one", "verified", promotion, lambda: True)
            before = store.firmware_current("fixture", "owner")
            store.add_job("two", "a" * 64)
            store.set_job("two", "running")
            checks = iter((True, False))
            with self.assertRaises(ValueError):
                store.finish_firmware(
                    "two", "verified", promotion | {"receipt": "b" * 64}, lambda: next(checks)
                )
            self.assertEqual(store.firmware_current("fixture", "owner"), before)
            self.assertEqual(store.job("two")["state"], "running")
            store.db.close()
            reopened = Store(Path(temp) / "state.sqlite3")
            reopened.recover()
            self.assertEqual(reopened.firmware_current("fixture", "owner"), before)
            self.assertEqual(reopened.job("two")["state"], "needs_review")
            reopened.db.close()


if __name__ == "__main__":
    unittest.main()
