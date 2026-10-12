"""ESP-IDF 6.0.1 enable gates, distinct from capability/preference symbols."""

from typing import Any


def validate_build_security(config: dict[str, Any]) -> None:
    from .build_security import interpret_idf601_security

    summary = interpret_idf601_security(config)
    if any(
        summary[key] is not False
        for key in (
            "secure_boot",
            "flash_encryption",
            "anti_rollback",
            "additional_security_enabled",
        )
    ):
        raise ValueError("build_security_configuration_not_authorized")
    # ROM logging choice changes are also eFuse effects, outside secure-boot
    # activation. Require explicit harmless choice/disabled leaf evidence.
    for key in ("BOOT_ROM_LOG_ALWAYS_OFF", "BOOT_ROM_LOG_ON_GPIO_HIGH", "BOOT_ROM_LOG_ON_GPIO_LOW"):
        value = config.get(key)
        if value is not False and not (
            value is None and config.get("BOOT_ROM_LOG_ALWAYS_ON") is True
        ):
            raise ValueError("build_security_configuration_not_authorized")
