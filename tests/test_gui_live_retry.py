"""Deterministic read-recovery policy and bounded diagnostics, without devices."""

import unittest

from cardputer_mcp.gui_live_retry import LiveRetry, diagnostic


class LiveRetryTests(unittest.TestCase):
    def test_backoff_jitter_cap_and_stable_connection_reset(self):
        retry = LiveRetry()
        fault = diagnostic("read_timeout", 1, 35000, retryable=True)
        for index, delay in enumerate([1, 2, 4, 8, 16, 30, 30]):
            retry.failed(fault, 100, jitter=1)
            self.assertEqual(retry.deadline, 100 + delay)
            self.assertEqual(retry.attempts, index + 1)
            self.assertFalse(retry.due(100 + delay - 0.001))
            self.assertTrue(retry.due(100 + delay))
            retry.starting()
            self.assertFalse(retry.due(200))
        retry.received(200)
        retry.received(259)
        self.assertEqual(retry.attempts, 7)
        retry.received(260)
        self.assertEqual(retry.attempts, 0)
        retry.failed(fault, 300, jitter=0.8)
        self.assertEqual(retry.deadline, 300.8)

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

    def test_invalid_data_or_delivery_gap_get_one_fresh_resync_not_loop(self):
        for reason in ["invalid_response", "delivery_gap"]:
            retry = LiveRetry()
            retry.failed(diagnostic(reason, 1, 4), 0, jitter=1)
            self.assertEqual(retry.deadline, 1)
            retry.received(2)
            retry.failed(diagnostic(reason, 2, 4), 3, jitter=1)
            self.assertTrue(retry.stopped)
            self.assertIsNone(retry.deadline)
            retry.manual()
            retry.failed(diagnostic(reason, 3, 4), 4, jitter=1)
            self.assertEqual(retry.deadline, 5)

    def test_diagnostics_drop_unknown_body_codes_and_bound_numbers(self):
        details = diagnostic("broker_rejected", 7, 10**12, status=503, code="PRIVATE token URL")
        self.assertEqual(details["elapsed_ms"], 86400000)
        self.assertEqual(details["http_status"], 503)
        self.assertIsNone(details["code"])
        self.assertNotIn("PRIVATE", repr(details))
        self.assertNotIn("PRIVATE", repr(diagnostic("PRIVATE reason", 1, 0)))
        self.assertIn("+00:00", details["utc"])
        self.assertEqual(
            diagnostic("broker_rejected", 7, -1, code="unavailable")["code"], "unavailable"
        )


if __name__ == "__main__":
    unittest.main()
