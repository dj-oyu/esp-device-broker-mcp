"""Strict local configuration. Real serial mode additionally requires runtime opt-in."""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import Digest
from .ports import DeviceSelector


class SerialSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selector: DeviceSelector
    protocol: Literal["pocketjs-hello-v1"] = "pocketjs-hello-v1"
    expected_firmware_sha256: Digest | None = None
    observe_seconds: float = Field(default=2, ge=0.1, le=10)


class TunnelMetadata(BaseModel):
    """Optional, non-secret Tunnel settings; loading it never opens a tunnel."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    tunnel_id: str | None = Field(default=None, pattern=r"^(?:tunnel_[a-f0-9]{32})?$")
    client_exe: Path | None = None
    client_archive: Path | None = None
    health_port: int = Field(default=8766, strict=True, ge=1024, le=65535)
    ingress_enabled: bool = Field(default=False, strict=True)

    @field_validator("client_exe", "client_archive", mode="before")
    @classmethod
    def reject_empty_paths(cls, value: object) -> object:
        if isinstance(value, str) and (not value.strip() or "\x00" in value):
            raise ValueError("invalid_tunnel_client_path")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    backend: Literal["mock", "serial"] = "mock"
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    state_dir: Path = Path(".state")
    allow_mock_execution: bool = False
    mock_stage_seconds: float = Field(default=0.15, ge=0.001, le=10)
    max_queue: int = Field(default=32, ge=1, le=256)
    max_work_sessions: int = Field(default=128, ge=1, le=1024)
    max_waiters: int = Field(default=32, ge=1, le=256)
    log_capacity: int = Field(default=256, ge=4, le=10000)
    serial: SerialSettings | None = None
    tunnel: TunnelMetadata | None = None

    @model_validator(mode="after")
    def guard_real_mode(self) -> "Settings":
        if self.backend == "serial" and (self.serial is None or self.allow_mock_execution):
            raise ValueError("serial mode needs a device selector and per-plan local approval")
        if self.backend == "mock" and self.serial is not None:
            raise ValueError("serial configuration is not accepted in mock mode")
        return self


def load_settings(path: Path | None) -> Settings:
    if path is None:
        return Settings()
    with path.open("rb") as handle:
        return Settings.model_validate(tomllib.load(handle))
