"""IDF 6.0.1 dependency fixture, no private SDK values or paths."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_security import validate_build_security


class SecurityPolicyTests(unittest.TestCase):
    def fixture(self):
        return {"SECURE_BOOT": False, "SECURE_FLASH_ENC_ENABLED": False,
                "BOOTLOADER_APP_ROLLBACK_ENABLE": False, "SECURE_BOOT_V2_PREFERRED": True,
                "SECURE_BOOT_V2_RSA_SUPPORTED": True, "BOOT_ROM_LOG_ALWAYS_ON": True}

    def test_disabled_parent_omission_and_capabilities_are_accepted(self):
        data = self.fixture()
        self.assertNotIn("BOOTLOADER_APP_ANTI_ROLLBACK", data)
        validate_build_security(data)

    def test_enable_unknown_type_and_uncertain_missing_are_rejected(self):
        for key, value in (
            ("SECURE_BOOT", True), ("SECURE_FLASH_ENC_ENABLED", True),
            ("SECURE_BOOT", None), ("SECURE_FLASH_ENC_ENABLED", 0),
            ("BOOTLOADER_APP_ROLLBACK_ENABLE", None), ("BOOTLOADER_APP_ROLLBACK_ENABLE", True),
            ("SECURE_DISABLE_ROM_DL_MODE", True), ("SECURE_ENABLE_SECURE_ROM_DL_MODE", True),
            ("SECURE_UNKNOWN_FUSE_WRITE", True), ("BOOT_ROM_LOG_ALWAYS_OFF", True),
        ):
            data = self.fixture()
            data[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_build_security(data)

    def test_explicit_anti_rollback_enable_is_rejected_even_with_disabled_parent(self):
        data = self.fixture()
        data["BOOTLOADER_APP_ANTI_ROLLBACK"] = True
        with self.assertRaises(ValueError):
            validate_build_security(data)


if __name__ == "__main__":
    unittest.main()
