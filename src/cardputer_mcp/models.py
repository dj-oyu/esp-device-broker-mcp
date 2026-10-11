from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

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
    HELLO = "hello"
    CAPTURE = "capture_home"


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
