"""Mock ownership/approval/security/cancel/readback/boot contracts, no USB."""

import hashlib
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_lifecycle import (
    FlashFailure,
    FlashLifecycle,
    OperationPlan,
    SecurityReceipt,
)
from cardputer_mcp.flash_manifest import FlashBundle, FlashSegment


class FakeDriver:
    def __init__(self):
        self.data = bytearray(b"x" * 8192)
        self.active = False
        self.uncertain = False
        self.closed = False
        self.writes = 0
        self.callback = lambda phase: None
        self.security = SecurityReceipt("a" * 64, len(self.data), "esp32s3", False, False, False)
        self.summary = {
            "boot_observed": True,
            "idf_version": "6.0.1",
            "target": "esp32s3",
            "hello_verified": True,
            "elf_sha256_prefix": "c" * 9,
        }

    def probe(self):
        self.active = True
        self.callback("probe")
        return self.security

    def read(self, offset, size):
        self.callback("read")
        return bytes(self.data[offset : offset + size])

    def write(self, segments):
        self.callback("write")
        self.writes += 1
        for offset, data in segments:
            self.data[offset : offset + len(data)] = data

    def boot(self):
        self.callback("boot")
        return self.summary

    def stop(self):
        pass

    def close(self):
        self.callback("close")
        if self.uncertain:
            raise RuntimeError("synthetic shutdown uncertainty")
        self.active = False
        self.closed = True


class FlashLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.engine = FlashLifecycle(self.driver)
        self.backup = bytes(self.driver.data)
        self.image = b"new-image" * 20
        segment = FlashSegment(
            4096, "app.bin", len(self.image), hashlib.sha256(self.image).hexdigest()
        )
        self.bundle = FlashBundle("b" * 64, "c" * 64, (segment,), "dio", "8MB", "80m")
        self.plan = OperationPlan(
            "flash",
            "flash-job",
            "mock-principal",
            "d" * 32,
            "e" * 32,
            "f" * 64,
            len(self.backup),
            "a" * 64,
            hashlib.sha256(self.backup).hexdigest(),
            "b" * 64,
            "c" * 64,
        )
        self.live = True
        self.stages = []

    def run_plan(self, plan=None, approved=None, closed=True, backup=None):
        plan = plan or self.plan
        return self.engine.run(
            plan,
            approved or plan.digest,
            live=lambda: self.live,
            reader_closed=lambda: closed,
            stage=self.stages.append,
            bundle=self.bundle,
            images=(self.image,),
            backup=self.backup if backup is None else backup,
        )

    def test_exact_approval_and_reader_closed_are_required(self):
        for approved, closed in (("0" * 64, True), (self.plan.digest, False)):
            with self.assertRaisesRegex(FlashFailure, "approval_or_handoff"):
                self.run_plan(approved=approved, closed=closed)
        self.assertEqual(self.driver.writes, 0)
        self.assertFalse(self.driver.active)

    def test_security_flags_unknown_or_enabled_block_backup_and_flash(self):
        for field in ("secure_boot", "flash_encryption", "secure_download"):
            for value in (True, None, 0):
                self.driver = FakeDriver()
                self.engine = FlashLifecycle(self.driver)
                self.driver.security = replace(self.driver.security, **{field: value})
                with self.assertRaisesRegex(FlashFailure, "security_or_identity"):
                    self.run_plan()
                self.assertEqual(self.driver.writes, 0)
                self.assertTrue(self.driver.closed)

    def test_device_swap_blocks_write(self):
        self.driver.security = replace(self.driver.security, device_sha256="0" * 64)
        with self.assertRaisesRegex(FlashFailure, "security_or_identity"):
            self.run_plan()
        self.assertEqual(self.driver.writes, 0)

    def test_success_readback_and_boot_with_confirmed_cleanup(self):
        _, _, result = self.run_plan()
        self.assertTrue(result["readback_verified"])
        self.assertTrue(result["boot_observed"])
        self.assertEqual(self.driver.writes, 1)
        self.assertFalse(self.engine.active)
        self.assertTrue(self.driver.closed)
        self.assertFalse(self.engine.request_stop(self.plan.job_id))

    def test_backup_receipt_binds_exact_device_capacity_and_hash(self):
        plan = replace(
            self.plan,
            operation="backup",
            expected_device_sha256=None,
            backup_sha256=None,
            manifest_sha256=None,
        )
        receipt, data, result = self.run_plan(plan)
        self.assertEqual(receipt.device_sha256, "a" * 64)
        self.assertEqual(receipt.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(receipt.device_fingerprint, plan.device_fingerprint)
        self.assertFalse(result["hardware_write"])
        self.assertEqual(self.driver.writes, 0)

    def test_missing_or_tampered_backup_never_writes(self):
        with self.assertRaisesRegex(FlashFailure, "verified_device_backup"):
            self.run_plan(backup=b"changed")
        self.assertEqual(self.driver.writes, 0)

    def test_changed_erase_sector_never_writes(self):
        self.driver.data[-1] ^= 1
        with self.assertRaisesRegex(FlashFailure, "backup_stale"):
            self.run_plan()
        self.assertEqual(self.driver.writes, 0)

    def test_cancel_at_each_stage_requires_review_and_closes(self):
        for phase in ("probe", "read", "write", "boot"):
            self.setUp()
            self.driver.callback = lambda actual, expected=phase: (
                self.engine.request_stop(self.plan.job_id) if actual == expected else None
            )
            with (
                self.subTest(phase=phase),
                self.assertRaisesRegex(FlashFailure, "cancelled_needs_review"),
            ):
                self.run_plan()
            self.assertTrue(self.driver.closed)
            self.assertFalse(self.engine.active)
            self.assertLessEqual(self.driver.writes, 1)

    def test_revoked_authority_blocks_write(self):
        self.driver.callback = lambda phase: (
            setattr(self, "live", False) if phase == "read" else None
        )
        with self.assertRaisesRegex(FlashFailure, "authority_changed"):
            self.run_plan()
        self.assertEqual(self.driver.writes, 0)

    def test_timeout_requires_review_without_native_error_details(self):
        def timeout(phase):
            if phase == "write":
                raise TimeoutError("synthetic private command")

        self.driver.callback = timeout
        with self.assertRaisesRegex(FlashFailure, "^flash_timeout_needs_review$"):
            self.run_plan()
        self.assertTrue(self.driver.closed)

    def test_uncertain_shutdown_retains_busy_and_blocks_reuse(self):
        self.driver.uncertain = True
        # Already uncertain ownership rejects before another operation.
        with self.assertRaisesRegex(FlashFailure, "busy_or_uncertain"):
            self.run_plan()
        self.driver.uncertain = False
        self.driver.callback = lambda phase: (
            setattr(self.driver, "uncertain", True) if phase == "close" else None
        )
        with self.assertRaisesRegex(FlashFailure, "shutdown_uncertain"):
            self.run_plan()
        self.assertTrue(self.engine.active)
        self.assertTrue(self.engine.uncertain)
        with self.assertRaisesRegex(FlashFailure, "busy_or_uncertain"):
            self.run_plan()

    def test_bad_boot_requires_review_without_automatic_restore(self):
        self.driver.summary["boot_observed"] = False
        with self.assertRaisesRegex(FlashFailure, "boot_unverified"):
            self.run_plan()
        self.assertEqual(self.driver.writes, 1)
        self.assertTrue(self.driver.closed)

    def test_elf_prefix_is_separate_from_bin_hash_and_required_for_flash(self):
        self.assertNotEqual(self.bundle.segments[0].sha256, self.plan.expected_elf_sha256)
        with self.assertRaisesRegex(FlashFailure, "expected_elf_receipt"):
            self.run_plan(replace(self.plan, expected_elf_sha256=None))
        self.assertEqual(self.driver.writes, 0)
        self.driver.summary["elf_sha256_prefix"] = "d" * 9
        with self.assertRaisesRegex(FlashFailure, "boot_unverified"):
            self.run_plan()
        self.assertEqual(self.driver.writes, 1)

    def test_hello_contract_failure_never_claims_boot_success(self):
        self.driver.summary["hello_verified"] = False
        with self.assertRaisesRegex(FlashFailure, "boot_unverified"):
            self.run_plan()
        self.assertEqual(self.driver.writes, 1)

    def test_restore_requires_its_own_exact_plan_approval(self):
        plan = replace(self.plan, operation="restore", manifest_sha256=None)
        with self.assertRaisesRegex(FlashFailure, "approval_or_handoff"):
            self.run_plan(plan, approved=self.plan.digest)
        self.assertEqual(self.driver.writes, 0)
        _, _, summary = self.run_plan(plan)
        self.assertTrue(summary["readback_verified"])
        self.assertEqual(bytes(self.driver.data), self.backup)


if __name__ == "__main__":
    unittest.main()
