"""U2/U5 and I1/I2/I5: fake ports, no SDK, broker, storage or serial."""

import ast
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from cardputer_mcp.fixed_diagnostic import (
    DiagnosticContext,
    finish_fixed_diagnostic,
    prepare_fixed_diagnostic,
)


class Evidence:
    def __init__(self, principal="owner", job="flash"):
        self.principal, self.job = principal, job
        self.calls = []

    def resolve(self, flash_job_id, context):
        self.calls.append((flash_job_id, context))
        return dict(
            principal_id=self.principal,
            flash_job_id=self.job,
            expected_elf_sha256="e" * 64,
            device_fingerprint="d" * 64,
        )


class Device:
    def __init__(self, identity="d" * 64):
        self.identity, self.calls = identity, []

    def prepare_fixed_receipt(self, elf_sha256):
        self.calls.append(elf_sha256)
        return dict(device_fingerprint=self.identity, expected_firmware_sha256=elf_sha256)


class FixedDiagnosticApplicationTests(unittest.TestCase):
    def test_verified_subject_is_carried_to_evidence_without_creating_a_device_job(self):
        context = DiagnosticContext("owner", "remote_mcp", "epoch", "lease")
        evidence, device = Evidence(), Device()
        result = prepare_fixed_diagnostic(context, "flash", evidence, device)
        self.assertEqual(evidence.calls, [("flash", context)])
        self.assertEqual(device.calls, ["e" * 64])
        self.assertEqual(result["collector_binding"]["principal_id"], "owner")
        with self.assertRaises(FrozenInstanceError):
            context.principal_id = "peer"

    def test_foreign_owner_or_flash_never_reaches_device_preflight(self):
        for evidence in (Evidence(principal="peer"), Evidence(job="other")):
            device = Device()
            with self.assertRaisesRegex(ValueError, "collector_successful_owned_flash_required"):
                prepare_fixed_diagnostic(DiagnosticContext("owner"), "flash", evidence, device)
            self.assertEqual(device.calls, [])

    def test_empty_subject_never_reads_evidence(self):
        evidence = Evidence()
        with self.assertRaisesRegex(ValueError, "collector_successful_owned_flash_required"):
            prepare_fixed_diagnostic(DiagnosticContext(""), "flash", evidence, Device())
        self.assertEqual(evidence.calls, [])

    def test_preflight_cannot_substitute_device_identity(self):
        with self.assertRaisesRegex(ValueError, "collector_device_binding_invalid"):
            prepare_fixed_diagnostic(DiagnosticContext("owner"), "flash", Evidence(), Device("x"))

    def test_inner_boundary_has_no_outward_imports(self):
        import cardputer_mcp.fixed_diagnostic as inner

        tree = ast.parse(Path(inner.__file__).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertEqual(node.level, 0)
                self.assertIn(node.module, {"dataclasses", "typing"})
            elif isinstance(node, ast.Import):
                self.assertTrue({n.name for n in node.names} <= {"hashlib", "json"})


class Completion:
    def __init__(self):
        self.calls = []

    def finish_fixed_trial(self, *args):
        self.calls.append(args)


class FixedDiagnosticCompletionTests(unittest.TestCase):
    def test_terminal_and_private_capture_are_one_owned_publication(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                completion, capture = Completion(), object()
                finish_fixed_diagnostic(
                    DiagnosticContext("owner"),
                    "job",
                    "hash",
                    "verified",
                    capture,
                    interrupted,
                    completion,
                )
                self.assertEqual(len(completion.calls), 1)
                call = completion.calls[0]
                self.assertEqual(call[:3], ("job", "hash", "owner"))
                self.assertEqual(call[3], "needs_review" if interrupted else "succeeded")
                self.assertIs(call[-1], capture)

    def test_missing_subject_never_publishes(self):
        completion = Completion()
        with self.assertRaisesRegex(ValueError, "fixed_trial_result_invalid"):
            finish_fixed_diagnostic(
                DiagnosticContext(""), "job", "hash", "verified", object(), False, completion
            )
        self.assertEqual(completion.calls, [])
