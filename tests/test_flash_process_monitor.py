"""Owned dummy subprocess revocation/stop; no serial or SDK operation."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.flash_lifecycle import FlashFailure


class ProcessMonitorTests(unittest.TestCase):
    def test_live_revocation_stops_the_owned_process_before_long_read_budget(self):
        process = BuildProcess()
        checks = 0

        def monitor():
            nonlocal checks
            checks += 1
            if checks >= 3:
                raise FlashFailure("flash_authority_changed_needs_review")

        with tempfile.TemporaryDirectory() as temp:
            started = time.monotonic()
            with self.assertRaisesRegex(FlashFailure, "authority_changed"):
                process.run(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    dict(os.environ),
                    Path(temp),
                    lambda line, stream: None,
                    timeout=600,
                    monitor=monitor,
                )
        self.assertLess(time.monotonic() - started, 6)
        self.assertFalse(process.active)
        self.assertFalse(process.uncertain)


if __name__ == "__main__":
    unittest.main()
