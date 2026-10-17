"""Bounded host receive timing, never raw console text or proof of pixel identity."""

from typing import Literal

from pydantic import ConfigDict, Field

from .models import StrictModel

NO_CAPTURE = "derby_photo_not_verified_no_capture"


class DerbyEvent(StrictModel):
    model_config = ConfigDict(strict=True)
    at_ms: int = Field(ge=0, le=180000)
    kind: Literal[
        "ready",
        "demo_start",
        "scene",
        "photo_observed",
        "demo_end",
        "capture_s_requested",
        "capture_s_sent",
        "capture_begin",
        "capture_end",
        "capture_skipped",
        "exit_requested",
        "home_ready",
    ]
    phase: Literal["pad", "gate", "race", "photo", "res"] | None = None
    race: str | None = Field(default=None, pattern=r"^[0-9]{1,20}$")
    seed: str | None = Field(default=None, pattern=r"^[0-9A-F]{1,16}$")
    reason: (
        Literal[
            "photo_observed",
            "photo_not_observed_before_deadline",
            "demo_ended_without_photo",
            "photo_not_current_before_s",
            "insufficient_capture_exit_budget",
            "capture_incomplete",
            "observation_complete",
            "observation_deadline",
        ]
        | None
    ) = None


class DerbyObservation(StrictModel):
    model_config = ConfigDict(strict=True)
    version: Literal[1] = 1
    time_basis: Literal["host_ms_since_serial_operation_start"] = (
        "host_ms_since_serial_operation_start"
    )
    events: tuple[DerbyEvent, ...] = Field(max_length=64)
    events_truncated: bool
    demo_cycle_observed: bool
    photo_context_correlation: Literal[
        "not_captured", "same_observed_phase_race_seed", "changed_or_incomplete"
    ]
    photo_pixels_verified: Literal[False] = False
    image_acquired: bool
    exit_home_observed: bool
    capture_reason: str = Field(
        pattern=(
            r"^(photo_observed|photo_not_observed_before_deadline|demo_ended_without_photo|"
            r"photo_not_current_before_s|insufficient_capture_exit_budget|capture_incomplete)$"
        )
    )

    def no_capture_completed(self) -> bool:
        return (
            self.exit_home_observed
            and not self.image_acquired
            and not any(e.kind == "capture_s_requested" for e in self.events)
            and self.capture_reason not in ("photo_observed", "capture_incomplete")
        )
