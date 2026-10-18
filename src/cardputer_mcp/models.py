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
    SOURCE = "firmware_fetch_source"
    BACKUP = "firmware_backup"
    FLASH = "firmware_flash"
    RESTORE = "firmware_restore"
    HELLO = "hello"
    CAPTURE = "capture_home"
    DERBY = "derby_smoke"


FLASH_TESTS = frozenset({TestName.BACKUP, TestName.FLASH, TestName.RESTORE})


class AcquireRequest(StrictModel):
    owner: Origin
    lease_seconds: int = Field(default=30, ge=1, le=300)


class LeaseRequest(StrictModel):
    session: Capability


class RenewRequest(LeaseRequest):
    lease_seconds: int = Field(default=30, ge=1, le=300)


class PrepareRequest(StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"test": {"const": operation}}, "required": ["test"]},
                    "then": {
                        "required": ["session", "device_id", *required],
                        "not": {
                            "required": ["backup_job_id", "lkg_receipt"],
                            "properties": {
                                "backup_job_id": {"type": "string"},
                                "lkg_receipt": {"type": "string"},
                            },
                        },
                        "properties": {
                            "session": {"type": "string"},
                            "device_id": {"type": "string"},
                            "build_job_id": {"type": "string" if required else "null"},
                            "project_id": {"type": "null"},
                            "source_commit": {"type": "null"},
                            "source_receipt": {"type": "null"},
                            **(
                                {"backup_job_id": {"type": "null"}, "lkg_receipt": {"type": "null"}}
                                if operation == "firmware_backup"
                                else {}
                            ),
                        },
                    },
                }
                for operation, required in (
                    ("firmware_backup", []),
                    ("firmware_flash", ["build_job_id"]),
                    ("firmware_restore", []),
                )
            ]
        },
    )
    session: Capability | None = Field(
        default=None,
        description=(
            "Owned live device lease required for backup/flash/restore and device "
            "jobs. Omit for registered source/build."
        ),
    )
    job_id: Identifier
    test: TestName = TestName.DIAGNOSTICS
    project_id: Identifier | None = Field(
        default=None,
        description=(
            "Registered source/build project. Do not supply for backup/flash/restore; "
            "the registered device selects its project (null is allowed)."
        ),
    )
    device_id: Identifier | None = Field(
        default=None,
        description=(
            "Required registered device ID for backup/flash/restore; not a port/path. "
            "Forbidden for other tests."
        ),
    )
    build_job_id: Identifier | None = Field(
        default=None,
        description=(
            "Required successful owned firmware_build job for firmware_flash. Omit "
            "for backup/restore and other tests."
        ),
    )
    backup_job_id: Identifier | None = Field(
        default=None,
        description=(
            "Owned successful backup input for flash/restore; mutually exclusive with "
            "lkg_receipt. Omit for firmware_backup."
        ),
    )
    lkg_receipt: Digest | None = Field(
        default=None,
        description=(
            "Verified recovery receipt for flash/restore, mutually exclusive with "
            "backup_job_id. A retained verified LKG may be selected when both are "
            "omitted."
        ),
    )
    source_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    source_receipt: Digest | None = None

    @model_validator(mode="after")
    def flash_references(self) -> "PrepareRequest":
        if self.test not in (TestName.SOURCE, TestName.BUILD) and self.session is None:
            raise ValueError("device_session_required")
        if self.test == TestName.SOURCE:
            if (
                self.project_id is None
                or self.source_commit is None
                or self.source_receipt is not None
            ):
                raise ValueError("source_fetch_requires_registered_project_and_full_commit")
        elif self.source_commit is not None:
            raise ValueError("source_commit_only_for_fetch")
        if self.source_receipt is not None and (
            self.test != TestName.BUILD or self.project_id is None
        ):
            raise ValueError("source_receipt_only_for_registered_build")
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
        elif self.device_id is None:
            raise ValueError("registered_flash_device_required")
        elif self.project_id is not None:
            raise ValueError("project_id_not_allowed_for_flash_operations")
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


class ExecuteRequest(StrictModel):
    session: Capability | None = None
    plan_hash: Digest
    wait_for_approval: bool = Field(
        default=False, strict=True,
        description="Deprecated compatibility input; ignored. Explicit execute queues immediately.",
    )


class CancelRequest(StrictModel):
    session: Capability | None = None
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
