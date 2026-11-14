"""Strict local configuration. Real serial mode additionally requires runtime opt-in."""

import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .exposure import (
    GATEWAY_LEGACY_FLAGS,
    TUNNEL_LEGACY_FLAGS,
    WEBUI_LEGACY_FLAGS,
    GatewayMode,
    TunnelIngressMode,
    WebUIMode,
    resolve_mode,
)
from .flash_models import FlashDevice
from .legacy_keys import drop_removed_gates
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
    # "ingress" lets the broker serve the Tunnel route. The legacy `ingress_enabled`
    # boolean is still read (see exposure.py).
    mode: TunnelIngressMode = "off"

    @model_validator(mode="before")
    @classmethod
    def legacy_flags(cls, data: Any) -> Any:
        return resolve_mode(data, "tunnel", TUNNEL_LEGACY_FLAGS)

    @property
    def ingress_enabled(self) -> bool:
        return self.mode == "ingress"

    @field_validator("client_exe", "client_archive", mode="before")
    @classmethod
    def reject_empty_paths(cls, value: object) -> object:
        if isinstance(value, str) and (not value.strip() or "\x00" in value):
            raise ValueError("invalid_tunnel_client_path")
        return value


class GatewaySettings(BaseModel):
    """Optional, non-secret Claude gateway settings; loading it never opens a listener.

    The Google client secret is never read from this file.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    public_url: str = Field(
        pattern=r"^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
    )
    port: int = Field(default=8770, strict=True, ge=1024, le=65535)
    google_client_id: str = Field(
        pattern=r"^[0-9]{1,32}-[a-z0-9]{1,64}\.apps\.googleusercontent\.com$"
    )
    allowed_google_sub: str = Field(pattern=r"^[0-9]{1,64}$")
    # Private files outside repositories: the stdio-format broker key file and a
    # file holding only CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET=<value>.
    broker_env_file: Path
    google_client_secret_file: Path
    # Funnel opens only when this is explicitly "public", and closes with the gateway.
    # The legacy `funnel = true` boolean is still read (see exposure.py).
    mode: GatewayMode = "off"
    tailscale_exe: Path = Path(r"C:\Program Files\Tailscale\tailscale.exe")

    @model_validator(mode="before")
    @classmethod
    def legacy_flags(cls, data: Any) -> Any:
        return resolve_mode(data, "gateway", GATEWAY_LEGACY_FLAGS)

    @property
    def funnel(self) -> bool:
        return self.mode == "public"

    @field_validator("broker_env_file", "google_client_secret_file", "tailscale_exe")
    @classmethod
    def require_absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or "\x00" in str(value):
            raise ValueError("absolute_gateway_path_required")
        return value


class WebUISettings(BaseModel):
    """Optional, non-secret read-only web UI settings; loading it never opens a listener.

    The web UI is tailnet-only (``tailscale serve``) unless ``funnel`` is explicitly
    true, which publishes it on Funnel port 8443 or 10000. Either way it requires a
    Google login and reads the broker with a local read-only key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    # This machine's tailnet HTTPS URL with the serve port, e.g. https://host.tailnet.ts.net:8443
    public_url: str = Field(
        pattern=r"^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
        r":[1-9][0-9]{2,4}$"
    )
    port: int = Field(default=8771, strict=True, ge=1024, le=65535)
    google_client_id: str = Field(
        pattern=r"^[0-9]{1,32}-[a-z0-9]{1,64}\.apps\.googleusercontent\.com$"
    )
    allowed_google_sub: str = Field(pattern=r"^[0-9]{1,64}$")
    # Private files outside repositories: the stdio-format file holding a local
    # read-only key (`keys issue --origin local_cli`, no --control) and the file
    # holding only CARDPUTER_GATEWAY_GOOGLE_CLIENT_SECRET=<value>.
    broker_env_file: Path
    google_client_secret_file: Path
    # "off" (default) opens nothing; "tailnet" runs `tailscale serve` (tailnet only);
    # "public" runs `tailscale funnel` (public internet, Google login still required)
    # and needs a Funnel-capable port (8443 or 10000) in public_url. The legacy
    # `serve`/`funnel` booleans are still read (see exposure.py).
    mode: WebUIMode = "off"
    tailscale_exe: Path = Path(r"C:\Program Files\Tailscale\tailscale.exe")

    @model_validator(mode="before")
    @classmethod
    def legacy_flags(cls, data: Any) -> Any:
        return resolve_mode(data, "webui", WEBUI_LEGACY_FLAGS)

    @property
    def serve(self) -> bool:
        return self.mode == "tailnet"

    @property
    def funnel(self) -> bool:
        return self.mode == "public"

    @field_validator("broker_env_file", "google_client_secret_file", "tailscale_exe")
    @classmethod
    def require_absolute(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or "\x00" in str(value):
            raise ValueError("absolute_webui_path_required")
        return value

    @model_validator(mode="after")
    def exclusive_exposure(self) -> "WebUISettings":
        if self.funnel and int(self.public_url.rsplit(":", 1)[1]) not in (8443, 10000):
            raise ValueError("webui_funnel_port_must_be_8443_or_10000")
        return self


class RegisteredBuildTool(BaseModel):
    """Trusted local executable receipt, not a remotely selectable command."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    executable: Path
    sha256: Digest
    version: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9 ._+()-]+$")

    @field_validator("executable")
    @classmethod
    def absolute_executable(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or value.suffix.casefold() != ".exe":
            raise ValueError("absolute_registered_executable_required")
        return value


REMOVED_SOURCE_GATES = {
    "private_acl_confirmed": "the broker no longer asks for this declaration; keep the "
    "cache_root directory private to this user",
}


class SourceRepository(BaseModel):
    """Locally trusted public GitHub origin and private snapshot root."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    github_repository: str = Field(pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+$")
    cache_root: Path
    submodule_repositories: list[str] = Field(default_factory=list, max_length=16)
    # Migration aid: earlier cache locations whose old-style digests stay trusted.
    # Location only; never part of any digest. Keep this field last.
    previous_cache_roots: list[Path] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def removed_attestation(cls, data: Any) -> Any:
        return drop_removed_gates(data, REMOVED_SOURCE_GATES)

    @field_validator("previous_cache_roots")
    @classmethod
    def previous_roots(cls, value: list[Path]) -> list[Path]:
        if len(set(value)) != len(value) or any(
            not item.is_absolute() or ".." in item.parts for item in value
        ):
            raise ValueError("absolute_previous_source_cache_roots_required")
        return value

    @model_validator(mode="after")
    def previous_roots_differ(self) -> "SourceRepository":
        if self.cache_root in self.previous_cache_roots:
            raise ValueError("previous_source_cache_root_must_differ")
        return self

    @field_validator("submodule_repositories")
    @classmethod
    def submodule_allowlist(cls, value: list[str]) -> list[str]:
        if len({item.casefold() for item in value}) != len(value) or any(
            re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", item) is None
            or any(part in (".", "..") for part in item.split("/"))
            for item in value
        ):
            raise ValueError("registered_submodule_repository_required")
        return value

    @field_validator("github_repository")
    @classmethod
    def repository_name(cls, value: str) -> str:
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("registered_github_repository_required")
        return value

    @field_validator("cache_root")
    @classmethod
    def cache_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("absolute_source_cache_root_required")
        return value


class BuildProject(BaseModel):
    """Trusted local registration, never supplied as an MCP pathname."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    project_dir: Path
    trusted_profile: Path
    build_root: Path | None = None
    build_min_free_bytes: int = Field(default=512 * 1024 * 1024, strict=True, ge=0, le=1 << 40)
    profile_execution_policy: Literal["inherit", "RemoteSigned"] = "inherit"
    node_tool: RegisteredBuildTool | None = None
    git_tool: RegisteredBuildTool | None = None
    source_repository: SourceRepository | None = None
    expected_idf: Literal["6.0.1"] = "6.0.1"
    target: Literal["esp32s3"] = "esp32s3"

    @field_validator("build_root")
    @classmethod
    def absolute_build_root(cls, value: Path | None) -> Path | None:
        if value is not None and (not value.is_absolute() or ".." in value.parts):
            raise ValueError("absolute_registered_output_root_required")
        return value

    @field_validator("project_dir", "trusted_profile")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts:
            raise ValueError("absolute_registered_path_required")
        return value


class Settings(BaseModel):
    # The root model decides whether error text echoes the input: it must not carry
    # registered private paths or serials.
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    backend: Literal["mock", "serial"] = "mock"
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)
    state_dir: Path = Path(".state")
    mock_stage_seconds: float = Field(default=0.15, ge=0.001, le=10)
    max_queue: int = Field(default=32, ge=1, le=256)
    max_work_sessions: int = Field(default=128, ge=1, le=1024)
    max_waiters: int = Field(default=32, ge=1, le=256)
    log_capacity: int = Field(default=256, ge=4, le=10000)
    flash_devices: dict[str, FlashDevice] = Field(default_factory=dict, max_length=8)
    build_projects: dict[str, BuildProject] = Field(default_factory=dict, max_length=16)
    serial: SerialSettings | None = None
    tunnel: TunnelMetadata | None = None
    gateway: GatewaySettings | None = None
    webui: WebUISettings | None = None

    @model_validator(mode="before")
    @classmethod
    def ignored_legacy_mock_switch(cls, data: Any) -> Any:
        # `allow_mock_execution` has granted nothing for a long time (explicit execution
        # is always required), so either legacy value is read and ignored.
        if isinstance(data, dict) and "allow_mock_execution" in data:
            if type(data["allow_mock_execution"]) is not bool:
                raise ValueError("removed_setting_allow_mock_execution_must_be_boolean")
            return {k: v for k, v in data.items() if k != "allow_mock_execution"}
        return data

    @model_validator(mode="after")
    def guard_real_mode(self) -> "Settings":
        if self.backend == "serial" and self.serial is None:
            raise ValueError("serial mode needs a registered device selector")
        if self.backend == "mock" and self.serial is not None:
            raise ValueError("serial configuration is not accepted in mock mode")
        return self


def load_settings(path: Path | None) -> Settings:
    if path is None:
        return Settings()
    with path.open("rb") as handle:
        return Settings.model_validate(tomllib.load(handle))
