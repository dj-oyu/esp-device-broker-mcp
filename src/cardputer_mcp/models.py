from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]
Capability = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Origin(StrEnum):
    LOCAL = "local_cli"
    REMOTE = "remote_mcp"


class TestName(StrEnum):
    DIAGNOSTICS = "diagnostics"
    WORKFLOW = "firmware_workflow"
    OBSERVE = "passive_observe"
    FIRMWARE = "firmware_observe"
    RESTART = "restart_and_observe"
    BUILD = "firmware_build"
    BACKUP = "firmware_backup"
    FLASH = "firmware_flash"
    RESTORE = "firmware_restore"
    HELLO = "hello"
    CAPTURE = "capture_home"


FLASH_TESTS = frozenset({TestName.BACKUP, TestName.FLASH, TestName.RESTORE})


class AcquireRequest(StrictModel):
    owner: Origin
    lease_seconds: int = Field(default=30, ge=1, le=300)


class LeaseRequest(StrictModel):
    session: Capability


class RenewRequest(LeaseRequest):
    lease_seconds: int = Field(default=30, ge=1, le=300)


class PrepareRequest(LeaseRequest):
    job_id: Identifier
    test: TestName = TestName.DIAGNOSTICS
    project_id: Identifier | None = None
    device_id: Identifier | None = None
    build_job_id: Identifier | None = None
    backup_job_id: Identifier | None = None
    lkg_receipt: Digest | None = None

    @model_validator(mode="after")
    def flash_references(self) -> "PrepareRequest":
        if self.test not in FLASH_TESTS:
            if any(
                value is not None
                for value in (
                    self.device_id,
                    self.build_job_id,
                    self.backup_job_id,
                    self.lkg_receipt,
                )
            ):
                raise ValueError("flash_references_only_for_flash_operations")
        elif self.device_id is None or self.project_id is not None:
            raise ValueError("registered_flash_device_required")
        elif self.test == TestName.BACKUP:
            if (
                self.build_job_id is not None
                or self.backup_job_id is not None
                or self.lkg_receipt is not None
            ):
                raise ValueError("backup_has_no_build_or_backup_input")
        elif self.backup_job_id is not None and self.lkg_receipt is not None:
            raise ValueError("choose_one_recovery_source")
        elif self.test == TestName.FLASH and self.build_job_id is None:
            raise ValueError("successful_build_job_required")
        elif self.test == TestName.RESTORE and self.build_job_id is not None:
            raise ValueError("restore_uses_backup_only")
        return self


class ExecuteRequest(LeaseRequest):
    plan_hash: Digest
    wait_for_approval: bool = Field(default=False, strict=True)


class CancelRequest(LeaseRequest):
    job_id: Identifier


class JobRequest(StrictModel):
    job_id: Identifier


class LogCursor(StrictModel):
    epoch: str = Field(pattern=r"^[a-f0-9]{32}$")
    sequence: int = Field(ge=0)


class LogRequest(StrictModel):
    cursor: LogCursor | None = None
    limit: int = Field(default=40, ge=1, le=100)


class CreateWorkSessionRequest(StrictModel):
    owner: Origin
    ttl_seconds: int = Field(default=3600, ge=1, le=86400)


class ResumeWorkSessionRequest(StrictModel):
    resume_token: Capability


class RequestDeviceRequest(ResumeWorkSessionRequest):
    lease_seconds: int = Field(default=30, ge=1, le=300)
    wait_seconds: int = Field(default=300, ge=1, le=3600)


class CancelDeviceRequest(ResumeWorkSessionRequest):
    request_id: Identifier


class CloseWorkSessionRequest(ResumeWorkSessionRequest):
    pass


BuildLogLevel = Literal["error", "warning", "info", "debug", "unknown"]


def _default_build_log_levels() -> list[BuildLogLevel]:
    return ["error", "warning"]


class BuildLogRequest(StrictModel):
    job_id: Identifier
    cursor: LogCursor | None = None
    levels: list[BuildLogLevel] = Field(default_factory=_default_build_log_levels, max_length=5)
    context: int = Field(default=2, ge=0, le=10)
    limit: int = Field(default=40, ge=1, le=100)
