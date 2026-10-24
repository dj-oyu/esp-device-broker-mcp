"""Accessible, non-mutating emphasis and durable, state-independent ordering."""

import unittest

from cardputer_mcp.gui_job_status import (
    job_presentation,
)


class JobPresentationTests(unittest.TestCase):
    def test_confirmed_fault_is_failed_independently_of_acknowledgement(self):
        for reviewed in (False, True):
            row = job_presentation("needs_review", result="serial_fault_marker", reviewed=reviewed)
            self.assertEqual(row.lifecycle, "terminal_record")
            self.assertEqual(row.outcome, "failed")
            self.assertEqual(row.label, "❌ Failed")
            self.assertEqual(row.tag, "job_failed")
            self.assertEqual(row.review_label, "Reviewed" if reviewed else "Not reviewed")

    def test_uncertain_completion_or_write_never_becomes_failed_or_success_by_review(self):
        for code in (
            None,
            "broker_restart_no_replay",
            "serial_close_unconfirmed",
            "flash_write_unconfirmed",
            "backend_error_no_retry",
            "PRIVATE",
        ):
            for reviewed in (False, True):
                row = job_presentation("needs_review", result=code, reviewed=reviewed)
                self.assertEqual(row.outcome, "unknown")
                self.assertEqual(row.label, "❔ Unknown")
                self.assertEqual(row.tag, "job_unknown")
                self.assertNotIn("PRIVATE", repr(row))

    def test_lifecycle_and_backend_terminal_outcomes_remain_distinct(self):
        self.assertEqual(job_presentation("prepared").lifecycle, "prepared")
        self.assertEqual(job_presentation("prepared").outcome, "not_started")
        self.assertEqual(job_presentation("running").outcome, "in_progress")
        for state in ("succeeded", "failed", "cancelled"):
            row = job_presentation(state)
            self.assertEqual(row.outcome, state)
            self.assertEqual(row.acknowledgement, "not_applicable")
            self.assertEqual(row.review_label, "-")


if __name__ == "__main__":
    unittest.main()
