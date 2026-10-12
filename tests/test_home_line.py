"""Offline HOME preview tests; every wire byte is synthetic, never a real port."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from cardputer_mcp.home_line import HomeLineProbe

SECRET = b"INVENTED_PRIVATE_TOKEN"
METADATA_KEYS = {"state", "remaining_seconds", "wire_bytes", "limit_bytes", "shape"}
SHAPE_KEYS = {
    "standard_prefix_count",
    "prefix_count_capped",
    "body_bytes",
    "home_token_count",
    "token_count_capped",
    "token_offset",
    "before_bytes",
    "after_bytes",
    "before_spaces",
    "before_tabs",
    "after_spaces",
    "after_tabs",
    "before_horizontal_only",
    "after_horizontal_only",
}


class Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def set(self, now: float) -> None:
        with self.lock:
            self.now = now


def active_probe() -> tuple[HomeLineProbe, Clock]:
    clock = Clock()
    probe = HomeLineProbe(clock, 220.0)
    probe.begin_window()
    return probe, clock


def assert_safe_metadata(snapshot: dict[str, Any]) -> None:
    assert set(snapshot) == METADATA_KEYS
    assert 0 <= snapshot["remaining_seconds"] <= 120
    assert 0 <= snapshot["wire_bytes"] <= 65536
    assert snapshot["limit_bytes"] == 1024
    assert SECRET.decode() not in json.dumps(snapshot)
    shape = snapshot["shape"]
    if shape is not None:
        assert set(shape) == SHAPE_KEYS
        for value in shape.values():
            assert type(value) in (int, bool)
            assert 0 <= value <= 65536
        assert shape["standard_prefix_count"] <= 4
        assert shape["home_token_count"] <= 8


def test_initial_metadata_is_fixed_and_window_is_explicit() -> None:
    clock = Clock()
    probe = HomeLineProbe(clock, 200.0)
    assert probe.metadata() == {
        "state": "capturing",
        "remaining_seconds": 100,
        "wire_bytes": 0,
        "limit_bytes": 1024,
        "shape": None,
    }
    probe.observe_line(b"HOME_READY " + SECRET)
    assert probe.metadata()["wire_bytes"] == 0
    probe.begin_window()
    probe.observe_line(b"HOME_READY")
    probe.finish()
    assert probe.preview()["escaped_line"] == "HOME_READY"


def test_only_first_match_survives_and_metadata_and_repr_hide_private_bytes() -> None:
    probe, _ = active_probe()
    first = b"HOME_READY " + SECRET
    probe.observe_line(b"unrelated secret=" + SECRET)
    probe.observe_line(first)
    probe.observe_line(b"HOME_READY SECOND_SYNTHETIC_TOKEN")
    probe.begin_window()
    probe.observe_line(b"HOME_READY THIRD_SYNTHETIC_TOKEN")
    probe.end_window()
    probe.finish()
    assert probe.preview()["escaped_line"] == r"HOME_READY\x20INVENTED_PRIVATE_TOKEN"
    assert probe.metadata()["wire_bytes"] == len(first)
    assert_safe_metadata(probe.metadata())
    assert SECRET.decode() not in repr(probe)
    assert not hasattr(probe, "__dict__")


@pytest.mark.parametrize(
    "line",
    [b"AHOME_READY", b"HOME_READY1", b"HOME_READY_EXTRA", b"xHOME_READY!", b"HOME_REA"],
)
def test_partial_tokens_and_word_substrings_are_not_candidates(line: bytes) -> None:
    probe, _ = active_probe()
    probe.observe_line(line)
    probe.finish()
    assert probe.preview()["state"] == "no_matching_line"
    assert probe.metadata()["wire_bytes"] == 0


@pytest.mark.parametrize("line", [b"[HOME_READY]", b"HOME_READY!", b"\tHOME_READY\t"])
def test_literal_ascii_token_boundaries_are_candidates(line: bytes) -> None:
    probe, _ = active_probe()
    probe.observe_line(line)
    probe.finish()
    assert probe.metadata()["state"] == "available"


@pytest.mark.parametrize(
    ("line", "finished_state"),
    [
        (b"HOME_READY " + SECRET, "available"),
        (b"HOME_READY " + b"x" * 1024, "line_too_long"),
        (b"PIX HOME_READY " + SECRET, "line_rejected"),
    ],
)
def test_selected_candidate_is_unavailable_until_job_finishes(
    line: bytes, finished_state: str
) -> None:
    probe, _ = active_probe()
    probe.observe_line(line)
    probe.end_window()
    before = probe.preview()
    assert before["state"] == "capturing"
    assert_safe_metadata(before)
    assert probe.metadata()["state"] == "capturing"
    # Hiding the outcome does not reopen selection or permit a replacement.
    probe.begin_window()
    probe.observe_line(b"HOME_READY replacement")
    assert probe.metadata()["wire_bytes"] == len(line)
    probe.finish()
    assert probe.preview()["state"] == finished_state
    assert ("escaped_line" in probe.preview()) is (finished_state == "available")


def test_end_window_and_finish_block_late_lines() -> None:
    probe, _ = active_probe()
    probe.end_window()
    probe.observe_line(b"HOME_READY " + SECRET)
    assert probe.metadata()["state"] == "capturing"
    probe.finish()
    probe.begin_window()
    probe.observe_line(b"HOME_READY " + SECRET)
    probe.finish()
    assert probe.metadata()["state"] == "no_matching_line"
    assert probe.preview() == probe.metadata()


@pytest.mark.parametrize("length", [1025, 16384, 65536, 70000])
def test_oversized_first_candidate_is_not_retained_and_consumes_selection(length: int) -> None:
    probe, _ = active_probe()
    raw = b"HOME_READY " + b"x" * (length - 11)
    probe.observe_line(raw)
    probe.observe_line(b"HOME_READY")
    probe.finish()
    result = probe.preview()
    assert result["state"] == "line_too_long"
    assert result["wire_bytes"] == min(length, 65536)
    assert probe._line == bytearray()
    assert_safe_metadata(result)
    if length <= 65536:
        assert result["shape"]["body_bytes"] == length
    else:
        assert result["shape"] is None


def test_token_after_bounded_shape_limit_still_consumes_first_oversized_line() -> None:
    probe, _ = active_probe()
    probe.observe_line(b"x" * 70000 + b" \x1b[31mHOME_READY\x1b[0m")
    probe.finish()
    assert probe.metadata()["state"] == "line_too_long"
    assert probe.metadata()["wire_bytes"] == 65536
    assert probe.metadata()["shape"] is None


def test_wire_limit_counts_cr_but_excludes_terminal_lf() -> None:
    probe, _ = active_probe()
    raw = b"HOME_READY " + b"x" * 1012 + b"\r\n"
    probe.observe_line(raw)
    probe.finish()
    assert probe.metadata()["state"] == "available"
    assert probe.metadata()["wire_bytes"] == 1024
    assert probe.preview()["escaped_line"].endswith(r"\r")
    too_long, _ = active_probe()
    too_long.observe_line(b"HOME_READY " + b"x" * 1013 + b"\r\n")
    too_long.finish()
    assert too_long.metadata()["state"] == "line_too_long"


@pytest.mark.parametrize(
    "line",
    [
        b"CAPTURE_BEGIN HOME_READY " + SECRET,
        b"CAPTURE_END HOME_READY " + SECRET,
        b"CAPTURE_UNKNOWN HOME_READY " + SECRET,
        b"PIX 0 HOME_READY " + SECRET,
        b"PIXEL HOME_READY " + SECRET,
        b"I (1) shell: PIX 0 HOME_READY " + SECRET,
        b"I (1) shell: W (2) shell: PIX 0 HOME_READY " + SECRET,
        b"\x1b[32m \tI (1) shell: \tCAPTURE_BEGIN HOME_READY " + SECRET,
    ],
)
def test_capture_families_are_rejected_without_pixels(line: bytes) -> None:
    probe, _ = active_probe()
    probe.observe_line(line)
    probe.observe_line(b"HOME_READY")
    probe.finish()
    assert probe.metadata()["state"] == "line_rejected"
    assert probe._line == bytearray()
    assert_safe_metadata(probe.preview())


def test_crlf_ansi_backslashes_controls_and_unicode_bytes_are_ascii_escaped() -> None:
    probe, _ = active_probe()
    raw = b"\x1b[32mI (7) app: HOME_READY \\ \t\x00\x7f\xff\xe2\x80\xae\r\n"
    probe.observe_line(raw)
    probe.finish()
    preview = probe.preview()
    assert preview["escaped_line"] == (
        r"\x1b[32mI\x20(7)\x20app:\x20HOME_READY\x20\\\x20"
        r"\t\x00\x7f\xff\xe2\x80\xae\r"
    )
    assert all(32 <= ord(character) <= 126 for character in preview["escaped_line"])
    assert_safe_metadata(probe.metadata())


def test_shape_preserves_padding_but_normalizes_only_one_prefix() -> None:
    probe, _ = active_probe()
    raw = b"\x1b[32mI (7) app: \t  HOME_READY \t\t \x1b[0m\r"
    probe.observe_line(raw)
    assert probe.metadata()["shape"] == {
        "standard_prefix_count": 1,
        "prefix_count_capped": False,
        "body_bytes": 17,
        "home_token_count": 1,
        "token_count_capped": False,
        "token_offset": 3,
        "before_bytes": 3,
        "after_bytes": 4,
        "before_spaces": 2,
        "before_tabs": 1,
        "after_spaces": 2,
        "after_tabs": 2,
        "before_horizontal_only": True,
        "after_horizontal_only": True,
    }
    duplicate, _ = active_probe()
    duplicate.observe_line(b"I (1) a: W (2) b: HOME_READY")
    shape = duplicate.metadata()["shape"]
    assert shape["standard_prefix_count"] == 2
    assert shape["body_bytes"] == len(b"W (2) b: HOME_READY")
    assert shape["token_offset"] == len(b"W (2) b: ")
    assert shape["before_horizontal_only"] is False


def test_shape_prefix_and_token_counts_saturate_independently() -> None:
    probe, _ = active_probe()
    probe.observe_line(b"I (1) a: " * 9 + b"HOME_READY " * 12)
    shape = probe.metadata()["shape"]
    assert shape["standard_prefix_count"] == 4
    assert shape["prefix_count_capped"] is True
    assert shape["home_token_count"] == 8
    assert shape["token_count_capped"] is True
    assert shape["after_horizontal_only"] is False
    assert_safe_metadata(probe.metadata())


def test_normalization_expressions_match_serial_backend_without_importing_it_in_module() -> None:
    from cardputer_mcp import home_line, serial_backend

    assert home_line._ANSI.pattern == serial_backend.ANSI.pattern
    assert home_line._PREFIX.pattern == serial_backend.PREFIX.pattern


def test_snapshots_are_detached_including_nested_shape() -> None:
    probe, _ = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    probe.finish()
    metadata = probe.metadata()
    preview = probe.preview()
    metadata["shape"]["body_bytes"] = 987654321
    preview["shape"]["home_token_count"] = 777
    preview["escaped_line"] = "REPLACED"
    metadata["state"] = "REPLACED"
    assert_safe_metadata(probe.metadata())
    assert probe.metadata()["shape"]["home_token_count"] == 1
    assert probe.preview()["escaped_line"] != "REPLACED"


@pytest.mark.parametrize("method", ["prune", "metadata", "preview", "begin_window", "finish"])
def test_expiry_erases_private_storage_and_never_reopens(method: str) -> None:
    probe, clock = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    storage = probe._line
    before = probe.metadata()
    clock.set(220.0)
    getattr(probe, method)()
    assert not storage
    assert probe.metadata()["state"] == "expired"
    assert probe.metadata()["remaining_seconds"] == 0
    assert probe.metadata()["wire_bytes"] == before["wire_bytes"]
    assert probe.metadata()["shape"] == before["shape"]
    clock.set(100.0)
    probe.begin_window()
    probe.observe_line(b"HOME_READY")
    assert_safe_metadata(probe.preview())
    assert probe.metadata()["state"] == "expired"


def test_clear_erases_storage_and_remains_closed() -> None:
    probe, clock = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    storage = probe._line
    probe.clear()
    assert not storage
    probe.begin_window()
    probe.observe_line(b"HOME_READY")
    probe.finish()
    clock.set(999.0)
    probe.prune()
    assert probe.metadata()["state"] == "cleared"
    assert probe.metadata()["remaining_seconds"] == 0
    assert_safe_metadata(probe.preview())


def test_deadline_is_capped_and_fractional_remaining_cannot_extend_deadline() -> None:
    clock = Clock()
    probe = HomeLineProbe(clock, 999999.0)
    assert probe.metadata()["remaining_seconds"] == 120
    clock.set(219.1)
    remainder = probe.metadata()["remaining_seconds"]
    assert type(remainder) is float
    assert remainder == pytest.approx(0.9)
    assert remainder <= 220.0 - clock()
    assert clock() + remainder <= 220.0
    clock.set(220.0)
    assert probe.metadata()["state"] == "expired"


@pytest.mark.parametrize("unsafe", [float("nan"), float("inf"), float("-inf"), True, "SECRET"])
def test_invalid_clock_values_fail_closed_without_conversion(unsafe: Any) -> None:
    probe, _ = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    probe._clock = lambda: unsafe
    assert probe.preview()["state"] == "expired"
    assert probe._line == bytearray()
    assert_safe_metadata(probe.metadata())


def test_clock_regression_and_clock_exception_fail_closed() -> None:
    probe, clock = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    clock.set(99.0)
    assert probe.preview()["state"] == "expired"
    other, _ = active_probe()
    other.observe_line(b"HOME_READY " + SECRET)

    def unsafe_clock() -> float:
        raise RuntimeError(SECRET.decode())

    other._clock = unsafe_clock
    assert other.preview()["state"] == "expired"
    assert_safe_metadata(other.preview())
    initially_unsafe = HomeLineProbe(unsafe_clock, 220.0)
    assert initially_unsafe.metadata()["state"] == "expired"


def test_deadline_crossing_during_preview_does_not_return_escaped_content() -> None:
    probe, _ = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    probe.finish()
    times = iter([219.0, 220.0])
    probe._clock = lambda: next(times)
    assert_safe_metadata(probe.preview())
    assert probe.metadata()["state"] == "expired"


def test_expiry_between_finish_and_preview_never_releases_candidate() -> None:
    probe, clock = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    clock.set(219.9)
    probe.finish()
    assert probe.metadata()["state"] == "available"
    clock.set(220.0)
    assert_safe_metadata(probe.preview())
    assert probe.metadata()["state"] == "expired"
    assert not probe._line


def test_invalid_deadline_and_hostile_objects_never_invoke_arbitrary_representations() -> None:
    class Hostile:
        def __repr__(self) -> str:
            raise RuntimeError(SECRET.decode())

        def __float__(self) -> float:
            raise RuntimeError(SECRET.decode())

    for deadline in (float("nan"), float("inf"), True, Hostile(), 10**1000, 99.0):
        probe = HomeLineProbe(Clock(), deadline)  # type: ignore[arg-type]
        assert_safe_metadata(probe.preview())
        assert probe.metadata()["state"] == "expired"
    probe, _ = active_probe()
    probe.observe_line(Hostile())  # type: ignore[arg-type]
    probe.observe_line(b"HOME_READY\nSECOND LINE")
    probe.finish()
    assert probe.metadata()["state"] == "no_matching_line"


def test_concurrent_candidates_choose_exactly_one_and_snapshots_stay_consistent() -> None:
    probe, _ = active_probe()
    barrier = threading.Barrier(8)

    def compete(index: int) -> None:
        barrier.wait()
        probe.observe_line(f"HOME_READY synthetic_{index}".encode())
        for _ in range(20):
            assert_safe_metadata(probe.metadata())
            assert probe.preview()["state"] == "capturing"
            assert_safe_metadata(probe.preview())

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(compete, range(8)))
    probe.finish()
    first = probe.preview()["escaped_line"]
    assert first in {rf"HOME_READY\x20synthetic_{index}" for index in range(8)}
    probe.observe_line(b"HOME_READY replacement")
    assert probe.preview()["escaped_line"] == first


def test_threaded_expiry_race_never_exposes_content_after_expired_snapshot() -> None:
    probe, clock = active_probe()
    probe.observe_line(b"HOME_READY " + SECRET)
    probe.finish()
    barrier = threading.Barrier(8)

    def inspect(index: int) -> None:
        barrier.wait()
        if index == 0:
            clock.set(220.0)
            probe.prune()
        for _ in range(50):
            snapshot = probe.preview()
            if snapshot["state"] == "expired":
                assert_safe_metadata(snapshot)
                probe.begin_window()
                probe.observe_line(b"HOME_READY replacement")
                assert_safe_metadata(probe.preview())

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(inspect, range(8)))
    assert probe.metadata()["state"] == "expired"
    assert not probe._line
