import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cardputer_mcp.private_workspace_plan import WorkspacePlan
from cardputer_mcp.private_workspace_windows import materialize_windows_workspace


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

