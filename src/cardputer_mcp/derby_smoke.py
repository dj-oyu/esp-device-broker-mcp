"""Fixed DERBY navigation and bounded observation; no caller-provided keys."""

import re
from typing import Any

from .capture import CapturedImage, CaptureError, LegacyCaptureDecoder

SMOKE = {
    "app_id": "local.derby",
    "menu_row": 12,
    "max_operation_seconds": 180,
    "max_demo_observe_seconds": 120,
    "demo_idle_seconds": 15,
    "capture": "one_s_at_photo_or_45_second_fallback",
    "commands": ["Esc", "a", "u x16", "d x12", "e", "s", "q"],
    "exit": "one_q_then_fresh_HOME_READY; close_even_if_interrupted",
}


def take_frame(reader: Any) -> CapturedImage:
    reader.capture_guard = True
    reader.capture_quiet_boundary()
    reader.capture_decoder = LegacyCaptureDecoder()
    reader.expected_marker = "CAPTURE_END"
    reader.receive_diagnostics.begin_command("capture_frame")
    try:
        if reader.port.write(b"s") != 1:
            raise reader.failure("incomplete_serial_write")
        reader.receive_diagnostics.write_completed()
        stop = min(reader.clock() + 18, reader.deadline)
        while not reader.capture_decoder.complete:
            if reader.clock() >= stop:
                raise reader.failure("serial_capture_timeout")
            reader.read_once()
        reader.capture_quiet_boundary()
        return reader.capture_decoder.finish()
    except CaptureError:
        raise reader.failure("serial_capture_invalid") from None
    finally:
        reader.capture_decoder = None
        reader.capture_guard = False


def derby_smoke(reader: Any) -> tuple[CapturedImage, str]:
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
    stop = min(start + 120, reader.deadline - 15)
    started = ended = False
    phases: list[str] = []
    image = None
    photographed = False
    sequence: list[str] = []
    race_id: bytes | None = None
    consistent = True

    def observe(line: bytes) -> None:
        nonlocal started, ended, race_id, consistent
        if line.startswith(b"APP_ID"):
            raise reader.failure("unexpected_app_launch")
        if re.fullmatch(rb"DERBY DEMO START [0-9,-]{1,80}", line):
            if started:
                consistent = False
            started = True
            reader.emit("derby_demo_start_observed")
        if started and (match := re.fullmatch(
            rb"DERBY SCENE (pad|gate|race|photo|res) race=(\d+) "
            rb"seed=[0-9A-Fa-f]+ plans=\d+ reg=\d+",
            line,
        )):
            if race_id is not None and race_id != match[2]:
                consistent = False
            race_id = match[2]
            phase = match[1].decode()
            if phase not in phases:
                phases.append(phase)
                if phase != "pad":
                    sequence.append(phase)
                reader.emit("derby_scene_" + phase + "_observed")
        if started and re.fullmatch(rb"DERBY DEMO END [0-9,-]{1,80}", line):
            ended = True
            reader.emit("derby_demo_end_observed")

    reader.phase_observer = observe
    while reader.clock() < stop:
        reader.read_once()
        if image is None and ("photo" in phases or reader.clock() - start >= 45 or ended):
            photographed = "photo" in phases
            image = take_frame(reader)
        if ended:
            break
    if image is None:
        image = take_frame(reader)
    reader.phase_observer = None
    # Exit is an explicit approved step. Revocation suppresses this write too.
    reader.command(b"q", rb"HOME_READY", timeout=12)
    complete = consistent and started and ended and sequence == ["gate", "race", "photo", "res"]
    return image, (
        "derby_demo_cycle_observed_render_buffer_lcd_unverified"
        if complete and photographed
        else "derby_image_observed_demo_cycle_or_photo_phase_not_verified"
    )
