"""Synthetic backup files only; no device or user credentials."""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_backup import BackupStore
from cardputer_mcp.flash_lifecycle import BackupReceipt, FlashFailure


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "private"
        self.store = BackupStore(self.directory, windows_private_acl_confirmed=True)
        self.data = b"synthetic-backup-data"
        self.receipt = BackupReceipt(
            "a" * 64, "b" * 64, len(self.data), hashlib.sha256(self.data).hexdigest()
        )

    def test_private_receipt_roundtrip_and_principal_binding(self):
        saved = self.store.save("backup-job", "principal", self.receipt, self.data)
        retained, data = self.store.load("backup-job", "principal")
        self.assertEqual(saved, retained)
        self.assertEqual(data, self.data)
        with self.assertRaisesRegex(FlashFailure, "owner_mismatch"):
            self.store.load("backup-job", "other")

    def test_tampering_fails_closed(self):
        self.store.save("backup-job", "principal", self.receipt, self.data)
        (self.directory / "backup-job.bin").write_bytes(b"!" + self.data[1:])
        with self.assertRaisesRegex(FlashFailure, "missing_or_changed"):
            self.store.load("backup-job", "principal")

    def test_unknown_backup_never_restores(self):
        with self.assertRaisesRegex(FlashFailure, "missing_or_changed"):
            self.store.load("unknown", "principal")

    def test_remote_path_and_existing_backup_cannot_overwrite(self):
        with self.assertRaisesRegex(FlashFailure, "invalid_backup_job"):
            self.store.save("../escape", "principal", self.receipt, self.data)
        self.store.save("backup-job", "principal", self.receipt, self.data)
        with self.assertRaises(FileExistsError):
            self.store.save("backup-job", "principal", self.receipt, self.data)

    @unittest.skipUnless(os.name == "posix", "POSIX privacy receipt")
    def test_relaxed_backup_permissions_are_rejected(self):
        self.store.save("backup-job", "principal", self.receipt, self.data)
        (self.directory / "backup-job.bin").chmod(0o644)
        with self.assertRaisesRegex(FlashFailure, "missing_or_changed"):
            self.store.load("backup-job", "principal")


if __name__ == "__main__":
    unittest.main()
