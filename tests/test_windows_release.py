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
from release_windows import (  # noqa: E402
    MEMBERS,
    create_archive,
    digest,
    record_build_runtime,
    release_version,
    verify_archive,
)


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
        pe.extend(
            struct.pack("!8sIIII64s", b"MEI\014\013\012\013\016", 88, 0, 0, 314, b"python314.dll")
        )
        self.exe.write_bytes(pe)
        self.version = release_version("refs/heads/test")
        self.ref = f"refs/tags/v{self.version}"
        self.commit = "a" * 40
        self.runtime_path = Path(str(self.exe) + ".build.json")
        self.runtime = {
            "schema_version": 1,
            "python_version": "3.14.3",
            "uv_version": "0.12.19",
            "source_commit": self.commit,
            "exe_sha256": digest(self.exe.read_bytes()),
            "uv_lock_sha256": digest((ROOT / "uv.lock").read_bytes()),
            "build_requirements_sha256": digest(
                (ROOT / "packaging/requirements-windows-build.txt").read_bytes()
            ),
        }
        self.runtime_path.write_text(json.dumps(self.runtime), encoding="utf-8")

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
        self.assertEqual(manifest["python_version"], "3.14.3")
        self.assertTrue(Path(str(archive) + ".sha256").is_file())

    def test_manifest_records_build_python_instead_of_archiver_runtime(self):
        from unittest.mock import patch

        with patch("release_windows.platform.python_version", return_value="3.13.16"):
            archive = self.archive()
        manifest = verify_archive(archive, self.ref, self.commit)
        self.assertEqual(manifest["python_version"], "3.14.3")

    def test_build_runtime_receipt_binds_exe_source_and_lock(self):
        for field in ("exe_sha256", "source_commit", "uv_lock_sha256", "build_requirements_sha256"):
            with self.subTest(field=field):
                self.runtime_path.write_text(json.dumps(dict(self.runtime, **{field: "0" * 64})))
                with self.assertRaisesRegex(ValueError, "build_runtime_binding"):
                    self.archive()

    def test_314_exe_cannot_be_relabelled_as_313_by_receipt_edit(self):
        self.runtime_path.write_text(json.dumps(dict(self.runtime, python_version="3.13.16")))
        with self.assertRaisesRegex(ValueError, "build_runtime_abi"):
            self.archive()

    def test_archive_receipt_cannot_be_modified_after_publication(self):
        archive = self.archive()
        self.rewrite(
            archive,
            "build-runtime.json",
            json.dumps(dict(self.runtime, uv_version="9.9.9")).encode(),
        )
        with self.assertRaisesRegex(ValueError, "manifest"):
            verify_archive(archive, self.ref, self.commit)

    def test_actual_build_interpreter_writes_hash_bound_receipt(self):
        from unittest.mock import patch

        with (
            patch("release_windows.platform.python_version", return_value="3.14.3"),
            patch(
                "release_windows.subprocess.check_output",
                side_effect=["", self.commit + "\n", "uv 0.12.19\n"],
            ),
        ):
            receipt = record_build_runtime(self.exe)
        self.assertEqual(json.loads(receipt.read_text()), self.runtime)

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

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(self.exe)),
        ):
            self.assertEqual(module._adapter_root(), self.folder.resolve())

    def test_frozen_private_file_rejects_installation_and_extraction(self):
        path = ROOT / "src/cardputer_mcp/client_environment.py"
        spec = importlib.util.spec_from_file_location("release_client_boundary", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name)
        spec.loader.exec_module(module)
        from unittest.mock import patch

        app = self.folder / "application"
        extraction = self.folder / "extraction"
        private = self.folder / "private"
        for folder in (app, extraction, private):
            folder.mkdir()
            (folder / "client.env").write_text("dummy")
        original_lstat = Path.lstat

        def fixture_lstat(candidate):
            # Model a standalone installation. The cloud sandbox itself has Git
            # markers in /workspace and /tmp; fixture-owned markers remain real.
            if sys.platform == "linux" and candidate in (
                Path("/workspace/.git"),
                Path("/tmp/.git"),
            ):
                raise FileNotFoundError
            return original_lstat(candidate)

        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", str(app / "cardputer.exe")),
            patch.object(sys, "_MEIPASS", str(extraction), create=True),
            patch.object(Path, "lstat", fixture_lstat),
        ):
            for folder in (app, extraction):
                with self.assertRaises(module.ClientEnvironmentError):
                    module._check_location((folder / "client.env").resolve())
            module._check_location((private / "client.env").resolve())
            (private / ".git").mkdir()
            with self.assertRaises(module.ClientEnvironmentError):
                module._check_location((private / "client.env").resolve())


if __name__ == "__main__":
    unittest.main()
