"""Actual SerialBackend restart/HELLO paths with fake ports and virtual clock."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_driver import NativeFlashDriver
from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.serial_backend import fingerprint
from test_serial_restart import FakePort, STARTUP, device


class HelloPort(FakePort):
    def __init__(self, clock):
        super().__init__(clock)
        self.row = 13
        self.launches = 0

    def write(self, data):
        self.writes.append(data)
        if data == b"q":
            reply = (b"MEM free=500 largest=300 js=0 frames=2\r\nAPP_STOPPED\r\n" if self.launches else b"") + b"HOME_READY\r\n"
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
        backend, ports, entries, created = device(chunks=(STARTUP.replace(b"v5.5-test", b"v6.0.1"),))
        ports[1] = HelloPort(backend.clock)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        driver = NativeFlashDriver(None, backend, fingerprint(entries[0]), Path(temporary.name), expected)
        # No native receipt/process/USB operation; only the synthetic SerialBackend.
        driver._verified_port = lambda: entries[0].port
        return driver, backend, ports, created

    def test_actual_serial_restart_and_hello_receive_complete_plan(self):
        driver, backend, ports, created = self.make_driver()
        summary = driver.boot()
        self.assertTrue(summary["boot_observed"])
        self.assertTrue(summary["hello_verified"])
        self.assertEqual(summary["idf_version"], "6.0.1")
        self.assertEqual(summary["elf_sha256_prefix"], "abcdef0123456789")
        self.assertEqual(len(created), 2)
        self.assertTrue(any(event[1:] == ("rts", True) for event in ports[0].trace))
        self.assertTrue(ports[1].writes)
        self.assertFalse(driver.active)
        self.assertFalse(driver.uncertain)
        self.assertIsNone(backend.config.expected_firmware_sha256)
        self.assertEqual(ports[0].opens, ports[0].closes)
        self.assertEqual(ports[1].opens, ports[1].closes)

    def test_without_attested_image_hash_hello_cannot_be_claimed(self):
        driver, _, ports, _ = self.make_driver(None)
        with self.assertRaisesRegex(FlashFailure, "application_identity_required"):
            driver.boot()
        self.assertEqual(ports[1].writes, [])
        self.assertFalse(driver.active)


if __name__ == "__main__":
    unittest.main()
