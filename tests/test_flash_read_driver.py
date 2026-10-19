"""Native-driver protocol using a fake owned process and virtual read clock."""

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.build_process import ProcessDeadlineExpired
from cardputer_mcp.flash_driver import NativeFlashDriver
from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.flash_read_progress import ReadProgress


class ReadDriverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.now = 0.0
        self.live = True
        self.logs = []
        registration = SimpleNamespace(
            flash_bytes=8388608,
            esptool_directory=root / "tools",
            python_tool=SimpleNamespace(executable=root / "python.exe", version="Python 3.13.16"),
        )
        backend = SimpleNamespace(active=False, hardware_connected=False)
        self.driver = NativeFlashDriver(
            registration, backend, "a" * 64, root / "work", live=lambda: self.live
        )
        self.driver._verified_port = lambda: "COM1"
        self.driver.progress_log = self.logs.append
        self.driver.process = SimpleNamespace(
            active=False, uncertain=False, stop_requested=threading.Event()
        )
        self.mode = "healthy"
        self.invocations = []

        def run(args, env, cwd, emit, *, timeout, monitor):
            payload = json.loads((cwd / "job.json").read_text())
            self.invocations.append((payload["action"], timeout, monitor))
            if payload["action"] in ("probe", "write"):
                emit('FLASH_RESULT={"ok":true}', "stdout")
                return 0
            if self.mode == "process_deadline":
                raise ProcessDeadlineExpired("build_interrupted_needs_review")
            monitor()
            if self.mode in {"native_failure", "unsafe_worker_failure"}:
                self.now = 43
                emit(
                    "FLASH_PROGRESS=" + json.dumps({"bytes": 978944, "total": payload["size"]}),
                    "stdout",
                )
                emit(
                    "FLASH_RESULT="
                    + json.dumps(
                        {
                            "ok": False,
                            "error": "flash_native_failed_needs_review",
                            "operation": "rom_read",
                            "exception_type": "SerialException"
                            if self.mode == "native_failure"
                            else "COM7-password=private",
                            "failure_class": "io",
                        }
                    ),
                    "stdout",
                )
                return 1
            if self.mode == "stalled":
                self.now = 60
                monitor()
            elif self.mode == "deadline":
                self.now = 4860
                emit('FLASH_PROGRESS={"bytes":1024,"total":8388608}', "stdout")
                monitor()
            elif self.mode == "revoked":
                self.live = False
                monitor()
            for index in range(1, 65):
                self.now = index * 5
                emit(
                    "FLASH_PROGRESS=" + json.dumps({"bytes": index * 131072, "total": 8388608}),
                    "stdout",
                )
                monitor()
            data = b"x" * payload["size"]
            (cwd / "read.bin").write_bytes(data)
            emit(
                "FLASH_RESULT="
                + json.dumps({"ok": True, "sha256": hashlib.sha256(data).hexdigest()}),
                "stdout",
            )
            return 0

        self.driver.process.run = run
        self.progress = patch(
            "cardputer_mcp.flash_driver.ReadProgress",
            lambda size, seconds: ReadProgress(size, seconds, clock=lambda: self.now),
        )
        self.progress.start()
        self.addCleanup(self.progress.stop)

    def test_progressing_full_read_exceeds_300_seconds_without_changing_write_deadline(self):
        self.assertEqual(len(self.driver.read(0, 8388608)), 8388608)
        self.assertEqual(self.now, 320)
        self.assertEqual(self.invocations[0][1], 4861)
        self.assertIn("native exit 0", "\n".join(self.logs))
        self.driver.write(((4096, b"synthetic"),))
        self.assertEqual(self.invocations[-1][1], 300)
        self.assertIsNone(self.invocations[-1][2])
        self.driver._invoke("probe")
        self.assertEqual(self.invocations[-1][1], 300)
        self.assertIsNone(self.invocations[-1][2])

    def test_stall_deadline_and_revocation_never_return_backup_bytes(self):
        for mode, code in (
            ("stalled", "read_stalled"),
            ("deadline", "read_deadline"),
            ("process_deadline", "read_deadline"),
            ("revoked", "authority_changed"),
        ):
            self.mode = mode
            self.now = 0
            self.live = True
            with self.assertRaisesRegex(FlashFailure, code):
                self.driver.read(0, 8388608)
            self.assertEqual(list(self.driver.work_root.glob("**/read.bin")), [])
        text = "\n".join(self.logs)
        self.assertNotIn("COM1", text)
        self.assertNotIn(str(self.driver.work_root), text)

    def test_native_read_failure_retains_only_bounded_safe_diagnostic(self):
        self.mode = "native_failure"
        with self.assertRaisesRegex(FlashFailure, "flash_native_failed_needs_review") as caught:
            self.driver.read(0, 2287648)
        self.assertEqual(
            caught.exception.diagnostic,
            {
                "code": "flash_native_failed_needs_review",
                "operation": "rom_read",
                "read_bytes": 978944,
                "read_total": 2287648,
                "elapsed_seconds": 43,
                "failure_origin": "native_worker",
                "exception_type": "SerialException",
                "failure_class": "io",
            },
        )
        self.assertEqual(list(self.driver.work_root.glob("flash-op-*")), [])
        self.assertNotIn("COM7", "\n".join(self.logs))

    def test_untrusted_worker_failure_fields_are_not_retained(self):
        self.mode = "unsafe_worker_failure"
        with self.assertRaisesRegex(FlashFailure, "flash_native_failed_needs_review") as caught:
            self.driver.read(0, 2287648)
        diagnostic = caught.exception.diagnostic
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic["failure_origin"], "driver_or_process")
        self.assertNotIn("exception_type", diagnostic)
        self.assertNotIn("COM7", json.dumps(diagnostic) + "\n".join(self.logs))


if __name__ == "__main__":
    unittest.main()
