"""Synthetic bytes only: no SDK, device, or serial commands."""

import base64
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace

from cardputer_mcp.derby_fixed import LEGACY_PLAN, FixedCollector, parse_trial
from cardputer_mcp.derby_reports import private_job
from cardputer_mcp.device_faults import fault_category
from cardputer_mcp.job_outcome import review_summary

FIXTURE = (Path(__file__).parent / "fixtures" / "derby_fixed_v1.log").read_bytes()
FAULTS = (
    b"E (99) js: InternalError: interrupted\n",
    b"E (99) app: APP ERROR\n",
    b"E (99) app: APP_ERROR\n",
    b"E (99) js: TypeError: private text must not escape\n",
    b"stop hook threw: failed\n",
    b"stop hook did not finish\n",
    b"watchdog timeout\n",
    b"FRAME RUNAWAY\n",
)
EXIT = b"DERBY_DETAIL_EXIT v=1 session=1 clean=1\n"
STOP = b"I (10) app: APP_STOPPED\n"
HOME = b"I (11) shell: HOME_READY\n"


class DeviceFaultTests(unittest.TestCase):
    def test_legacy_evidence_is_readable_without_upgrading_old_success(self):
        for raw in (FIXTURE, FIXTURE + b"InternalError: interrupted\n"):
            plan = {
                "test": "derby_fixed_workload",
                "job_id": "legacy",
                "backend": "serial",
                "protocol": "pocketjs-derby-fixed-v1",
                "fixed_workload": LEGACY_PLAN,
                "broker_epoch": "a" * 32,
                "device_fingerprint": "b" * 64,
                "expected_firmware_sha256": "c" * 64,
            }
            digest = hashlib.sha256(
                json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            job = {"id": "legacy", "plan_hash": digest, "state": "succeeded"}
            receipt = {
                "complete": True,
                "bytes": len(raw),
                "duration_ms": 1000,
                "sha256": hashlib.sha256(raw).hexdigest(),
                **dict.fromkeys(
                    (
                        "gaps",
                        "overflow",
                        "truncated",
                        "partial_tail",
                        "disconnect",
                        "reset",
                        "refused",
                    ),
                    False,
                ),
            }
            record = {
                "raw": raw,
                "receipt": receipt,
                "sha256": receipt["sha256"],
                "valid": True,
                "parsed": parse_trial(FIXTURE, require_owner_exit=False),
            }
            with sqlite3.connect(":memory:") as db:
                db.execute("CREATE TABLE execution_requests(job_id,plan_hash,principal_id)")
                db.execute(
                    "INSERT INTO execution_requests VALUES(?,?,?)", ("legacy", digest, "owner")
                )
                store = SimpleNamespace(db=db, fixed_trial=lambda *_, value=record: value)
                result = private_job(store, job, plan, "owner")
                self.assertEqual(result["state"], "succeeded")
                self.assertTrue(result["fixed_trial"]["historical_valid"])
                self.assertFalse(result["fixed_trial"]["valid"])
                self.assertFalse(result["fixed_trial"]["terminal_success_verified"])
                self.assertEqual(base64.b64decode(result["fixed_trial"]["raw_base64"]), raw)
                self.assertNotIn("fixed_trial", private_job(store, job, plan, "other"))

    def test_owner_exit_contract_and_explicit_legacy_read(self):
        self.assertTrue(parse_trial(FIXTURE + EXIT + STOP + HOME)["work_verified"])
        self.assertTrue(parse_trial(FIXTURE, require_owner_exit=False)["work_verified"])
        for tail in (
            STOP + HOME,
            EXIT + EXIT + STOP + HOME,
            EXIT.replace(b"session=1", b"session=2") + STOP + HOME,
            EXIT.replace(b"clean=1", b"clean=0") + STOP + HOME,
            STOP + EXIT + HOME,
            EXIT + HOME + STOP,
            EXIT + STOP,
            EXIT + HOME,
            EXIT + STOP.replace(b"app:", b"unrelated:") + HOME,
            EXIT + STOP + HOME.replace(b"shell:", b"unrelated:"),
            EXIT + b"InternalError: interrupted\n" + STOP + HOME,
        ):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                parse_trial(FIXTURE + tail)

    def test_collector_requires_matching_exit_before_stop(self):
        for exit_line in (EXIT, b"", EXIT + EXIT, EXIT.replace(b"session=1", b"session=2")):
            collector = FixedCollector(0.0)
            raw = (
                b"APP_ID local.derby\nDERBY READY tones=true\n" + FIXTURE + exit_line + STOP + HOME
            )
            collector.feed(raw)
            for line in raw.splitlines():
                normalized = line.removeprefix(b"I (10) app: ").removeprefix(b"I (11) shell: ")
                collector.observe_line(normalized)
            self.assertEqual(collector.snapshot(1.0, complete=True).valid, exit_line == EXIT)

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

    def test_fault_after_end_cannot_qualify_complete_rows(self):
        for fault in FAULTS:
            with self.subTest(fault=fault):
                for tail in (fault, b"APP_STOPPED\n" + fault, b"APP_STOPPED\nHOME_READY\n" + fault):
                    with self.assertRaisesRegex(ValueError, "fixed_device_fault"):
                        parse_trial(FIXTURE + tail)

    def test_fault_latches_in_collector_across_every_chunk_boundary(self):
        for fault in FAULTS:
            for split in range(len(fault) + 1):
                collector = FixedCollector(0.0)
                collector.feed(FIXTURE)
                collector.feed(fault[:split])
                collector.feed(fault[split:])
                for line in (
                    b"APP_ID local.derby",
                    b"DERBY READY tones=true",
                    FIXTURE.splitlines()[-1],
                    EXIT.rstrip(b"\n"),
                    b"APP_STOPPED",
                    b"HOME_READY",
                ):
                    collector.observe_line(line)
                trial = collector.snapshot(1.0, complete=True)
                with self.subTest(fault=fault, split=split):
                    self.assertFalse(trial.valid)
                    self.assertFalse(trial.receipt["complete"])
                    self.assertIsNone(trial.parsed)
                    self.assertIn(fault, trial.raw)

    def test_normal_stop_is_not_an_exception_exemption(self):
        self.assertIsNone(fault_category(b"APP_STOPPED\nHOME_READY\n"))
        self.assertIsNone(fault_category(b"DERBY GO LOADSTALL"))
        self.assertEqual(
            fault_category(b"DERBY GO LOADSTALL", fixed_workload=True), "fixed_workload_stall"
        )
        self.assertEqual(fault_category(b"InternalError: interrupted"), "vm_exception")
        self.assertEqual(fault_category(b"watchdog timeout"), "watchdog_or_runaway")
        self.assertEqual(fault_category(b"\x1b[31mAPP ERROR\x1b[0m"), "device_fault")


if __name__ == "__main__":
    unittest.main()
