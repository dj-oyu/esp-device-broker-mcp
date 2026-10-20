"""Accessible, non-mutating emphasis and durable, state-independent ordering."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cardputer_mcp.gui_job_status import (
    LABELS,
    configure_job_status_tags,
    job_presentation,
    job_status_label,
    job_status_tag,
)
from cardputer_mcp.timeline_order import initialize_timeline_order


class TimelineOrderTests(unittest.TestCase):
    def database(self, path=""):
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE plans(hash TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT,reviewed INTEGER DEFAULT 0,
                              created TEXT,finished TEXT);
        """)
        return db

    def prepare(self, db, job, digest):
        db.execute("INSERT INTO plans VALUES(?,?)", (digest, json.dumps({"job_id": job})))

    def ordered(self, db):
        return db.execute(
            "SELECT job_id,created FROM timeline_order ORDER BY sequence DESC"
        ).fetchall()

    def test_prepare_execute_finish_and_review_preserve_registration(self):
        db = self.database()
        initialize_timeline_order(db)
        self.prepare(db, "old", "a")
        self.prepare(db, "new", "b")
        before = self.ordered(db)
        db.execute("INSERT INTO jobs VALUES('old','running',0,'2099-01-01',NULL)")
        db.execute("UPDATE jobs SET state='needs_review',finished='2099-02-01' WHERE id='old'")
        db.execute("UPDATE jobs SET reviewed=1 WHERE id='old'")
        self.assertEqual(self.ordered(db), before)
        self.assertEqual([row[0] for row in before], ["new", "old"])
        # Clock changes and equal timestamps never reorder registration sequence.
        db.execute("INSERT INTO jobs VALUES('third','failed',0,'1900-01-01',NULL)")
        self.assertEqual([row[0] for row in self.ordered(db)], ["third", "new", "old"])
        self.prepare(db, "old", "replacement")
        self.assertEqual(len(self.ordered(db)), 3)
        db.close()

    def test_legacy_backfill_is_idempotent_preserves_history_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "history.db")
            db = self.database(path)
            self.prepare(db, "undated", "a")
            self.prepare(db, "executed", "b")
            db.execute("INSERT INTO jobs VALUES('executed','needs_review',1,'2020-01-01',NULL)")
            db.execute("INSERT INTO jobs VALUES('newer','failed',0,'2021-01-01',NULL)")
            original = db.execute("SELECT * FROM jobs").fetchall()
            plans = db.execute("SELECT * FROM plans").fetchall()
            initialize_timeline_order(db)
            expected = self.ordered(db)
            self.assertEqual(
                expected, [("newer", "2021-01-01"), ("executed", "2020-01-01"), ("undated", None)]
            )
            self.assertEqual(db.execute("SELECT * FROM jobs").fetchall(), original)
            self.assertEqual(db.execute("SELECT * FROM plans").fetchall(), plans)
            db.close()
            db = sqlite3.connect(path)
            changes = db.total_changes
            initialize_timeline_order(db)
            self.assertEqual(db.total_changes, changes)
            self.assertEqual(self.ordered(db), expected)
            self.prepare(db, "future", "c")
            self.assertEqual(self.ordered(db)[0][0], "future")
            db.close()

    def test_invalid_plan_payloads_cannot_break_registration(self):
        db = self.database()
        for digest, payload in enumerate(
            [
                "{",
                "[]",
                '{"job_id":null}',
                '{"job_id":1}',
                '{"job_id":""}',
                '{"job_id":"' + "x" * 20000 + '"}',
            ]
        ):
            db.execute("INSERT INTO plans VALUES(?,?)", (str(digest), payload))
        initialize_timeline_order(db)
        self.assertEqual(self.ordered(db), [])
        db.execute("INSERT INTO plans VALUES('later','{')")
        self.assertEqual(self.ordered(db), [])
        db.close()


class JobStatusTests(unittest.TestCase):
    def test_shapes_labels_and_review_badges_are_distinct(self):
        self.assertEqual(
            set(LABELS) | {"needs_review"},
            {
                "prepared",
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "expired",
                "unknown",
                "approval_required",
                "awaiting_approval",
                "approved",
                "ready",
                "needs_review",
            },
        )
        self.assertIn("未実行", job_status_label("prepared"))
        self.assertEqual(job_status_label("failed"), "[X] 失敗")
        self.assertEqual(job_status_label("needs_review"), "[?] 結果不明")
        self.assertEqual(job_status_tag("needs_review"), "job_unknown")
        self.assertEqual(job_status_tag("succeeded"), "")
        self.assertEqual(job_status_label("foreign"), "[?] 結果不明")

    def test_background_and_accessibility_fallback_preserve_selected_theme(self):
        tree = Mock()
        with patch("cardputer_mcp.gui_job_status.high_contrast_enabled", return_value=False):
            configure_job_status_tags(tree)
        self.assertEqual(tree.tag_configure.call_args_list[0].kwargs["background"], "#FDE8E7")
        self.assertEqual(tree.tag_configure.call_args_list[1].kwargs["background"], "#FFF0CC")
        with patch("cardputer_mcp.gui_job_status.high_contrast_enabled", return_value=True):
            configure_job_status_tags(tree)
        self.assertEqual(
            tree.tag_configure.call_args.kwargs,
            {"background": "SystemWindow", "foreground": "SystemWindowText"},
        )
        tree.configure.assert_not_called()


class JobPresentationTests(unittest.TestCase):
    def test_confirmed_fault_is_failed_independently_of_acknowledgement(self):
        for reviewed in (False, True):
            row = job_presentation("needs_review", result="serial_fault_marker", reviewed=reviewed)
            self.assertEqual(row.lifecycle, "terminal_record")
            self.assertEqual(row.outcome, "failed")
            self.assertEqual(row.label, "[X] 失敗")
            self.assertEqual(row.tag, "job_failed")
            self.assertEqual(row.review_label, "[確認済み]" if reviewed else "[未確認]")

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
                self.assertEqual(row.label, "[?] 結果不明")
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
