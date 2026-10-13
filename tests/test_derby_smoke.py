"""Offline fixed-plan smoke fixtures, never real USB."""

import threading
import unittest
from collections import deque

from cardputer_mcp.derby_smoke import derby_smoke
from cardputer_mcp.serial_backend import Reader, SerialFailure
from cardputer_mcp.smoke_port import SmokePort


class Wire:
    def __init__(self, *, identity=b"local.derby", phases=True):
        self.now = 0.0
        self.row = 13
        self.identity, self.phases = identity, phases
        self.queue = deque()
        self.writes = []
        self.closed = False
        self.running = False
        self.demo = 0
        self.is_open = False
        self.dtr = self.rts = False
        self.port = None

    def read(self, size=1):
        self.now += 0.1
        if self.queue:
            return self.queue.popleft()
        if self.running and self.phases:
            self.demo += 1
            if self.demo == 2:
                self.now += 15
                return b"DERBY DEMO START 5000,0,0,100\n"
            if 3 <= self.demo <= 6:
                phase = (b"gate", b"race", b"photo", b"res")[self.demo - 3]
                return b"DERBY SCENE " + phase + b" race=1000001 seed=AB123456 plans=2 reg=3\n"
            if self.demo == 7:
                return b"DERBY DEMO END 5000,0,0,100\n"
        return b""

    def write(self, data):
        self.writes.append(data)
        if data in (b"\x1b", b"q"):
            self.running = False
            reply = b"HOME_READY\n"
        elif data == b"a":
            reply = b"CATEGORY 0\n"
        elif data in (b"u", b"d"):
            self.row = max(0, self.row - 1) if data == b"u" else self.row + 1
            reply = f"APP {self.row}\n".encode()
        elif data == b"e":
            self.running = True
            reply = b"APP_ID " + self.identity + b"\nDERBY READY tones=true\n"
        elif data == b"s":
            reply = b"CAPTURE_BEGIN 240 135\n"
            reply += b"".join(f"PIX {y} ".encode() + b"0000" * 240 + b"\n" for y in range(135))
            reply += b"CAPTURE_END\n"
        else:
            raise AssertionError
        self.queue.extend(reply[at:at + 1024] for at in range(0, len(reply), 1024))
        return len(data)

    def open(self):
        self.is_open = True

    def reset_input_buffer(self):
        raise AssertionError("no buffer discard")

    def close(self):
        self.closed = True
        self.is_open = False


class DerbyTests(unittest.TestCase):
    def run_wire(self, wire):
        reader = Reader(wire, lambda _: None, lambda: wire.now, 180)
        return derby_smoke(reader)

    def test_exact_navigation_and_one_frame_with_explicit_exit(self):
        wire = Wire()
        image, code = self.run_wire(wire)
        self.assertTrue(image.png)
        self.assertEqual(wire.writes, [b"\x1b", b"a"] + [b"u"] * 16 + [b"d"] * 12 + [b"e", b"s", b"q"])
        self.assertEqual(code, "derby_demo_cycle_observed_render_buffer_lcd_unverified")

    def test_wrong_app_never_captures_or_issues_game_inputs(self):
        wire = Wire(identity=b"local.other")
        with self.assertRaises(SerialFailure):
            self.run_wire(wire)
        self.assertNotIn(b"s", wire.writes)
        self.assertNotIn(b"q", wire.writes)

    def test_absent_phases_returns_time_image_limit_not_cycle_success(self):
        image, code = self.run_wire(Wire(phases=False))
        self.assertTrue(image.png)
        self.assertEqual(code, "derby_image_observed_demo_cycle_or_photo_phase_not_verified")

    def test_cancel_or_lease_loss_blocks_every_new_write_but_allows_close(self):
        for stop_requested in (False, True):
            wire = Wire()
            stop = threading.Event()
            live = [True]
            guarded = SmokePort(wire, lambda: live[0], stop)
            guarded.write(b"a")
            if stop_requested:
                stop.set()
            else:
                live[0] = False
            for key in (b"e", b"s", b"q"):
                with self.assertRaises(SerialFailure):
                    guarded.write(key)
            guarded.close()
            self.assertEqual(wire.writes, [b"a"])
            self.assertTrue(wire.closed)


if __name__ == "__main__":
    unittest.main()
