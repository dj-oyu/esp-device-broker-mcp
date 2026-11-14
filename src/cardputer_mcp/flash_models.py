"""Local-only flash registration. MCP never supplies these paths or enables writes."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .legacy_keys import drop_removed_gates
from .models import Digest, Identifier
from .ports import DeviceSelector


class FlashPython(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    executable: Path
    sha256: Digest
    version: str = Field(pattern=r"^Python 3\.[0-9]+\.[0-9]+$")

    @field_validator("executable")
    @classmethod
    def registered_python(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or value.name.casefold() != "python.exe":
            raise ValueError("registered_external_python_required")
        return value


REMOVED_UNATTENDED_GATES = {
    "enabled": "the table's presence is what enables it now; delete the whole "
    "unattended_flash table to disable it",
}


class UnattendedFlash(BaseModel):
    """Exact local permission; its presence is what enables it (and it grants no authority).

    Older files carried ``enabled``: ``true`` is read and ignored, ``false`` is refused.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    @model_validator(mode="before")
    @classmethod
    def removed_enabled_flag(cls, data: Any) -> Any:
        return drop_removed_gates(data, REMOVED_UNATTENDED_GATES)

    principal_id: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    device_id: Identifier
    project_id: Identifier
    operation: Literal["firmware_flash"]
    source_receipt: Digest
    source_repository: str = Field(pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")

    @field_validator("source_repository")
    @classmethod
    def public_repository(cls, value: str) -> str:
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("registered_github_repository_required")
        return value


REMOVED_FLASH_GATES = {
    "allow_flash": "flash is available for every registered flash device",
    "allow_backup": "backup is available for every registered flash device",
    "allow_restore": "restore is available for every registered flash device",
    "private_backup_acl_confirmed": "the broker no longer asks for this declaration; keep "
    "the backup_directory private to this user",
}


class FlashDevice(BaseModel):
    """A registered flash device: flash, backup and restore are available for it.

    The former per-device ``allow_*`` switches and ``private_backup_acl_confirmed`` are
    read for old files (``true`` ignored, ``false`` refused); see legacy_keys.py.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    selector: DeviceSelector
    flash_bytes: Literal[2097152, 4194304, 8388608, 16777216]
    project_id: Identifier
    python_tool: FlashPython
    esptool_directory: Path
    esptool_sha256: Digest
    esptool_version: Literal["5.1.0"] = "5.1.0"
    backup_directory: Path
    original_elf_sha256: Digest | None = None
    unattended_flash: UnattendedFlash | None = None

    @model_validator(mode="before")
    @classmethod
    def removed_gates(cls, data: Any) -> Any:
        return drop_removed_gates(data, REMOVED_FLASH_GATES)

    @field_validator("esptool_directory", "backup_directory")
    @classmethod
    def absolute_directory(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("absolute_registered_directory_required")
        return value
