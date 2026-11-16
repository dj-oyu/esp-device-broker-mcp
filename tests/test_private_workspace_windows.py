import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cardputer_mcp.private_workspace_plan import WorkspacePlan
from cardputer_mcp.private_workspace_windows import _Scratch, materialize_windows_workspace


@unittest.skipUnless(sys.platform == "win32", "native Windows required")
class NativeScratchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="broker-scratch-fixture-")
        self.container = Path(self.temp.name)
        self.plan = WorkspacePlan("a" * 64, (("src/a.bin", b"good"), ("src/b.bin", b"more")))

    def tearDown(self):
        self.temp.cleanup()

    def test_write_read_acl_pin_and_cleanup(self):
        with materialize_windows_workspace(self.plan, container=self.container) as root:
            self.assertEqual((root / "src/a.bin").read_bytes(), b"good")
            with self.assertRaises(PermissionError):
                root.rename(root.with_name("moved"))
            (root / "build").mkdir()
            (root / "build/result.bin").write_bytes(b"output")
        self.assertFalse(root.exists())

    def test_created_objects_owned_by_broker(self):
        import win32security as security

        native = _Scratch()
        expected = security.ConvertSidToStringSid(native.sid)
        with materialize_windows_workspace(self.plan, container=self.container) as root:
            for path in (root, root / "src", root / "src/a.bin", root / "src/b.bin"):
                sd = security.GetFileSecurity(str(path), security.OWNER_SECURITY_INFORMATION)
                self.assertEqual(
                    security.ConvertSidToStringSid(sd.GetSecurityDescriptorOwner()), expected
                )

    def test_wrong_owner_rejected_without_modifying_acl(self):
        native = _Scratch()
        wrong = native.security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            "O:S-1-5-18D:P(A;OICI;FA;;;SY)", 1
        )
        if native.security.ConvertSidToStringSid(native.sid) == "S-1-5-18":
            self.skipTest("requires non-SYSTEM fixture identity")
        with patch.object(native.security, "GetSecurityInfo", return_value=wrong):
            with self.assertRaisesRegex(ValueError, "owner_unexpected"):
                native.verify_acl(0)

    def test_child_lock_fails_cleanup_until_child_joined(self):
        child = None
        try:
            with self.assertRaisesRegex(ValueError, "cleanup_failed"):
                with materialize_windows_workspace(self.plan, container=self.container) as root:
                    script = (
                        "import sys,win32file,win32con;"
                        "h=win32file.CreateFile(sys.argv[1],win32con.GENERIC_READ,"
                        "win32con.FILE_SHARE_READ|win32con.FILE_SHARE_WRITE,None,"
                        "win32con.OPEN_EXISTING,0,None);"
                        "print('ready',flush=True);sys.stdin.read();h.Close()"
                    )
                    child = subprocess.Popen(
                        [sys.executable, "-c", script, str(root / "src/a.bin")],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    ready = queue.Queue()
                    threading.Thread(
                        target=lambda: ready.put(child.stdout.readline().strip()), daemon=True
                    ).start()
                    self.assertEqual(ready.get(timeout=10), "ready")
            self.assertTrue(root.exists())
            self.assertEqual((root / "src/a.bin").read_bytes(), b"good")
        finally:
            if child is not None:
                try:
                    child.communicate(input="", timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0)
        # TemporaryDirectory teardown cleans only this synthetic fixture after join.

    def test_exception_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, "build failure"):
            with materialize_windows_workspace(self.plan, container=self.container) as root:
                raise RuntimeError("build failure")
        self.assertFalse(root.exists())

    def test_cancel_cleanup(self):
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls > len(self.container.parts) + 2

        with self.assertRaisesRegex(ValueError, "cancelled"):
            with materialize_windows_workspace(
                self.plan, container=self.container, cancelled=cancelled
            ):
                self.fail("must not publish partial scratch")
        self.assertEqual(list(self.container.iterdir()), [])

    def test_cleanup_junction_does_not_touch_target(self):
        outside = self.container / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_bytes(b"keep")
        with materialize_windows_workspace(self.plan, container=self.container) as root:
            result = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(root / "junction"), str(outside)],
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0)
        self.assertEqual(sentinel.read_bytes(), b"keep")
        self.assertFalse(root.exists())

    def test_junction_container_rejected(self):
        real = self.container / "real"
        real.mkdir()
        link = self.container / "link"
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(real)], capture_output=True
        )
        self.assertEqual(result.returncode, 0)
        try:
            with self.assertRaises(ValueError):
                with materialize_windows_workspace(self.plan, container=link):
                    self.fail("junction was followed")
            self.assertEqual(list(real.iterdir()), [])
        finally:
            os.rmdir(link)


if __name__ == "__main__":
    unittest.main()
