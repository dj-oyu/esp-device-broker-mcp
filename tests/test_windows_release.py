"""Distribution boundary tests, stdlib-only so they run before dependency setup."""

import importlib.util
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
from release_windows import MEMBERS, create_archive, release_version, verify_archive


class WindowsReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.exe = self.folder / "cardputer.exe"
        pe = bytearray(72)
        pe[:2] = b"MZ"
        struct.pack_into("<I", pe, 60, 64)
        pe[64:68] = b"PE\0\0"
        struct.pack_into("<H", pe, 68, 0x8664)
        self.exe.write_bytes(pe)
        self.version = release_version("refs/heads/test")
        self.ref = f"refs/tags/v{self.version}"
        self.commit = "a" * 40

    def archive(self):
        return create_archive(self.exe, self.folder / "dist", self.ref, self.commit)

    def rewrite(self, archive, name, data):
        with zipfile.ZipFile(archive) as handle:
            payload = {member: handle.read(member) for member in handle.namelist()}
        payload[name] = data
        with zipfile.ZipFile(archive, "w") as handle:
            for member, content in payload.items():
                handle.writestr(member, content)

    def test_matching_tag_and_branch_builds(self):
        self.assertEqual(release_version(self.ref), self.version)
        self.assertEqual(release_version("refs/pull/2/merge"), self.version)

    def test_wrong_or_injected_tag_rejected(self):
        for ref in ("refs/tags/v999.0.0", self.ref + ";bad", self.ref + "-rc1"):
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                release_version(ref)

    def test_allowlisted_archive_and_metadata(self):
        # Unrelated private-looking files beside the EXE must never enter the ZIP.
        (self.folder / "device.local.toml").write_text("do-not-package")
        archive = self.archive()
        with zipfile.ZipFile(archive) as handle:
            self.assertEqual(set(handle.namelist()), set(MEMBERS))
        manifest = verify_archive(archive, self.ref, self.commit)
        self.assertEqual(manifest["version"], self.version)
        self.assertEqual(manifest["source_commit"], self.commit)
        self.assertTrue(Path(str(archive) + ".sha256").is_file())

    def test_modified_executable_is_rejected(self):
        archive = self.archive()
        self.rewrite(archive, "cardputer.exe", self.exe.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "payload_hash"):
            verify_archive(archive, self.ref, self.commit)

    def test_extra_or_traversal_member_is_rejected(self):
        for name in ("device.local.toml", "../escape"):
            archive = self.archive()
            self.rewrite(archive, name, b"unexpected")
            with self.assertRaisesRegex(ValueError, "zip_members"):
                verify_archive(archive, self.ref, self.commit)

    def test_wrong_commit_or_manifest_is_rejected(self):
        archive = self.archive()
        with self.assertRaisesRegex(ValueError, "manifest"):
            verify_archive(archive, self.ref, "b" * 40)
        with zipfile.ZipFile(archive) as handle:
            manifest = json.loads(handle.read("manifest.json"))
        manifest["version"] = "999.0.0"
        self.rewrite(archive, "manifest.json", json.dumps(manifest).encode())
        with self.assertRaisesRegex(ValueError, "manifest"):
            verify_archive(archive, self.ref, self.commit)

    def test_linux_or_wrong_arch_executable_rejected(self):
        for data in (b"\x7fELF", b"MZ" + bytes(70)):
            self.exe.write_bytes(data)
            with self.assertRaises(ValueError):
                self.archive()

    def test_frozen_adapter_excludes_executable_installation(self):
        path = ROOT / "src/cardputer_mcp/client_environment.py"
        spec = importlib.util.spec_from_file_location("release_client_environment", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(module)
        from unittest.mock import patch
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(self.exe)):
            self.assertEqual(module._adapter_root(), self.folder.resolve())


if __name__ == "__main__":
    unittest.main()
