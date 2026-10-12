"""Build receipts bind generated flash offsets without granting flash permission."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardputer_mcp.firmware_build import (
    BuildFlashLayoutFailure,
    BuildFlashSecurityFailure,
    build_flash_security_receipt,
    build_flash_layout_receipt,
)


class BuildFlashLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "firmware.bin"
        self.binary.write_bytes(b"synthetic firmware")
        self.artifacts = [
            {
                "name": "firmware.bin",
                "bytes": self.binary.stat().st_size,
                "sha256": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
            }
        ]
        self.layout = {
            "flash_files": {"0x10000": "firmware.bin"},
            "extra_esptool_args": {"chip": "esp32s3"},
            "flash_settings": {"flash_mode": "dio", "flash_size": "8MB", "flash_freq": "80m"},
            "write_flash_args": [
                "--flash-mode",
                "dio",
                "--flash-size",
                "8MB",
                "--flash-freq",
                "80m",
            ],
        }
        self.write_layout()

    def write_layout(self):
        self.raw = json.dumps(self.layout, indent=2).encode()
        (self.root / "flasher_args.json").write_bytes(self.raw)

    def verify(self):
        return build_flash_layout_receipt(self.root, "esp32s3", self.artifacts)

    def test_exact_bytes_receipt(self):
        receipt = self.verify()
        self.assertEqual(
            receipt,
            {
                "name": "flasher_args.json",
                "bytes": len(self.raw),
                "sha256": hashlib.sha256(self.raw).hexdigest(),
            },
        )

    def test_wrong_target_and_missing_layout_fail_closed(self):
        self.layout["extra_esptool_args"]["chip"] = "esp32p4"
        self.write_layout()
        with self.assertRaises(BuildFlashLayoutFailure):
            self.verify()
        (self.root / "flasher_args.json").unlink()
        with self.assertRaises(BuildFlashLayoutFailure):
            self.verify()

    def test_oversized_malformed_and_duplicate_json(self):
        for raw in (b" " * 65537, b"not-json", b'{"flash_files":{},"flash_files":{}}'):
            (self.root / "flasher_args.json").write_bytes(raw)
            with self.assertRaises(BuildFlashLayoutFailure):
                self.verify()

    def test_escape_absolute_and_unattested_binary_paths(self):
        for name in ("../outside.bin", "/outside.bin", "C:\\outside.bin", "missing.bin"):
            self.layout["flash_files"] = {"0x10000": name}
            self.write_layout()
            with self.assertRaises(BuildFlashLayoutFailure):
                self.verify()

    def test_changed_size_or_hash_refuses_receipt(self):
        self.binary.write_bytes(b"changed firmware")
        with self.assertRaises(BuildFlashLayoutFailure):
            self.verify()
        self.binary.write_bytes(b"synthetic firmwarX")
        with self.assertRaises(BuildFlashLayoutFailure):
            self.verify()

    def test_linked_layout_is_rejected(self):
        outside = self.root / "outside.json"
        outside.write_bytes(self.raw)
        path = self.root / "flasher_args.json"
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaises(BuildFlashLayoutFailure):
            self.verify()

    def test_reparse_marker_is_rejected(self):
        original = Path.lstat

        def reparse(path):
            value = original(path)
            if path.name == "flasher_args.json":
                from types import SimpleNamespace

                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value

        with patch.object(Path, "lstat", reparse):
            with self.assertRaises(BuildFlashLayoutFailure):
                self.verify()

    def test_duplicate_numeric_offsets_are_rejected(self):
        self.layout["flash_files"] = {"0x10000": "firmware.bin", "0x00010000": "firmware.bin"}
        self.write_layout()
        with self.assertRaises(BuildFlashLayoutFailure):
            self.verify()


class BuildFlashSecurityTests(unittest.TestCase):
    def test_security_receipt_without_secret_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            config = {
                "IDF_TARGET": "esp32s3",
                "SECURE_BOOT": False,
                "SECURE_FLASH_ENC_ENABLED": False,
                "BOOTLOADER_APP_ANTI_ROLLBACK": False,
                "WIFI_PASSWORD": "private-example-value",
            }
            raw = json.dumps(config).encode()
            (root / "config/sdkconfig.json").write_bytes(raw)
            receipt, summary = build_flash_security_receipt(root, "esp32s3")
            self.assertEqual(receipt["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(receipt["bytes"], len(raw))
            self.assertNotIn("private-example-value", str((receipt, summary)))
            self.assertFalse(summary["secure_boot"])
            self.assertEqual(summary["flash_admission"], "not_evaluated")
            config["SECURE_BOOT"] = True
            (root / "config/sdkconfig.json").write_text(json.dumps(config))
            _, summary = build_flash_security_receipt(root, "esp32s3")
            self.assertTrue(summary["secure_boot"])

    def test_missing_malformed_and_wrong_target_config_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "config").mkdir()
            for raw in (b"{}", b"not-json", b" " * 1048577):
                (root / "config/sdkconfig.json").write_bytes(raw)
                with self.assertRaises(BuildFlashSecurityFailure):
                    build_flash_security_receipt(root, "esp32s3")
            (root / "config/sdkconfig.json").unlink()
            with self.assertRaises(BuildFlashSecurityFailure):
                build_flash_security_receipt(root, "esp32s3")
