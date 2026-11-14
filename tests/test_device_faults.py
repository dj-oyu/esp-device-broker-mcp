"""Synthetic bytes only: no SDK, device, or serial commands."""

import unittest

from cardputer_mcp.device_faults import fault_category
from cardputer_mcp.job_outcome import review_summary


class DeviceFaultTests(unittest.TestCase):
    def test_review_resolution_preserves_historical_failure(self):
        pending = review_summary("needs_review", 0)
        resolved = review_summary("needs_review", 1)
        self.assertTrue(pending["historical_failure"])
        self.assertFalse(pending["uncertainty_resolved"])
        self.assertTrue(resolved["historical_failure"])
        self.assertTrue(resolved["uncertainty_resolved"])
        self.assertTrue(resolved["failure_result_preserved"])
        self.assertEqual(resolved["required_action"], "consult_current_resource_status")
        self.assertFalse(review_summary("succeeded", 0)["historical_failure"])

    def test_normal_stop_is_not_an_exception_exemption(self):
        self.assertIsNone(fault_category(b"APP_STOPPED\nHOME_READY\n"))
        self.assertIsNone(fault_category(b"GO LOADSTALL"))
        self.assertEqual(fault_category(b"InternalError: interrupted"), "vm_exception")
        self.assertEqual(fault_category(b"watchdog timeout"), "watchdog_or_runaway")
        self.assertEqual(fault_category(b"\x1b[31mAPP ERROR\x1b[0m"), "device_fault")


if __name__ == "__main__":
    unittest.main()
