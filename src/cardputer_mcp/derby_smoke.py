"""Fixed DERBY navigation and bounded observation; no caller-provided keys."""

import math
import re
from collections.abc import Callable
from typing import Any

from .backend import DeviceOperationFailure
from .capture import CapturedImage, CaptureError, LegacyCaptureDecoder
from .derby_observation import NO_CAPTURE, DerbyEvent, DerbyObservation

CAPTURE_SECONDS = 18
EXIT_SECONDS = 12
MARGIN_SECONDS = 5

SMOKE = {
    "app_id": "local.derby",
    "menu_row": 12,
    "max_operation_seconds": 180,
    "max_demo_observe_seconds": 120,
    "demo_idle_seconds": 15,
    "capture": "one_s_only_at_current_observed_photo_no_fallback_v2",
    "capture_seconds": CAPTURE_SECONDS,
    "exit_seconds": EXIT_SECONDS,
    "reserved_margin_seconds": MARGIN_SECONDS,
    "observation": "bounded_host_phase_race_seed_and_capture_timing; pixels_not_attested",
    "commands": ["Esc", "a", "u x16", "d x12", "e", "s", "q"],
    "exit": "one_q_then_fresh_HOME_READY; close_even_if_interrupted",
}


def take_frame(
    reader: Any, current_photo: Callable[[], bool], mark: Callable[[str], None]
) -> CapturedImage | None:
    reader.capture_guard = True
    original_deadline = reader.deadline
    reader.deadline = min(original_deadline, reader.clock() + CAPTURE_SECONDS)
    try:
        reader.capture_quiet_boundary()
        # Draining may observe res/another race before s; never use a stale photo.
        if not current_photo():
            return None
        decoder = LegacyCaptureDecoder()
        reader.capture_decoder = decoder
        reader.expected_marker = "CAPTURE_END"
        reader.receive_diagnostics.begin_command("capture_frame")
        mark("capture_s_requested")
        try:
            written = reader.port.write(b"s")
        except DeviceOperationFailure as failure:
            # Owned-port revocation may fail directly inside write's guard.
            # Preserve its fixed failure code plus the requested/not-completed trace.
            raise reader.failure(failure.code) from None
        except Exception:
            raise reader.failure("serial_write_failed") from None
        if written != 1:
            raise reader.failure("incomplete_serial_write")
        mark("capture_s_sent")
        reader.receive_diagnostics.write_completed()
        while not decoder.complete:
            if reader.clock() >= reader.deadline:
                raise reader.failure("serial_capture_timeout")
            reader.read_once()
        reader.capture_quiet_boundary()
        return decoder.finish()
    except CaptureError:
        raise reader.failure("serial_capture_invalid") from None
    finally:
        reader.capture_decoder = None
        reader.capture_guard = False
        reader.deadline = original_deadline


def derby_smoke(reader: Any) -> tuple[CapturedImage | None, str]:
    origin = reader.clock()
    events: list[DerbyEvent] = []
    truncated = False
    started = ended = False
    sequence: list[str] = []
    identity: tuple[str, str] | None = None
    scene: tuple[str, str, str] | None = None
    selected: tuple[str, str, str] | None = None
    consistent = True
    demo_epoch = 0
    capturing = context_changed = attempted = home = False
    image: CapturedImage | None = None
    reason = "photo_not_observed_before_deadline"

    def record(kind: str, **fields: Any) -> None:
        nonlocal truncated, consistent
        elapsed = reader.clock() - origin
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("invalid_derby_clock")
        event = DerbyEvent.model_validate(
            dict(at_ms=min(180000, int(elapsed * 1000)), kind=kind, **fields)
        )
        if len(events) < 64:
            events.append(event)
        else:
            truncated = True
            consistent = False

    def complete() -> bool:
        return consistent and started and ended and sequence == ["gate", "race", "photo", "res"]

    def snapshot() -> DerbyObservation:
        return DerbyObservation(
            events=tuple(events),
            events_truncated=truncated,
            demo_cycle_observed=complete(),
            photo_context_correlation=(
                "same_observed_phase_race_seed"
                if image is not None and not context_changed and not truncated
                else "changed_or_incomplete"
                if attempted
                else "not_captured"
            ),
            image_acquired=image is not None,
            exit_home_observed=home,
            capture_reason=reason,
        )

    reader.derby_observation = snapshot
    reader.derby_mode = True
    reader.command(b"\x1b", rb"HOME_READY", step="capture_home")
    category = reader.command(b"a", rb"CATEGORY \d+")
    if category[-1] != b"CATEGORY 0":
        raise reader.failure("wrong_category")
    previous = None
    for _ in range(16):
        row = int(reader.command(b"u", rb"APP \d+")[-1].split()[1])
        if row > 16 or (previous is not None and row != max(0, previous - 1)):
            raise reader.failure("wrong_app_navigation")
        previous = row
    if previous != 0:
        raise reader.failure("wrong_app_navigation")
    for expected in range(1, 13):
        row = reader.command(b"d", rb"APP \d+")
        if row[-1] != b"APP " + str(expected).encode():
            raise reader.failure("wrong_app_navigation")
    launch = reader.command(b"e", rb"DERBY READY tones=(?:true|false)", timeout=20)
    if [line for line in launch if line.startswith(b"APP_ID")] != [b"APP_ID local.derby"]:
        raise reader.failure("wrong_app_identity")
    ready = launch[-1]
    if launch.index(b"APP_ID local.derby") >= launch.index(ready):
        raise reader.failure("wrong_app_identity")
    start = reader.clock()
    record("ready")
    stop = min(start + 120, reader.deadline - CAPTURE_SECONDS - EXIT_SECONDS - MARGIN_SECONDS)
    phases: list[str] = []

    def capture_mark(kind: str) -> None:
        fields = (
            dict(zip(("phase", "race", "seed"), scene, strict=True)) if scene is not None else {}
        )
        record(kind, reason="photo_observed", **fields)

    def observe(line: bytes) -> None:
        nonlocal started, ended, identity, consistent, scene, context_changed, demo_epoch
        if line.startswith(b"APP_ID"):
            raise reader.failure("unexpected_app_launch")
        if line in (b"APP_STOPPED", b"HOME_READY"):
            raise reader.failure("teardown_sequence_invalid")
        if capturing and line in (b"CAPTURE_BEGIN 240 135", b"CAPTURE_END"):
            capture_mark("capture_begin" if line.startswith(b"CAPTURE_BEGIN") else "capture_end")
        if re.fullmatch(rb"DERBY DEMO START [0-9,-]{1,80}", line):
            if started:
                consistent = False
            if capturing:
                context_changed = True
            demo_epoch += 1
            # START establishes a new cycle even when identifiers repeat. No
            # earlier photo/identity/order can qualify a write in this cycle.
            scene = identity = None
            phases.clear()
            sequence.clear()
            ended = False
            started = True
            record("demo_start")
            reader.emit("derby_demo_start_observed")
        if started and (
            match := re.fullmatch(
                rb"DERBY SCENE (pad|gate|race|photo|res) race=([0-9]{1,20}) "
                rb"seed=([0-9A-Fa-f]{1,16}) plans=[0-9]{1,10} reg=[0-9]{1,10}",
                line,
            )
        ):
            phase, race, seed = match[1].decode(), match[2].decode(), match[3].decode().upper()
            if identity is not None and identity != (race, seed):
                consistent = False
            identity = race, seed
            scene = phase, race, seed
            record("scene", phase=phase, race=race, seed=seed)
            if phase == "photo":
                record("photo_observed", phase=phase, race=race, seed=seed)
            if capturing and selected is not None and scene != selected:
                context_changed = True
            if phase != "pad":
                if len(sequence) < 16:
                    sequence.append(phase)
                else:
                    consistent = False
            if phase not in phases:
                phases.append(phase)
                reader.emit("derby_scene_" + phase + "_observed")
        if started and re.fullmatch(rb"DERBY DEMO END [0-9,-]{1,80}", line):
            ended = True
            if capturing:
                context_changed = True
            record("demo_end")
            reader.emit("derby_demo_end_observed")

    reader.phase_observer = observe
    while reader.clock() < stop:
        reader.read_once()
        if not attempted and scene is not None and scene[0] == "photo":
            if reader.clock() >= stop:
                reason = "insufficient_capture_exit_budget"
                break
            attempted = capturing = True
            selected = scene
            selected_epoch = demo_epoch
            reason = "capture_incomplete"

            def current_photo(
                expected: tuple[str, str, str] | None = selected,
                expected_epoch: int = selected_epoch,
            ) -> bool:
                nonlocal reason
                if not started or demo_epoch != expected_epoch or scene != expected or ended:
                    reason = "photo_not_current_before_s"
                    record("capture_skipped", reason=reason)
                    return False
                return True

            image = take_frame(reader, current_photo, capture_mark)
            capturing = False
            if image is not None:
                reason = "photo_observed"
        if ended:
            break
    if image is None and not attempted:
        if reason == "insufficient_capture_exit_budget":
            pass
        elif "photo" in phases:
            reason = "photo_not_current_before_s"
        elif ended:
            reason = "demo_ended_without_photo"
        elif stop < start:
            reason = "insufficient_capture_exit_budget"
        record("capture_skipped", reason=reason)
    reader.phase_observer = None
    # Exit is an explicitly requested step. Revocation suppresses this write too.
    record("exit_requested", reason="observation_complete" if ended else "observation_deadline")
    reader.command(b"q", rb"HOME_READY", timeout=EXIT_SECONDS)
    home = True
    record("home_ready")
    if image is None:
        return None, NO_CAPTURE
    return image, (
        "derby_demo_cycle_observed_render_buffer_lcd_unverified"
        if complete() and not context_changed
        else "derby_image_observed_demo_cycle_or_photo_phase_not_verified"
    )
