"""Registered flash tools updated in place are reported, not refused."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cardputer_mcp.firmware_build import hash_file
from cardputer_mcp.flash_driver import esptool_fingerprint, registered_flash_tool_drift


class FlashToolDriftTests(unittest.TestCase):
    def test_changed_bytes_at_the_registered_paths_are_named(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "python.exe"
            python.write_bytes(b"python")
            esptool = root / "esptool"
            esptool.mkdir()
            (esptool / "esptool.py").write_bytes(b"v5")
            registration = SimpleNamespace(
                python_tool=SimpleNamespace(executable=python, sha256=hash_file(python)),
                esptool_directory=esptool,
                esptool_sha256=esptool_fingerprint(esptool),
            )
            self.assertEqual(registered_flash_tool_drift(registration), [])
            (esptool / "esptool.py").write_bytes(b"v5.1")
            self.assertEqual(registered_flash_tool_drift(registration), ["esptool"])
            python.write_bytes(b"python 3.13.17")
            self.assertEqual(registered_flash_tool_drift(registration), ["python", "esptool"])

    def test_unregistered_hashes_report_no_drift(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / "python.exe"
            python.write_bytes(b"python")
            registration = SimpleNamespace(
                python_tool=SimpleNamespace(executable=python, sha256=None),
                esptool_directory=root,
                esptool_sha256=None,
            )
            self.assertEqual(registered_flash_tool_drift(registration), [])

    def test_tool_hashes_and_versions_are_optional_in_the_config(self):
        from cardputer_mcp.config import RegisteredBuildTool
        from cardputer_mcp.flash_models import FlashPython

        base = Path(tempfile.gettempdir()).resolve()
        node = RegisteredBuildTool(executable=base / "node.exe")
        self.assertIsNone(node.sha256)
        self.assertIsNone(node.version)
        python = FlashPython(executable=base / "python.exe")
        self.assertIsNone(python.sha256)
        self.assertIsNone(python.version)


if __name__ == "__main__":
    unittest.main()
