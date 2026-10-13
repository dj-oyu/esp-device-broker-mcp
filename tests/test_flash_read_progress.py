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
        self.progress = ReadProgress(8388608, 900, clock=lambda: self.now)

    def report(self, done):
        return self.progress.accept(json.dumps({"bytes": done, "total": 8388608}))

    def test_advancing_read_can_cross_old_300_second_limit(self):
        for step in range(1, 62):
            self.now = step * 5
            self.report(step * 1024)
            self.progress.check()
        self.assertEqual(self.now, 305)

    def test_progress_never_extends_hard_deadline(self):
        self.now = 899
        self.report(1024)
        self.now = 900
        with self.assertRaisesRegex(FlashFailure, "read_deadline"):
            self.progress.check()

    def test_idle_and_duplicate_progress_fail_closed(self):
        self.report(1024)
        self.now = 30
        with self.assertRaisesRegex(FlashFailure, "read_stalled"):
            self.progress.check()
        self.report(1024)
        with self.assertRaisesRegex(FlashFailure, "progress_invalid"):
            self.progress.check()

    def test_bad_fields_bool_overrun_and_wrong_total_are_rejected(self):
        for value in (
            {"bytes": True, "total": 8388608},
            {"bytes": 8388609, "total": 8388608},
            {"bytes": 1024, "total": 4096},
            {"bytes": 1024, "total": 8388608, "data": "private"},
        ):
            progress = ReadProgress(8388608, 900)
            self.assertIsNone(progress.accept(json.dumps(value)))
            with self.assertRaisesRegex(FlashFailure, "progress_invalid"):
                progress.check()

    def test_public_progress_contains_only_counts_and_elapsed_time(self):
        self.now = 5
        self.assertEqual(self.report(1024), "info: ROM read 1024/8388608 bytes; elapsed 5 s")


if __name__ == "__main__":
    unittest.main()
