"""Host-owned, bounded collection of the public DERBY fixed workload protocol.

The device's quality fields are inputs to validation, never a transport receipt.
Raw bytes stay private to the explicitly requested job and its principal.
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .device_faults import fault_category

MAX_BYTES = 65536
MAX_LINE = 1024
MAX_SECONDS = 200
FIXTURE = "oval-pan8-390-510-v1"
MANIFEST = "84e0341ee1ac10c232e3008f28f8aada8823155e19229f926e633d7780a2021d"
PARSER_COMMIT = "4d60de5e82193975596bc23981d3189e2e6bf78d"
FIXTURE_DETAILS = {
    "id": FIXTURE,
    "seed": "9112A7AB",
    "race": 1000001,
    "oval": 1,
    "tier": 1,
    "camera": 8,
    "start": 390,
    "end": 510,
    "contexts": 120,
    "repetitions": 3,
    "independent": False,
    "finish": "5DC86E18",
}
LEGACY_PLAN = {
    "fixture": FIXTURE,
    "parser_commit": PARSER_COMMIT,
    "max_seconds": MAX_SECONDS,
    "max_bytes": MAX_BYTES,
    "max_line_bytes": MAX_LINE,
    "repetitions": 3,
    "retry": 0,
}
TERMINAL_CONTRACT = "end_owner_exit_stop_home_v2"
PROTOCOL = "pocketjs-derby-fixed-v2"
FIXED_PLAN = {
    **LEGACY_PLAN,
    "terminal_contract": TERMINAL_CONTRACT,
    "owner_exit_wire_commit": "9b9e9ae46c08a6597f871419b280874d5e09033c",
}
END_KEYS = frozenset("v session reps valid quality independent".split())
ROW_KEYS = frozenset(
    "v fixture manifest variant session rep seq valid quality independent seed oval tier cam "
    "start end contexts done cont capture faults refused tx_fail wall_us work_us finish".split()
) | {f"{kind}{index}" for kind, count in (("p", 3), ("c", 10), ("s", 10)) for index in range(count)}
IDENTITY = {
    "v": "1",
    "fixture": FIXTURE,
    "manifest": MANIFEST,
    "valid": "1",
    "quality": "0",
    "independent": "0",
    "seed": "9112A7AB",
    "oval": "1",
    "tier": "1",
    "cam": "8",
    "start": "390",
    "end": "510",
    "contexts": "120",
    "done": "120",
    "capture": "0",
    "faults": "0",
    "refused": "0",
    "tx_fail": "0",
    "finish": "5DC86E18",
}
KNOWN_COUNTS = {
    "baseline": (0, 120, 600, 7735, 7735, 0, 0, 0, 0, 0),
    "native": (0, 120, 600, 7735, 120, 7735, 0, 120, 0, 0),
    "batch": (0, 120, 600, 7735, 7735, 0, 0, 0, 120, 960),
}
BRIDGE_CALLS = {"baseline": 2086, "native": 2086, "batch": 1246}
FIXED_CALL_STAGES = (1, 2, 3)


def _number(value: str) -> int:
    if not 1 <= len(value) <= 20 or not value.isascii() or not value.isdigit():
        raise ValueError("invalid_fixed_counter")
    result = int(value)
    if result > 2**64 - 1:
        raise ValueError("invalid_fixed_counter")
    return result


def _tuple(value: str, width: int) -> tuple[int, ...]:
    parts = value.split(":")
    if len(parts) != width:
        raise ValueError("invalid_fixed_tuple")
    return tuple(_number(part) for part in parts)


def _row(line: str, prefix: str, keys: frozenset[str]) -> dict[str, str]:
    parts = line.split()
    if not parts or parts[0] != prefix:
        raise ValueError("invalid_fixed_line")
    pairs = [part.split("=", 1) for part in parts[1:]]
    if any(len(pair) != 2 or not pair[0] or not pair[1] for pair in pairs):
        raise ValueError("invalid_fixed_fields")
    result = dict(pairs)
    if set(result) != keys or len(result) != len(pairs):
        raise ValueError("invalid_fixed_fields")
    return result


def _parse_trial(
    raw: bytes, *, verify_work: bool, require_owner_exit: bool = True
) -> dict[str, Any]:
    """Parse a whole trial; format-only results never qualify measured work."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES or not raw.endswith(b"\n"):
        raise ValueError("fixed_transport_incomplete")
    # END qualifies measured rows, never errors during teardown or after HOME.
    if fault_category(raw) is not None:
        raise ValueError("fixed_device_fault")
    try:
        text = raw.decode("ascii")
    except UnicodeError:
        raise ValueError("fixed_transport_non_ascii") from None
    rows: list[dict[str, str]] = []
    end: dict[str, str] | None = None
    if any(len(line) > MAX_LINE for line in text.splitlines(keepends=True)):
        raise ValueError("fixed_line_limit")
    for line in text.splitlines():
        if line.startswith("DERBY_FIXED_END"):
            if end is not None:
                raise ValueError("fixed_duplicate_end")
            end = _row(line, "DERBY_FIXED_END", END_KEYS)
        elif line.startswith("DERBY_FIXED"):
            if end is not None:
                raise ValueError("fixed_record_after_end")
            rows.append(_row(line, "DERBY_FIXED", ROW_KEYS))
    if len(rows) != 3 or end is None:
        raise ValueError("fixed_whole_trial_required")
    variant, session = rows[0]["variant"], rows[0]["session"]
    if variant not in KNOWN_COUNTS:
        raise ValueError("fixed_variant_invalid")
    _number(session)
    first_calls: tuple[int, ...] | None = None
    for index, row in enumerate(rows, 1):
        if (
            any(row[key] != value for key, value in IDENTITY.items())
            or row["variant"] != variant
            or row["session"] != session
            or row["rep"] != str(index)
            or row["seq"] != str(index)
        ):
            raise ValueError("fixed_identity_or_sequence_invalid")
        wall, work = _number(row["wall_us"]), _number(row["work_us"])
        _number(row["cont"])
        if not 0 < work <= wall <= 180000000:
            raise ValueError("fixed_time_invalid")
        pace = tuple(_tuple(row[f"p{part}"], 3) for part in range(3))
        counts = tuple(_number(row[f"c{part}"]) for part in range(10))
        stages = tuple(_tuple(row[f"s{part}"], 2) for part in range(10))
        if sum(part[0] for part in pace) + work > wall:
            raise ValueError("fixed_time_invalid")
        if any(self_us > wall or (calls == 0 and self_us) for self_us, calls in stages):
            raise ValueError("fixed_stage_self_invalid")
        if sum(self_us for self_us, _ in stages) > work:
            raise ValueError("fixed_stage_self_sum_invalid")
        if verify_work:
            if counts != KNOWN_COUNTS[variant] or stages[1][1] != BRIDGE_CALLS[variant]:
                raise ValueError("fixed_known_work_invalid")
            if stages[2][1] == 0 or stages[3][1] == 0:
                raise ValueError("fixed_native_or_build_work_missing")
            calls = tuple(stages[index][1] for index in FIXED_CALL_STAGES)
            if first_calls is not None and calls != first_calls:
                raise ValueError("fixed_repetition_work_invalid")
            first_calls = calls
            if stages[0][1] != 120 + _number(row["cont"]):
                raise ValueError("fixed_guest_continuation_invalid")
            if (
                stages[4][1] < stages[3][1]
                or stages[6][1] < 120
                or stages[7][1] < 120
                or stages[8][1] != 0
            ):
                raise ValueError("fixed_renderer_work_invalid")
    if end != {
        "v": "1",
        "session": session,
        "reps": "3",
        "valid": "1",
        "quality": "0",
        "independent": "0",
    }:
        raise ValueError("fixed_end_invalid")
    if require_owner_exit:
        _verify_terminal(text, session)
    return {
        "fixture": dict(FIXTURE_DETAILS),
        "manifest_sha256": MANIFEST,
        "variant": variant,
        "session": session,
        "independent_trials": False,
        "format_valid": True,
        "work_verified": verify_work,
        "all_repetitions_valid": verify_work,
        "repetitions": rows,
        "transport_sha256": hashlib.sha256(raw).hexdigest(),
    }


def parse_trial_format(raw: bytes, *, require_owner_exit: bool = True) -> dict[str, Any]:
    """For synthetic controller data only; never a work-verification result."""
    return _parse_trial(raw, verify_work=False, require_owner_exit=require_owner_exit)


def parse_trial(raw: bytes, *, require_owner_exit: bool = True) -> dict[str, Any]:
    """Verify format, identity, time, known work and variant-specific calls."""
    return _parse_trial(raw, verify_work=True, require_owner_exit=require_owner_exit)


def parse_collected(
    raw: bytes, receipt: dict[str, Any], *, require_owner_exit: bool = True
) -> dict[str, Any]:
    """Bind a successful collector-owned receipt to the exact raw bytes."""
    flags = {"gaps", "overflow", "truncated", "partial_tail", "disconnect", "reset", "refused"}
    if (
        type(receipt) is not dict
        or set(receipt) != {"complete", "bytes", "sha256", "duration_ms", *flags}
        or receipt["complete"] is not True
        or any(receipt[key] is not False for key in flags)
        or type(receipt["bytes"]) is not int
        or type(raw) is not bytes
        or not 0 < len(raw) <= MAX_BYTES
        or receipt["bytes"] != len(raw)
        or type(receipt["duration_ms"]) is not int
        or not 0 <= receipt["duration_ms"] <= MAX_SECONDS * 1000
        or receipt["sha256"] != hashlib.sha256(raw).hexdigest()
    ):
        raise ValueError("fixed_transport_receipt_invalid")
    return parse_trial(raw, require_owner_exit=require_owner_exit)


def _verify_terminal(text: str, session: str) -> None:
    """Exact owner EXIT is necessary but does not replace STOP/HOME/close."""
    lines = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text).splitlines()
    ends = [i for i, line in enumerate(lines) if line.startswith("DERBY_FIXED_END")]
    exits = [i for i, line in enumerate(lines) if line.startswith("DERBY_DETAIL_EXIT")]
    stops = [
        i for i, line in enumerate(lines)
        if re.fullmatch(r"(?:I \([0-9]+\) app: )?APP_STOPPED", line)
    ]
    homes = [
        i for i, line in enumerate(lines)
        if re.fullmatch(r"(?:I \([0-9]+\) shell: )?HOME_READY", line)
    ]
    if (
        len(ends) != 1 or len(exits) != 1 or len(stops) != 1 or len(homes) != 1
        or not ends[0] < exits[0] < stops[0] < homes[0]
    ):
        raise ValueError("fixed_terminal_sequence_invalid")
    if _row(lines[exits[0]], "DERBY_DETAIL_EXIT", frozenset(("v", "session", "clean"))) != {
        "v": "1", "session": session, "clean": "1",
    }:
        raise ValueError("fixed_owner_exit_invalid")


@dataclass(frozen=True)
class FixedTrial:
    raw: bytes = field(repr=False)
    receipt: dict[str, Any]
    parsed: dict[str, Any] | None = None
    valid: bool = False


class FixedCollector:
    """Owned raw-byte observer; host receipt is made only after a quiet HOME."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.raw = bytearray()
        self.pending = b""
        self.phase = "await_launch"
        self.end_seen = self.exit_seen = self.stopped = self.home = False
        self.session: str | None = None
        self.overflow = self.reset = self.refused = False
        self.device_fault: str | None = None

    def feed(self, incoming: bytes) -> None:
        if type(incoming) is not bytes:
            self.refused = True
            raise ValueError("fixed_read_type_invalid")
        if len(self.raw) + len(incoming) > MAX_BYTES:
            self.raw.extend(incoming[: MAX_BYTES - len(self.raw)])
            self.overflow = True
            raise ValueError("fixed_transport_overflow")
        fault_window = bytes(self.raw[-256:]) + incoming
        self.raw.extend(incoming)
        # Latch across chunk boundaries, independently of Reader's fatal check.
        # The existing bounded raw buffer is the only retained wire copy.
        self.device_fault = self.device_fault or fault_category(fault_window)
        lines = (self.pending + incoming).split(b"\n")
        self.pending = lines[-1]
        if any(len(line) + 1 > MAX_LINE for line in lines[:-1]) or len(self.pending) >= MAX_LINE:
            self.overflow = True
            raise ValueError("fixed_line_limit")

    def observe_line(self, line: bytes) -> None:
        if line == b"APP_ID local.derby":
            if self.phase == "await_launch":
                self.phase = "await_ready"
            else:
                self.reset = True
        elif line.startswith(b"APP_ID"):
            self.reset = True
        elif line.startswith(b"DERBY READY"):
            if self.phase == "await_ready" and re.fullmatch(
                rb"DERBY READY tones=(?:true|false)", line
            ):
                self.phase = "running"
            else:
                self.reset = True
        elif line.startswith(b"DERBY_FIXED_END"):
            if self.phase == "running":
                try:
                    end = _row(line.decode("ascii"), "DERBY_FIXED_END", END_KEYS)
                    self.session = end["session"]
                except (UnicodeError, ValueError):
                    self.reset = True
                self.phase = "ended"
                self.end_seen = True
            else:
                self.reset = True
        elif line.startswith(b"DERBY_DETAIL_EXIT"):
            expected = f"DERBY_DETAIL_EXIT v=1 session={self.session} clean=1".encode()
            if self.phase == "ended" and line == expected:
                self.phase = "exited"
                self.exit_seen = True
            else:
                self.reset = True
        elif line == b"APP_STOPPED":
            if self.phase == "exited":
                self.phase = "stopped"
                self.stopped = True
            else:
                self.reset = True
        elif line == b"HOME_READY":
            if self.phase == "stopped":
                self.phase = "home"
                self.home = True
            else:
                self.reset = True
        elif re.search(rb"(?:rst:0x|ESP-ROM:)", line):
            self.reset = True

    def snapshot(
        self, now: float, *, complete: bool = False, disconnect: bool = False
    ) -> FixedTrial:
        elapsed = now - self.started
        duration = (
            min(200000, max(0, int(elapsed * 1000))) if 0 <= elapsed < float("inf") else 200000
        )
        raw = bytes(self.raw)
        ready = (
            complete
            and self.phase == "home"
            and self.end_seen
            and self.exit_seen
            and self.stopped
            and self.home
            and not self.pending
            and raw.endswith(b"\n")
            and not (self.overflow or self.reset or self.refused or disconnect or self.device_fault)
            and elapsed <= MAX_SECONDS
        )
        receipt: dict[str, Any] = {
            "complete": bool(ready),
            "bytes": len(raw),
            "duration_ms": duration,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "gaps": False if ready else True,
            "overflow": self.overflow,
            "truncated": not ready,
            "partial_tail": bool(self.pending),
            "disconnect": disconnect,
            "reset": self.reset,
            "refused": self.refused,
        }
        if not ready:
            return FixedTrial(raw, receipt)
        try:
            parsed = parse_collected(raw, receipt)
        except ValueError:
            return FixedTrial(raw, receipt)
        return FixedTrial(raw, receipt, parsed, True)


def execute_fixed(reader: Any) -> FixedTrial:
    """Use only the existing fixed Derby navigation, then read until auto-exit."""
    reader.derby_mode = True
    reader.command(b"\x1b", rb"HOME_READY", step="capture_home")
    if reader.command(b"a", rb"CATEGORY \d+")[-1] != b"CATEGORY 0":
        raise reader.failure("wrong_category")
    previous: int | None = None
    for _ in range(16):
        row = int(reader.command(b"u", rb"APP \d+")[-1].split()[1])
        if row > 16 or (previous is not None and row != max(0, previous - 1)):
            raise reader.failure("wrong_app_navigation")
        previous = row
    if previous != 0:
        raise reader.failure("wrong_app_navigation")
    for expected in range(1, 13):
        if reader.command(b"d", rb"APP \d+")[-1] != b"APP " + str(expected).encode():
            raise reader.failure("wrong_app_navigation")

    collector = FixedCollector(reader.clock())
    reader.fixed_collector = collector
    reader.wire_observer = collector.feed
    reader.phase_observer = collector.observe_line
    launch = reader.command(b"e", rb"DERBY READY tones=(?:true|false)", timeout=20)
    if [line for line in launch if line.startswith(b"APP_ID")] != [b"APP_ID local.derby"]:
        raise reader.failure("wrong_app_identity")
    if launch.index(b"APP_ID local.derby") >= launch.index(launch[-1]):
        raise reader.failure("wrong_app_identity")
    while reader.clock() < reader.deadline and not collector.home:
        reader.read_once()
    if not collector.home:
        raise reader.failure("derby_fixed_trial_invalid")
    reader.capture_quiet_boundary()
    trial = collector.snapshot(reader.clock(), complete=True)
    if not trial.valid:
        failure = reader.failure("derby_fixed_trial_invalid")
        failure.fixed_trial = trial
        raise failure
    return trial
