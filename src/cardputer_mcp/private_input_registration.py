"""Trusted local declarations only; no ingestion or runtime delivery yet."""

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .private_input_contract import _path


class PrivateInputRegistration(BaseModel):
    """An exact input/build pairing, never accepted in MCP request parameters.

    Parsing this declaration does not read the directory or authorize ingestion.
    Future consumers must use owned receipts, not caller-provided declarations.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    local_root: Path = Field(repr=False, exclude=True)
    destination: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    max_files: int = Field(default=100_000, strict=True, ge=1, le=100_000)
    max_bytes: int = Field(default=256 * 1024 * 1024, strict=True, ge=1, le=256 * 1024 * 1024)

    @field_validator("local_root")
    @classmethod
    def absolute_root(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or "\x00" in str(value):
            raise ValueError("absolute_private_input_root_required")
        return value

    @field_validator("destination")
    @classmethod
    def portable_destination(cls, value: str) -> str:
        _path(value)
        return value

    def identity(self, *, project_id: str, input_id: str) -> str:
        """Bind registration including its private location without exposing it."""
        record = self.model_dump(mode="json") | {
            "schema": "private-input-registration-v1",
            "project_id": project_id,
            "input_id": input_id,
            "local_root": str(self.local_root),
        }
        raw = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(raw.encode("ascii")).hexdigest()
