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


class FlashDevice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selector: DeviceSelector
    flash_bytes: Literal[2097152, 4194304, 8388608, 16777216, 33554432]
    project_id: Identifier
    python_tool: FlashPython
    esptool_directory: Path
    esptool_sha256: Digest
    esptool_version: Literal["5.1.0"] = "5.1.0"
    backup_directory: Path
    private_backup_acl_confirmed: bool = Field(default=False, strict=True)
    allow_backup: bool = Field(default=False, strict=True)
    allow_flash: bool = Field(default=False, strict=True)
    allow_restore: bool = Field(default=False, strict=True)

    @field_validator("esptool_directory", "backup_directory")
    @classmethod
    def absolute_directory(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("absolute_registered_directory_required")
        return value
