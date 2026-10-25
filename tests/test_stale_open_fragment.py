"""Invented bytes only: a stale FIFO head from a previous session (issue #111).

After a port open the device may first send up to one 64-byte USB Serial/JTAG
TX FIFO of the line it was logging when the host closed the port, without its
newline. Only that bounded head of the first line may be dropped, and only at
an embedded standard ESP log prefix; the record itself must stay exact.
"""

import json
from collections import deque

import pytest

from cardputer_mcp.serial_backend import (
    STALE_OPEN_FRAGMENT_MAX,
    Reader,
    SerialFailure,
    stale_open_fragment_length,
)
from cardputer_mcp.serial_diagnostics import sanitize_receive_summary

# The two first lines reported in issue #111 (jobs A1c and A1b).
FRAGMENT_A1C = b"I (147553) motion: ACC -625 9039 3522 mm/s2 GYR 0 0 0 mrad/s (of"
FRAGMENT_A1B = b"I (109256) motion: ACC -625 9039 3522 mm/s2 GYR 0 0 0 mrad/s (of"
HOME_A1C = b"I (229949) shell: HOME_READY"
HOME_A1B = b"I (134634) shell: HOME_READY"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FragmentPort:
    """Never a real serial connection; replies with invented bytes after a write."""

    def __init__(self, clock, reply, *, split, preloaded=()):
        self.clock, self.reply, self.split = clock, reply, split
        self.chunks = deque(preloaded)
        self.writes = []

    def read(self, size=1):
        self.clock.now += 0.01
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        return chunk[:size]

    def write(self, key):
        self.writes.append(key)
        self.chunks.extend(
            self.reply[index : index + self.split]
            for index in range(0, len(self.reply), self.split)
        )
        return len(key)


def make_reader(reply, *, split=4096, preloaded=()):
    clock = Clock()
    port = FragmentPort(clock, reply, split=split, preloaded=preloaded)
    events = []
    return Reader(port, events.append, clock, clock() + 90), port, events


def home(reader):
    return reader.command(b"\x1b", rb"HOME_READY", step="capture_home", timeout=2)


def summary(reader):
    return reader.receive_diagnostics.snapshot(reader.diagnostic_pending_bytes)


def test_issue_fragments_fill_exactly_one_fifo():
    assert len(FRAGMENT_A1C) == len(FRAGMENT_A1B) == STALE_OPEN_FRAGMENT_MAX == 64


@pytest.mark.parametrize("fragment,record", [(FRAGMENT_A1C, HOME_A1C), (FRAGMENT_A1B, HOME_A1B)])
@pytest.mark.parametrize("split", [1, 7, 63, 4096])
@pytest.mark.parametrize("end", [b"\n", b"\r\n"])
def test_issue_glued_home_ready_after_open_is_accepted(fragment, record, split, end):
    reader, port, events = make_reader(fragment + record + end, split=split)
    assert home(reader) == [b"HOME_READY"]
    assert port.writes == [b"\x1b"]
    assert events == ["serial_wait_capture_home", "serial_home_ready"]
    value = summary(reader)
    assert value["stale_open_fragment_bytes"] == 64
    assert value["after_command"]["home_ready_exact_lines"] == 1
    assert value["home_marker_shape"]["normalization"] == "exact"
    assert value["home_marker_shape"]["standard_prefix_count"] == 1
    sanitized = sanitize_receive_summary(value)
    assert sanitized is not None and sanitized["stale_open_fragment_bytes"] == 64
    # Diagnostics keep only the length, never the device text of the fragment.
    rendered = json.dumps(sanitized)
    for material in (b"motion", b"mrad", b"147553", b"109256"):
        assert material.decode() not in rendered


def test_colored_embedded_record_is_split_at_its_ansi_prefix():
    reply = FRAGMENT_A1C[:40] + b"\x1b[0;32m" + HOME_A1C + b"\x1b[0m\r\n"
    reader, _, _ = make_reader(reply)
    assert home(reader) == [b"HOME_READY"]
    assert summary(reader)["stale_open_fragment_bytes"] == 40


def test_split_applies_to_other_markers_too():
    reply = FRAGMENT_A1C + b"I (229950) shell: CATEGORY 0\n"
    reader, _, events = make_reader(reply)
    assert reader.command(b"a", rb"CATEGORY \d+", timeout=2) == [b"CATEGORY 0"]
    assert events[-1] == "serial_category_apps"


def test_fragment_that_arrived_before_the_command_is_still_dropped():
    reader, _, _ = make_reader(HOME_A1C + b"\n", preloaded=[FRAGMENT_A1C])
    assert home(reader) == [b"HOME_READY"]
    value = summary(reader)
    assert value["stale_open_fragment_bytes"] == 64
    assert value["home_marker_shape"]["cross_command_partial"] is True


def test_record_bytes_pending_before_the_command_still_void_the_line():
    # Part of the HOME record itself preceded the write: never this command's ack.
    preloaded = [FRAGMENT_A1C + HOME_A1C[:13]]
    reader, _, _ = make_reader(HOME_A1C[13:] + b"\n", preloaded=preloaded)
    with pytest.raises(SerialFailure, match="serial_marker_timeout"):
        home(reader)


def test_glued_home_ready_on_a_later_line_is_rejected():
    reply = b"I (229940) shell: idle\n" + FRAGMENT_A1C + HOME_A1C + b"\n"
    reader, _, events = make_reader(reply)
    with pytest.raises(SerialFailure, match="serial_marker_timeout") as error:
        home(reader)
    assert "serial_home_ready" not in events
    summary_value = error.value.diagnostic["receive_summary"]
    assert "stale_open_fragment_bytes" not in summary_value
    assert summary_value["after_command"]["home_ready_exact_lines"] == 0


def test_glued_home_ready_after_an_earlier_session_line_is_rejected():
    reader, _, _ = make_reader(
        FRAGMENT_A1C + HOME_A1C + b"\n", preloaded=[b"I (229930) shell: idle\n"]
    )
    with pytest.raises(SerialFailure, match="serial_marker_timeout"):
        home(reader)


@pytest.mark.parametrize("length", [65, 66, 200])
def test_fragment_longer_than_one_fifo_is_rejected(length):
    fragment = (b"ACC -625 9039 3522 mm/s2 GYR 0 0 0 mrad/s (of" * 5)[:length]
    assert stale_open_fragment_length(fragment + HOME_A1C) == 0
    reader, _, events = make_reader(fragment + HOME_A1C + b"\n")
    with pytest.raises(SerialFailure, match="serial_marker_timeout") as error:
        home(reader)
    assert "serial_home_ready" not in events
    assert "stale_open_fragment_bytes" not in error.value.diagnostic["receive_summary"]


def test_split_uses_the_first_embedded_prefix_so_a_longer_head_stays_rejected():
    # 65 bytes before HOME: the first embedded prefix (offset 1) starts a record
    # that is not exact, so nothing is accepted.
    reader, _, events = make_reader(b"x" + FRAGMENT_A1C + HOME_A1C + b"\n")
    with pytest.raises(SerialFailure, match="serial_marker_timeout"):
        home(reader)
    assert "serial_home_ready" not in events


@pytest.mark.parametrize(
    "line",
    [
        b"(of HOME_READY",
        b"(ofshell: HOME_READY",
        b"(of(229949) shell: HOME_READY",
        b"(ofX (229949) shell: HOME_READY",
        b"(ofI (229949) shell: HOME_READY trailing",
        b"(ofI (229949) shell: I (229950) shell: HOME_READY",
    ],
)
def test_no_standard_prefix_or_inexact_record_is_rejected(line):
    reader, _, events = make_reader(line + b"\n")
    with pytest.raises(SerialFailure, match="serial_marker_timeout"):
        home(reader)
    assert "serial_home_ready" not in events


def test_unsplit_first_line_keeps_existing_behavior():
    assert stale_open_fragment_length(HOME_A1C) == 0
    reader, _, _ = make_reader(HOME_A1C + b"\n")
    assert home(reader) == [b"HOME_READY"]
    assert "stale_open_fragment_bytes" not in summary(reader)


@pytest.mark.parametrize("bad", [0, 65, -1, True, "64", None])
def test_sanitizer_rejects_invalid_fragment_lengths(bad):
    reader, _, _ = make_reader(FRAGMENT_A1C + HOME_A1C + b"\n")
    home(reader)
    value = summary(reader)
    value["stale_open_fragment_bytes"] = bad
    assert sanitize_receive_summary(value) is None
