"""Fixed reason summaries never infer OOM from a generic failure or expose raw."""

import unittest

from cardputer_mcp.device_faults import fault_category, fault_detail
from cardputer_mcp.job_failure_reason import REASON_LABELS, failure_reason


class FailureReasonTests(unittest.TestCase):
    def test_explicit_oom_hint_is_display_only_and_content_free(self):
        raw = (
            b"PRIVATE APP_LOAD play compile out of memory\n"
            b"OOM first_req=1 used=2\nSTART_FAILED ESP_FAIL\n"
        )
        category = fault_category(raw)
        self.assertEqual(category, "device_fault")
        detail = fault_detail(raw)
        self.assertEqual(detail, "app_load_out_of_memory")
        reason = failure_reason("needs_review", "serial_fault_marker", category, detail)
        self.assertIn("メモリ不足", REASON_LABELS[reason])
        self.assertNotIn("PRIVATE", repr((category, detail, reason)))
        self.assertEqual(fault_detail(b"OOM first_req=1 used=2"), "out_of_memory_observed")
        self.assertEqual(
            fault_detail(b"APP_LOAD play compile out of memory\n"), "out_of_memory_observed"
        )
        self.assertIsNone(fault_detail(b"APP ERROR\n"))
        self.assertIsNone(fault_detail(b"ROOM BOOM\n"))

    def test_old_generic_failure_is_not_relabelled_as_oom(self):
        self.assertEqual(
            failure_reason("needs_review", "serial_fault_marker", None, None), "device_fault"
        )
        self.assertEqual(
            failure_reason("needs_review", "serial_fault_marker", "vm_exception", None),
            "vm_exception",
        )
        self.assertIsNone(failure_reason("succeeded", "serial_fault_marker", "vm_exception", None))
        self.assertIsNone(failure_reason("failed", "PRIVATE", "PRIVATE", "PRIVATE"))
        self.assertEqual(
            failure_reason("needs_review", "serial_fault_marker", "device_fault", "PRIVATE"),
            "device_fault",
        )
        self.assertEqual(
            failure_reason(
                "needs_review", "serial_close_unconfirmed", None, "app_load_out_of_memory"
            ),
            "serial_close_unconfirmed",
        )


if __name__ == "__main__":
    unittest.main()
