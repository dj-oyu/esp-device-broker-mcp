"""Actual SerialBackend restart/HELLO paths with fake ports and virtual clock."""

import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from test_serial_restart import STARTUP, FakePort, device

from cardputer_mcp.flash_driver import NativeFlashDriver
from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.serial_backend import SerialFailure, fingerprint


class HelloPort(FakePort):
    def __init__(self, clock):
        super().__init__(clock)
        self.row = 13
        self.launches = 0

    def write(self, data):
        self.writes.append(data)
        if data == b"q":
            reply = (
                b"MEM free=500 largest=300 js=0 frames=2\r\nAPP_STOPPED\r\n"
                if self.launches
                else b""
            ) + b"HOME_READY\r\n"
        elif data == b"a":
            reply = b"CATEGORY 0\r\n"
        elif data == b"u":
            self.row = max(0, self.row - 1)
            reply = f"APP {self.row}\r\n".encode()
        elif data == b"e" and not self.launches:
            self.launches += 1
            reply = b"APP_ID local.hello\r\nHELLO_READY\r\nKASANE_FRAME_PRESENTED\r\n"
        elif data == b"e":
            reply = b"HELLO_COUNT 1\r\n"
        else:
            raise AssertionError("unexpected synthetic command")
        self.chunks.append(reply)
        return len(data)


class DriverBootTests(unittest.TestCase):
    def make_driver(self, expected="a" * 64):
        backend, ports, entries, created = device(
            chunks=(STARTUP.replace(b"v5.5-test", b"v6.0.1"),)
        )
        ports[1] = HelloPort(backend.clock)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        driver = NativeFlashDriver(
            None,
            backend,
            fingerprint(entries[0]),
            Path(temporary.name),
            expected,
            "abcdef0123456789" + "0" * 48,
            lambda: True,
        )
        # No native receipt/process/USB operation; only the synthetic SerialBackend.
        driver._verified_port = lambda: entries[0].port
        return driver, backend, ports, created

    def test_actual_restart_passively_verifies_boot_without_hello_commands(self):
        driver, _, ports, created = self.make_driver()
        summary = driver.boot()
        self.assertTrue(summary["boot_observed"])
        self.assertEqual(summary["idf_version"], "6.0.1")
        self.assertEqual(summary["elf_sha256_prefix"], "abcdef0123456789")
        self.assertEqual(len(created), 1)
        self.assertEqual(ports[0].writes, [])
        self.assertEqual(ports[1].writes, [])
        self.assertFalse(driver.active)
        self.assertFalse(driver.uncertain)
        self.assertEqual(ports[0].opens, ports[0].closes)

    def test_wrong_or_missing_boot_evidence_never_sends_hello_keys(self):
        for startup in (
            STARTUP.replace(b"v5.5-test", b"v6.0.2"),
            STARTUP.replace(b"v5.5-test", b"v6.0.1").replace(
                b"abcdef0123456789", b"1111111111111111"
            ),
            b"synthetic noise\r\n",
        ):
            driver, _, ports, _ = self.make_driver()
            ports[0].chunks = [startup]
            with self.assertRaises((FlashFailure, SerialFailure)):
                driver.boot()
            self.assertEqual(ports[1].writes, [])
            self.assertFalse(driver.active)

    def test_expiry_or_revocation_during_observe_blocks_all_hello_keys(self):
        for revoked in (False, True):
            driver, backend, ports, _ = self.make_driver()
            deadline = backend.clock() + 1
            driver.live = lambda revoked=revoked, backend=backend, deadline=deadline: (
                not revoked and backend.clock() < deadline
            )
            with self.assertRaises((FlashFailure, SerialFailure)):
                driver.boot()
            self.assertEqual(ports[1].writes, [])
            self.assertFalse(driver.active)
            if revoked:
                self.assertEqual(ports[0].opens, 0)

    def test_stop_during_passive_read_closes_without_commands(self):
        driver, _, ports, _ = self.make_driver()
        original = ports[0].read

        def stop_after_read(size):
            result = original(size)
            driver.stop()
            return result

        ports[0].read = stop_after_read
        with self.assertRaises((FlashFailure, SerialFailure)):
            driver.boot()
        self.assertEqual(ports[0].writes, [])
        self.assertEqual(ports[1].writes, [])
        self.assertFalse(driver.active)
        self.assertEqual(ports[0].opens, ports[0].closes)

    def test_stop_before_reset_controls_never_opens(self):
        driver, backend, ports, _ = self.make_driver()
        original = backend.factory

        def stop_at_factory():
            driver.stop()
            return original()

        backend.factory = stop_at_factory
        with self.assertRaises((FlashFailure, SerialFailure)):
            driver.boot()
        self.assertEqual(ports[0].opens, 0)
        self.assertFalse(any(event[1:] == ("rts", True) for event in ports[0].trace))
        self.assertFalse(driver.active)

    def test_post_open_revocation_never_starts_reset(self):
        driver, backend, ports, _ = self.make_driver()
        original = backend.inventory
        count = 0

        def revoke_after_open():
            nonlocal count
            count += 1
            if count == 3:
                driver.stop()
            return original()

        backend.inventory = revoke_after_open
        with self.assertRaises((FlashFailure, SerialFailure)):
            driver.boot()
        self.assertEqual(ports[0].opens, 1)
        self.assertFalse(any(event[1:] == ("rts", True) for event in ports[0].trace))
        self.assertEqual(ports[0].opens, ports[0].closes)
        self.assertFalse(driver.active)

    def test_cancel_during_pulse_allows_deassertion_and_close(self):
        driver, backend, ports, _ = self.make_driver()
        original = backend.pause

        def stop_during_pulse(seconds):
            original(seconds)
            driver.stop()

        backend.pause = stop_during_pulse
        with self.assertRaises((FlashFailure, SerialFailure)):
            driver.boot()
        rts = [event[2] for event in ports[0].trace if event[1] == "rts"]
        self.assertEqual(rts[-2:], [True, False])
        self.assertEqual(ports[0].opens, ports[0].closes)
        self.assertEqual(ports[1].opens, 0)
        self.assertFalse(driver.active)

    def test_stop_during_disconnect_never_reopens(self):
        driver, _, ports, _ = self.make_driver()

        def disconnect(size):
            driver.stop()
            raise OSError("synthetic disconnected")

        ports[0].read = disconnect
        with self.assertRaises((FlashFailure, SerialFailure)):
            driver.boot()
        self.assertEqual(ports[1].opens, 0)
        self.assertEqual(ports[0].opens, ports[0].closes)
        self.assertFalse(driver.active)

    def test_without_expected_elf_boot_cannot_be_claimed(self):
        driver, _, ports, _ = self.make_driver()
        driver.expected_elf_sha256 = None
        with self.assertRaisesRegex(FlashFailure, "boot_unverified"):
            driver.boot()
        self.assertEqual(ports[1].writes, [])
        self.assertFalse(driver.active)

    def test_slash_version_boot_preserves_exact_elf_and_idf_checks(self):
        for version, idf, elf, accepted in (
            (b"archive/20261021/ci/full-contra", b"v6.0.1", b"abcdef0123456789", True),
            (b"archive/20261021/ci/full-contra", b"v6.0.2", b"abcdef0123456789", False),
            (b"archive/20261021/ci/full-contra", b"v6.0.1", b"1111111111111111", False),
        ):
            driver, _, ports, _ = self.make_driver()
            ports[0].chunks = deque(
                [
                    STARTUP.replace(b"v0.0.7-test", version)
                    .replace(b"v5.5-test", idf)
                    .replace(b"abcdef0123456789", elf)
                ]
            )
            if accepted:
                self.assertTrue(driver.boot()["boot_observed"])
            else:
                with self.assertRaisesRegex(FlashFailure, "boot_unverified"):
                    driver.boot()
            self.assertEqual(driver.last_observation.firmware.app_version, version.decode())
            self.assertIsNotNone(driver.last_boot_attempt)
            self.assertEqual(ports[0].writes, [])
            self.assertEqual(ports[0].opens, ports[0].closes)

    def test_partial_transport_failure_and_failed_close_keep_primary_evidence(self):
        for close_failure in (False, True):
            driver, _, ports, _ = self.make_driver()
            partial = b"error: SYNTHETIC_BOOT_NOTE\npassword=SYNTHETIC_PRIVATE\n"
            ports[0].chunks = deque([partial, RuntimeError("SYNTHETIC_EXCEPTION_PRIVATE")])
            ports[0].close_error = close_failure
            with self.assertRaises(SerialFailure):
                driver.boot()
            report = driver.last_observation
            self.assertEqual(report.bytes_received, len(partial))
            self.assertEqual(report.complete_lines, 2)
            self.assertNotIn("SYNTHETIC_PRIVATE", str(report.payload()))
            self.assertEqual(
                driver.last_boot_attempt.reason,
                "serial_close_unconfirmed" if close_failure else "serial_read_failed",
            )
            self.assertEqual(
                driver.last_boot_attempt.primary_failure_code,
                "serial_read_failed" if close_failure else None,
            )
            self.assertIsNotNone(driver.last_boot_attempt.observation_elapsed_ms)
            self.assertEqual(ports[0].writes, [])
            self.assertEqual(driver.uncertain, close_failure)


if __name__ == "__main__":
    unittest.main()
