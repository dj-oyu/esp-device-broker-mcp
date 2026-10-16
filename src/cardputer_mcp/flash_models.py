"""Local-only flash registration. MCP never supplies these paths or enables writes."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class UnattendedFlash(BaseModel):
    """Exact local permission; enabling a flash operation alone grants no consent."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = Field(default=False, strict=True)
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


class FlashDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selector: DeviceSelector
    flash_bytes: Literal[2097152, 4194304, 8388608, 16777216]
    project_id: Identifier
    python_tool: FlashPython
    esptool_directory: Path
    esptool_sha256: Digest
    esptool_version: Literal["5.1.0"] = "5.1.0"
    backup_directory: Path
    private_backup_acl_confirmed: bool = Field(default=False, strict=True)
    original_elf_sha256: Digest | None = None
    allow_backup: bool = Field(default=False, strict=True)
    allow_flash: bool = Field(default=False, strict=True)
    unattended_flash: UnattendedFlash | None = None
    allow_restore: bool = Field(default=False, strict=True)

    @field_validator("esptool_directory", "backup_directory")
    @classmethod
    def absolute_directory(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("absolute_registered_directory_required")
        return value
