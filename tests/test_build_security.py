"""Real IDF6.0.1 ordinary projection and strict dangerous/unknown negatives."""

import unittest

from cardputer_mcp.build_security import BuildSecurityConfigError, interpret_idf601_security


class IDF601SecurityTests(unittest.TestCase):
    def fixture(self):
        return {
            "SECURE_BOOT": False,
            "SECURE_FLASH_ENC_ENABLED": False,
            "BOOTLOADER_APP_ROLLBACK_ENABLE": False,
            "SECURE_BOOT_V2_PREFERRED": True,
            "SECURE_BOOT_V2_RSA_SUPPORTED": True,
        }

    def test_actual_ordinary_projection(self):
        value = interpret_idf601_security(self.fixture())
        self.assertFalse(value["secure_boot"])
        self.assertFalse(value["flash_encryption"])
        self.assertFalse(value["anti_rollback"])
        self.assertFalse(value["additional_security_enabled"])
        self.assertTrue(value["anti_rollback_inferred_disabled"])

    def test_capabilities_are_not_activation(self):
        config = self.fixture()
        config["SECURE_BOOT_V2_ECC_SUPPORTED"] = True
        config["SECURE_ROM_DL_MODE_ENABLED"] = True
        self.assertFalse(interpret_idf601_security(config)["additional_security_enabled"])

    def test_real_enable_remains_dangerous(self):
        for key in (
            "SECURE_BOOT",
            "SECURE_FLASH_ENC_ENABLED",
            "SECURE_BOOT_V2_ENABLED",
            "SECURE_SIGNED_APPS_NO_SECURE_BOOT",
            "SECURE_DISABLE_ROM_DL_MODE",
        ):
            config = self.fixture() | {key: True}
            result = interpret_idf601_security(config)
            self.assertTrue(
                any(
                    result[name]
                    for name in ("secure_boot", "flash_encryption", "additional_security_enabled")
                )
            )
        config = self.fixture() | {
            "BOOTLOADER_APP_ROLLBACK_ENABLE": True,
            "BOOTLOADER_APP_ANTI_ROLLBACK": True,
        }
        self.assertTrue(interpret_idf601_security(config)["anti_rollback"])

    def test_unknown_true_is_not_a_capability_exemption(self):
        for key in (
            "SECURE_BOOT_FUTURE_SUPPORTED",
            "SECURE_BOOT_NEW_FEATURE",
            "SECURE_FLASH_ENCRYPTION_UNKNOWN",
            "SECURE_ROM_DL_FUTURE_ENABLE",
        ):
            with self.assertRaises(BuildSecurityConfigError):
                interpret_idf601_security(self.fixture() | {key: True})

    def test_uncertain_missing_and_malformed_values_refused(self):
        for change in (
            {"BOOTLOADER_APP_ROLLBACK_ENABLE": True},
            {"BOOTLOADER_APP_ROLLBACK_ENABLE": None},
            {"SECURE_BOOT": None},
            {"SECURE_FLASH_ENC_ENABLED": "n"},
            {"SECURE_BOOT_V2_PREFERRED": "y"},
        ):
            with self.assertRaises(BuildSecurityConfigError):
                interpret_idf601_security(self.fixture() | change)
        config = self.fixture()
        del config["BOOTLOADER_APP_ROLLBACK_ENABLE"]
        with self.assertRaises(BuildSecurityConfigError):
            interpret_idf601_security(config)

    def test_explicit_disabled_antirollback_does_not_need_inference(self):
        config = self.fixture() | {"BOOTLOADER_APP_ANTI_ROLLBACK": False}
        del config["BOOTLOADER_APP_ROLLBACK_ENABLE"]
        self.assertFalse(interpret_idf601_security(config)["anti_rollback_inferred_disabled"])
