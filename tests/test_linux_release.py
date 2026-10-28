"""Linux distribution boundary tests, stdlib-only so they run before dependency setup."""

import io
import json
import struct
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))
import release_linux  # noqa: E402
import release_windows  # noqa: E402
from release_linux import MEMBERS, create_archive, digest, verify_archive  # noqa: E402


def fake_elf(library: bytes = b"libpython3.14.so.1.0", machine: int = 0x3E) -> bytes:
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4], header[5] = 2, 1
    struct.pack_into("<H", header, 18, machine)
    header.extend(struct.pack("!8sIIII64s", b"MEI\014\013\012\013\016", 88, 0, 0, 314, library))
    return bytes(header)


class LinuxReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.exe = self.folder / "cardputer"
        self.version = release_windows.release_version("refs/heads/test")
        self.ref = f"refs/tags/v{release_windows.tag_spelling(self.version)}"
        self.commit = "b" * 40
        self.write_executable(fake_elf())

    def write_executable(self, data: bytes) -> None:
        self.exe.write_bytes(data)
        runtime = {
            "schema_version": 1,
            "python_version": "3.14.3",
            "uv_version": "0.12.19",
            "source_commit": self.commit,
            "exe_sha256": digest(data),
            "uv_lock_sha256": digest((ROOT / "uv.lock").read_bytes()),
            "build_requirements_sha256": digest(
                (ROOT / "packaging/requirements-linux-build.txt").read_bytes()
            ),
        }
        Path(str(self.exe) + ".build.json").write_text(json.dumps(runtime), encoding="utf-8")

    def archive(self) -> Path:
        return create_archive(self.exe, self.folder / "dist", self.ref, self.commit)

    def rewrite(self, archive: Path, name: str, data: bytes) -> None:
        members = release_linux.read_tar(archive)
        members[name] = data
        release_linux.write_tar(archive, members)

    def test_allowlisted_archive_and_metadata(self):
        archive = self.archive()
        self.assertEqual(archive.name, f"cardputer-v{self.version}-linux-x64.tar.gz")
        with tarfile.open(archive) as handle:
            names = handle.getnames()
            modes = {info.name: info.mode for info in handle.getmembers()}
        self.assertEqual(sorted(names), sorted(MEMBERS))
        self.assertEqual(modes["cardputer"], 0o755)
        manifest = verify_archive(archive, self.ref, self.commit)
        self.assertEqual(manifest["platform"], "linux-x64")
        self.assertEqual(manifest["python_version"], "3.14.3")
        sidecar = (self.folder / "dist" / (archive.name + ".sha256")).read_text()
        self.assertEqual(sidecar, f"{digest(archive.read_bytes())}  {archive.name}\n")

    def test_same_inputs_give_the_same_archive(self):
        first = self.archive().read_bytes()
        again = create_archive(self.exe, self.folder / "again", self.ref, self.commit)
        self.assertEqual(first, again.read_bytes())

    def test_windows_or_wrong_arch_executable_rejected(self):
        for data in (b"MZ" + bytes(100), fake_elf(machine=0xB7)):
            self.write_executable(data)
            with self.assertRaises(ValueError):
                self.archive()

    def test_python_abi_must_match_receipt(self):
        self.write_executable(fake_elf(library=b"libpython3.13.so.1.0"))
        with self.assertRaisesRegex(ValueError, "build_runtime_abi_mismatch"):
            self.archive()

    def test_modified_executable_is_rejected(self):
        archive = self.archive()
        self.rewrite(archive, "cardputer", fake_elf() + b"x")
        with self.assertRaises(ValueError):
            verify_archive(archive, self.ref, self.commit)

    def test_extra_or_traversal_member_is_rejected(self):
        archive = self.archive()
        for name in ("extra.txt", "../cardputer"):
            members = release_linux.read_tar(archive)
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
                for member, data in {**members, name: b"x"}.items():
                    info = tarfile.TarInfo(member)
                    info.size = len(data)
                    handle.addfile(info, io.BytesIO(data))
            bad = self.folder / "bad.tar.gz"
            bad.write_bytes(buffer.getvalue())
            with self.assertRaises(ValueError):
                verify_archive(bad, self.ref, self.commit)

    def test_wrong_commit_or_ref_is_rejected(self):
        archive = self.archive()
        with self.assertRaises(ValueError):
            verify_archive(archive, self.ref, "c" * 40)
        with self.assertRaises(ValueError):
            verify_archive(archive, "refs/heads/other", self.commit)


class ReleaseCandidateTagTests(unittest.TestCase):
    def test_candidate_is_tagged_with_a_dash_and_dot(self):
        self.assertEqual(release_windows.tag_spelling("1.0.0rc1"), "1.0.0-rc.1")
        self.assertEqual(release_windows.tag_spelling("0.13.1"), "0.13.1")


if __name__ == "__main__":
    unittest.main()
