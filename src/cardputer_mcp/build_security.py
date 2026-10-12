"""ESP-IDF 6.0.1 generated security policy interpretation; no I/O or flash grant.

Dependencies and capability definitions:
https://github.com/espressif/esp-idf/blob/v6.0.1/components/bootloader/Kconfig.app_rollback
https://github.com/espressif/esp-idf/blob/v6.0.1/components/bootloader/Kconfig.projbuild
"""

from typing import Any


class BuildSecurityConfigError(ValueError):
    def __init__(self) -> None:
        super().__init__("build_security_configuration_invalid")


CAPABILITY_FLAGS = frozenset({
    "SECURE_BOOT_V1_SUPPORTED", "SECURE_BOOT_V2_RSA_SUPPORTED",
    "SECURE_BOOT_V2_ECC_SUPPORTED", "SECURE_BOOT_V2_PREFERRED",
    "SECURE_ROM_DL_MODE_ENABLED",
})
# Exact 6.0.1 symbols, not a suffix/prefix allowlist for future capabilities.
ENABLE_FLAGS = frozenset(['SECURE_SIGNED_ON_BOOT', 'SECURE_SIGNED_ON_UPDATE', 'SECURE_SIGNED_APPS', 'SECURE_BOOT_V2_ECDSA_ENABLED', 'SECURE_BOOT_V2_RSA_ENABLED', 'SECURE_BOOT_FLASH_ENC_KEYS_BURN_TOGETHER', 'SECURE_SIGNED_APPS_NO_SECURE_BOOT', 'SECURE_SIGNED_APPS_ECDSA_SCHEME', 'SECURE_SIGNED_APPS_RSA_SCHEME', 'SECURE_SIGNED_APPS_ECDSA_V2_SCHEME', 'SECURE_BOOT_ECDSA_KEY_LEN_192_BITS', 'SECURE_BOOT_ECDSA_KEY_LEN_256_BITS', 'SECURE_BOOT_ECDSA_KEY_LEN_384_BITS', 'SECURE_SIGNED_ON_BOOT_NO_SECURE_BOOT', 'SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT', 'SECURE_BOOT', 'SECURE_BOOT_V1_ENABLED', 'SECURE_BOOT_V2_ENABLED', 'SECURE_BOOTLOADER_ONE_TIME_FLASH', 'SECURE_BOOTLOADER_REFLASHABLE', 'SECURE_BOOT_BUILD_SIGNED_BINARIES', 'SECURE_BOOT_ENABLE_AGGRESSIVE_KEY_REVOKE', 'SECURE_BOOT_V2_ALLOW_EFUSE_RD_DIS', 'SECURE_BOOT_FLASH_BOOTLOADER_DEFAULT', 'SECURE_BOOTLOADER_KEY_ENCODING_256BIT', 'SECURE_BOOTLOADER_KEY_ENCODING_192BIT', 'SECURE_BOOT_INSECURE', 'SECURE_FLASH_ENC_ENABLED', 'SECURE_FLASH_ENCRYPTION_KEY_SOURCE_EFUSES', 'SECURE_FLASH_ENCRYPTION_KEY_SOURCE_KEY_MGR', 'SECURE_FLASH_ENCRYPTION_AES128_DERIVED', 'SECURE_FLASH_ENCRYPTION_AES128', 'SECURE_FLASH_ENCRYPTION_AES256', 'SECURE_FLASH_ENCRYPTION_MODE_DEVELOPMENT', 'SECURE_FLASH_ENCRYPTION_MODE_RELEASE', 'SECURE_FLASH_HAS_WRITE_PROTECTION_CACHE', 'SECURE_BOOT_ALLOW_ROM_BASIC', 'SECURE_BOOT_ALLOW_JTAG', 'SECURE_BOOT_ALLOW_SHORT_APP_PARTITION', 'SECURE_BOOT_ALLOW_UNUSED_DIGEST_SLOTS', 'SECURE_BOOT_SKIP_WRITE_PROTECTION_SCA', 'SECURE_FLASH_UART_BOOTLOADER_ALLOW_ENC', 'SECURE_FLASH_UART_BOOTLOADER_ALLOW_DEC', 'SECURE_FLASH_UART_BOOTLOADER_ALLOW_CACHE', 'SECURE_FLASH_REQUIRE_ALREADY_ENABLED', 'SECURE_FLASH_SKIP_WRITE_PROTECTION_CACHE', 'SECURE_FLASH_ENCRYPT_ONLY_IMAGE_LEN_IN_APP_PART', 'SECURE_FLASH_CHECK_ENC_EN_IN_APP', 'SECURE_FLASH_PSEUDO_ROUND_FUNC', 'SECURE_FLASH_PSEUDO_ROUND_FUNC_STRENGTH_LOW', 'SECURE_FLASH_PSEUDO_ROUND_FUNC_STRENGTH_MEDIUM', 'SECURE_FLASH_PSEUDO_ROUND_FUNC_STRENGTH_HIGH', 'SECURE_ROM_DL_MODE_ENABLED', 'SECURE_DISABLE_ROM_DL_MODE', 'SECURE_ENABLE_SECURE_ROM_DL_MODE', 'SECURE_INSECURE_ALLOW_DL_MODE'])
PROTECTED_PREFIXES = ("SECURE_BOOT", "SECURE_FLASH", "SECURE_SIGNED_APPS", "SECURE_SIGNED_ON",
                      "SECURE_DISABLE_ROM_DL_MODE", "SECURE_ENABLE_SECURE_ROM_DL_MODE",
                      "SECURE_ROM_DL", "SECURE_INSECURE_ALLOW_DL_MODE")
SCALAR_OPTIONS = {"SECURE_BOOT_SIGNING_KEY": str, "SECURE_BOOT_VERIFICATION_KEY": str,
                  "SECURE_FLASH_PSEUDO_ROUND_FUNC_STRENGTH": int}


def _explicit_bool(config: dict[str, Any], key: str) -> bool:
    value = config.get(key)
    if type(value) is not bool:
        raise BuildSecurityConfigError()
    return value


def interpret_idf601_security(config: dict[str, Any]) -> dict[str, Any]:
    """Missing antirollback is disabled only when its dependency is explicit false.

    Capability/preference booleans are not activation. Unknown true security flags
    and malformed flag types remain rejected; no general missing=false inference.
    The result is a summary, never a flash admission or device-security assertion.
    """
    boot = _explicit_bool(config, "SECURE_BOOT")
    encryption = _explicit_bool(config, "SECURE_FLASH_ENC_ENABLED")
    inferred = "BOOTLOADER_APP_ANTI_ROLLBACK" not in config
    if inferred:
        if _explicit_bool(config, "BOOTLOADER_APP_ROLLBACK_ENABLE") is not False:
            raise BuildSecurityConfigError()
        antirollback = False
    else:
        antirollback = _explicit_bool(config, "BOOTLOADER_APP_ANTI_ROLLBACK")
        if "BOOTLOADER_APP_ROLLBACK_ENABLE" in config:
            rollback = _explicit_bool(config, "BOOTLOADER_APP_ROLLBACK_ENABLE")
            if antirollback and not rollback:
                raise BuildSecurityConfigError()
    additional = False
    for key, value in config.items():
        if not key.startswith(PROTECTED_PREFIXES):
            continue
        if key in SCALAR_OPTIONS:
            if type(value) is not SCALAR_OPTIONS[key]:
                raise BuildSecurityConfigError()
            continue
        if type(value) is not bool:
            raise BuildSecurityConfigError()
        if key in CAPABILITY_FLAGS or value is False:
            continue
        if key not in ENABLE_FLAGS:
            raise BuildSecurityConfigError()
        if key not in ("SECURE_BOOT", "SECURE_FLASH_ENC_ENABLED"):
            additional = True
    return {"secure_boot": boot, "flash_encryption": encryption, "anti_rollback": antirollback,
            "anti_rollback_inferred_disabled": inferred, "additional_security_enabled": additional,
            "flash_admission": "not_evaluated"}
