"""ESP-IDF 6.0.1 enable gates, distinct from capability/preference symbols."""

from typing import Any


def validate_build_security(config: dict[str, Any]) -> None:
    def deny() -> None:
        raise ValueError("build_security_configuration_not_authorized")
    if config.get("SECURE_BOOT") is not False or config.get("SECURE_FLASH_ENC_ENABLED") is not False:
        deny()
    # Disabled-parent omission is permitted only for this explicit dependency.
    anti = config.get("BOOTLOADER_APP_ANTI_ROLLBACK")
    if anti is not False and not (anti is None and config.get("BOOTLOADER_APP_ROLLBACK_ENABLE") is False):
        deny()
    for key in ("SECURE_BOOT_V1_ENABLED", "SECURE_BOOT_V2_ENABLED", "SECURE_SIGNED_APPS_NO_SECURE_BOOT"):
        if config.get(key) is True:
            deny()
    # These choice symbols change security/download eFuses during startup.
    for key in ("SECURE_DISABLE_ROM_DL_MODE", "SECURE_ENABLE_SECURE_ROM_DL_MODE"):
        value = config.get(key)
        disabled_parent = config.get("SECURE_BOOT") is False and config.get("SECURE_BOOT_V2_ENABLED") in (None, False) and config.get("SECURE_FLASH_ENC_ENABLED") is False
        if value is not False and not (value is None and disabled_parent):
            deny()
    for key in ("BOOT_ROM_LOG_ALWAYS_OFF", "BOOT_ROM_LOG_ON_GPIO_HIGH", "BOOT_ROM_LOG_ON_GPIO_LOW"):
        value = config.get(key)
        if value is not False and not (value is None and config.get("BOOT_ROM_LOG_ALWAYS_ON") is True):
            deny()
    # SECURE_BOOT_V2_PREFERRED/RSA_SUPPORTED/ECDSA_SUPPORTED describe selection
    # or capability; they do not enable secure boot while SECURE_BOOT is false.
    capabilities = {
        "SECURE_BOOT_V1_SUPPORTED", "SECURE_BOOT_V2_RSA_SUPPORTED", "SECURE_BOOT_V2_ECC_SUPPORTED",
        "SECURE_BOOT_V2_ECDSA_SUPPORTED", "SECURE_BOOT_V2_PREFERRED", "SECURE_ROM_DL_MODE_ENABLED",
        "SECURE_FLASH_HAS_WRITE_PROTECTION_CACHE", "SECURE_INSECURE_ALLOW_DL_MODE",
    }
    for key, value in config.items():
        if key.startswith("SECURE_") and value is True and key not in capabilities:
            deny()
