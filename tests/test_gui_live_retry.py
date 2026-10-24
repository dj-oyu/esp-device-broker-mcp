"""Deterministic read-recovery policy and bounded diagnostics, without devices."""

import unittest

from cardputer_mcp.gui_live_retry import LiveRetry, diagnostic


class LiveRetryTests(unittest.TestCase):
    def test_authentication_and_permanent_rejections_stop(self):
        for reason in [
            "authentication_failed",
            "http_rejected",
            "broker_rejected",
            "unexpected_exception",
        ]:
            retry = LiveRetry()
            retry.failed(diagnostic(reason, 1, 4), 0, jitter=1)
            self.assertTrue(retry.stopped)
            self.assertIsNone(retry.deadline)
            self.assertFalse(retry.due(10000))


if __name__ == "__main__":
    unittest.main()
