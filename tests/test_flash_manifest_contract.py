"""Offline admission tests; no process, USB or physical device operation."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from cardputer_mcp import flash_manifest as module  # noqa: E402


class FlashManifestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.layout = {
            "write_flash_args": [
                "--flash-mode",
                "dio",
                "--flash-size",
                "8MB",
                "--flash-freq",
                "80m",
            ],
            "flash_settings": {"flash_mode": "dio", "flash_size": "8MB", "flash_freq": "80m"},
            "flash_files": {"0x0": "boot.bin", "0x10000": "app.bin"},
            "extra_esptool_args": {"chip": "esp32s3"},
            "app": {"offset": "0x10000", "file": "app.bin", "encrypted": "false"},
        }
        self.manifest = {
            "schema_version": 1,
            "idf_version": "6.0.1",
            "target": "esp32s3",
            "fingerprint": "a" * 64,
            "environment_fingerprint": "b" * 64,
            "flash_authorized": False,
            "artifacts": [],
        }
        security = json.dumps(
            {
                "SECURE_BOOT": False,
                "SECURE_FLASH_ENC_ENABLED": False,
                "BOOTLOADER_APP_ANTI_ROLLBACK": False,
                "BOOTLOADER_APP_ROLLBACK_ENABLE": False,
                "BOOT_ROM_LOG_ALWAYS_ON": True,
            }
        ).encode()
        (self.root / "config").mkdir()
        (self.root / "config/sdkconfig.json").write_bytes(security)
        self.manifest["flash_security"] = self.receipt("config/sdkconfig.json", security)
        for name in ("boot.bin", "app.bin"):
            data = b"synthetic-contract-only" * 8
            (self.root / name).write_bytes(data)
            self.manifest["artifacts"].append(self.receipt(name, data))
        self.save_layout()

    def receipt(self, name, data):
        return {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def save_layout(self):
        data = json.dumps(self.layout).encode()
        (self.root / "flasher_args.json").write_bytes(data)
        self.manifest["flash_layout"] = self.receipt("flasher_args.json", data)

    def verify(self, manifest=None):
        return module.verify_bundle(
            self.root, manifest or self.manifest, flash_bytes=8 * 1024 * 1024
        )

    def test_verified_layout_is_inert_and_manifest_bound(self):
        bundle = self.verify()
        self.assertEqual([segment.offset for segment in bundle.segments], [0, 0x10000])
        self.assertEqual(bundle.layout_sha256, self.manifest["flash_layout"]["sha256"])
        self.assertFalse(self.manifest["flash_authorized"])

    def test_security_build_configuration_never_authorizes_first_boot_fuse_changes(self):
        for key in ("SECURE_BOOT", "SECURE_FLASH_ENC_ENABLED", "BOOTLOADER_APP_ANTI_ROLLBACK"):
            security = {
                "SECURE_BOOT": False,
                "SECURE_FLASH_ENC_ENABLED": False,
                "BOOTLOADER_APP_ANTI_ROLLBACK": False,
                "BOOTLOADER_APP_ROLLBACK_ENABLE": False,
                "BOOT_ROM_LOG_ALWAYS_ON": True,
            }
            security[key] = True
            data = json.dumps(security).encode()
            (self.root / "config/sdkconfig.json").write_bytes(data)
            self.manifest["flash_security"] = self.receipt("config/sdkconfig.json", data)
            with self.assertRaisesRegex(
                module.FlashAdmissionError, "security_configuration_not_authorized"
            ):
                self.verify()

    def test_changed_binary_and_layout_are_rejected(self):
        for name in ("app.bin", "flasher_args.json"):
            with self.subTest(name=name):
                path = self.root / name
                saved = path.read_bytes()
                path.write_bytes(saved[:-1] + b"!")
                with self.assertRaisesRegex(module.FlashAdmissionError, "hash_mismatch"):
                    self.verify()
                path.write_bytes(saved)

    def test_erase_all_arguments_and_encryption_are_rejected(self):
        self.layout["write_flash_args"].append("--erase-all")
        self.save_layout()
        with self.assertRaisesRegex(module.FlashAdmissionError, "unsupported_flash_arguments"):
            self.verify()
        self.layout["write_flash_args"].pop()
        self.layout["app"]["encrypted"] = "true"
        self.save_layout()
        with self.assertRaisesRegex(module.FlashAdmissionError, "encrypted_flash_not_authorized"):
            self.verify()


if __name__ == "__main__":
    unittest.main()
