"""Virtual-time progress deadlines; no native process or USB operation."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.flash_read_progress import ReadProgress


class ReadProgressTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.progress = ReadProgress(8388608, 900, idle_seconds=30, clock=lambda: self.now)

    def report(self, done):
        return self.progress.accept(json.dumps({"bytes": done, "total": 8388608}))

    def test_progress_never_extends_hard_deadline(self):
        self.now = 899
        self.report(1024)
        self.now = 900
        with self.assertRaisesRegex(FlashFailure, "read_deadline"):
            self.progress.check()

    def test_public_progress_contains_only_counts_and_elapsed_time(self):
        self.now = 5
        self.assertEqual(self.report(1024), "info: ROM read 1024/8388608 bytes; elapsed 5 s")
        self.assertEqual(
            self.progress.snapshot(),
            {"read_bytes": 1024, "read_total": 8388608, "elapsed_seconds": 5},
        )
        self.now = 200000
        self.assertEqual(self.progress.snapshot()["elapsed_seconds"], 100000)

    def test_failure_diagnostic_rejects_raw_or_unbounded_fields(self):
        code = "flash_native_failed_needs_review"
        safe = {
            "code": code,
            "operation": "rom_read",
            "read_bytes": 1024,
            "read_total": 8388608,
            "elapsed_seconds": 5,
            "failure_origin": "native_worker",
            "exception_type": "SerialException",
            "failure_class": "io",
        }
        self.assertEqual(FlashFailure(code, safe).diagnostic, safe)
        for unsafe in (
            safe | {"raw_message": "password=private COM7"},
            safe | {"read_bytes": 8388609},
            safe | {"exception_type": ["SerialException"]},
            safe | {"failure_origin": ["native_worker"]},
        ):
            self.assertIsNone(FlashFailure(code, unsafe).diagnostic)


if __name__ == "__main__":
    unittest.main()
