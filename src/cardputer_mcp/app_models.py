"""Bounded application plans and commands; no source, path or raw input fields."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Action = Literal["accept", "back", "up", "right", "down", "left"]
AppId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]{1,31}$")]
U32 = Annotated[int, Field(strict=True, ge=0, le=0xFFFFFFFF)]
U64 = Annotated[int, Field(strict=True, ge=1, le=0xFFFFFFFFFFFFFFFF)]


class AppModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _default_actions() -> list[Action]:
    return ["accept", "back"]


class AppSessionOptions(AppModel):
    app_id: AppId
    start: Literal["fresh", "resume"] = "fresh"
    expected_boot: U64 | None = None
    expected_run: U32 | None = None
    allowed_actions: list[Action] = Field(default_factory=_default_actions, max_length=6)
    max_commands: int = Field(default=32, strict=True, ge=1, le=59)
    rate_per_second: int = Field(default=10, strict=True, ge=1, le=10)
    duration_ms: int = Field(default=30000, strict=True, ge=1000, le=90000)
    log_bytes: int = Field(default=262144, strict=True, ge=1024, le=262144)
    end: Literal["stop"] = "stop"

    @model_validator(mode="after")
    def identities(self) -> "AppSessionOptions":
        if len(set(self.allowed_actions)) != len(self.allowed_actions):
            raise ValueError("duplicate_action")
        if self.start == "resume" and (self.expected_boot is None or not self.expected_run):
            raise ValueError("resume_requires_boot_and_run")
        if self.start == "fresh" and self.expected_run not in (None, 0):
            raise ValueError("fresh_requires_run_zero")
        return self


class AppCommandRequest(AppModel):
    session: str = Field(pattern=r"^[a-f0-9]{64}$")
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    boot_id: U64
    run_id: U32
    control_session: U32
    command_id: U32 = 0
    seq: U32 = 0
    operation: Literal["action.tap", "status", "suspend", "resume", "stop"]
    action: Action | None = None

    @model_validator(mode="after")
    def typed_command(self) -> "AppCommandRequest":
        if (self.operation == "action.tap") != (self.action is not None):
            raise ValueError("action_only_for_tap")
        if self.operation != "status" and (not self.command_id or not self.seq):
            raise ValueError("command_identity_required")
        if self.command_id >= 0xFFFFFFFE:
            raise ValueError("reserved_cleanup_command")
        return self


class AppEventCursor(AppModel):
    epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    control: int = Field(default=0, ge=0, strict=True)
    log: int = Field(default=0, ge=0, strict=True)


class AppEventsRequest(AppModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    cursor: AppEventCursor | None = None
    limit: int = Field(default=40, ge=1, le=100, strict=True)
