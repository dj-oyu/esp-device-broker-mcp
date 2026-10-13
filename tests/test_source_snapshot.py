"""Offline archives and immutable-source receipts; no downloads or SDK installs."""

import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cardputer_mcp.config import BuildProject, Settings, SourceRepository
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.models import PrepareRequest, TestName as Action
from cardputer_mcp.source_snapshot import SourceError, SourceSnapshots, extract, file_receipt, tree_hash

COMMIT = "a" * 40
REPOSITORY = "fixture/project"
FILES = {"CMakeLists.txt": b"project(synthetic)\n", "sdkconfig.defaults": b"CONFIG_IDF_TARGET=esp32s3\n"}


def archive(files=FILES, *, mode=stat.S_IFREG | 0o644):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, data in files.items():
            entry = zipfile.ZipInfo("project-" + COMMIT + "/" + name)
            entry.external_attr = mode << 16
            zipped.writestr(entry, data)
    return stream.getvalue()


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = self.root / "profile.ps1"
        self.profile.write_text("synthetic profile")
        self.settings = Settings(
            state_dir=self.root / "state",
            build_projects={"fixture": BuildProject(
                project_dir=self.root / "user-worktree", trusted_profile=self.profile,
                build_root=self.root / "build-output", build_min_free_bytes=0,
                source_repository=SourceRepository(
                    github_repository=REPOSITORY, cache_root=self.root / "cache",
                    private_acl_confirmed=True,
                ),
            )},
        )
        receipts = {name: file_receipt(data, "100644") for name, data in FILES.items()}
        self.tree = tree_hash(receipts)
        self.urls = []

        def transport(url, deadline, live, limit):
            self.urls.append(url)
            if not live():
                raise SourceError("source_fetch_interrupted")
            if url.startswith("https://api.github.com/"):
                return json.dumps({"sha": COMMIT, "tree": {"sha": self.tree}}).encode()
            return archive()

        self.snapshots = SourceSnapshots(self.settings, transport)

    def tearDown(self):
        self.temp.cleanup()

    def fetch(self, principal="owner", job="fetch-one"):
        plan = self.snapshots.prepare("fixture", COMMIT, principal) | {"job_id": job}
        return self.snapshots.fetch(plan, lambda: True, lambda _: None)

    def test_explicit_fetch_and_receipt_build_preserve_user_worktree(self):
        self.assertEqual(self.snapshots.usage("fixture")["bytes"], 0)
        self.assertFalse((self.root / "cache").exists())
        result = self.fetch()
        source, metadata = self.snapshots.resolve("fixture", result["source_receipt"], "owner")
        self.assertEqual(metadata["tree_sha1"], self.tree)
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertFalse((self.root / "user-worktree").exists())
        self.assertEqual((source / "CMakeLists.txt").read_bytes(), FILES["CMakeLists.txt"])
        plan = FirmwareBuild(self.settings).prepare("fixture", "owner", result["source_receipt"])
        self.assertEqual(plan["source_provenance"]["commit"], COMMIT)
        self.assertEqual(plan["source_provenance"]["source_sha256"], result["source_sha256"])
        self.assertEqual(len(self.urls), 2)
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 1)

    def test_principal_tampering_and_untracked_files_are_rejected(self):
        result = self.fetch()
        receipt = result["source_receipt"]
        with self.assertRaises(SourceError):
            self.snapshots.resolve("fixture", receipt, "other")
        source, _ = self.snapshots.resolve("fixture", receipt, "owner")
        (source / "unknown.c").write_bytes(b"unknown")
        with self.assertRaises(SourceError):
            self.snapshots.resolve("fixture", receipt, "owner")
        (source / "unknown.c").unlink()
        (source / "CMakeLists.txt").write_bytes(b"tampered")
        with self.assertRaises(SourceError):
            FirmwareBuild(self.settings).prepare("fixture", "owner", receipt)

    def test_git_tree_mismatch_and_revocation_publish_nothing(self):
        self.tree = "b" * 40
        with self.assertRaises(SourceError):
            self.fetch()
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)
        plan = self.snapshots.prepare("fixture", COMMIT, "owner") | {"job_id": "revoked"}
        with self.assertRaises(SourceError):
            self.snapshots.fetch(plan, lambda: False, lambda _: None)
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 0)

    def test_traversal_windows_aliases_symlinks_and_lfs_are_rejected(self):
        for files in (
            {"../outside": b"escape"}, {"CON.txt": b"alias"},
            {"a": b"one", "A": b"two"}, {".gitmodules": b"module"},
            {"large.bin": b"version https://git-lfs.github.com/spec/v1\n"},
        ):
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(SourceError):
                    extract(archive(files), Path(folder), "project-" + COMMIT, lambda: True)
        with self.assertRaises(SourceError):
            extract(archive(mode=stat.S_IFLNK | 0o777), self.root, "project-" + COMMIT, lambda: True)

    def test_expansion_and_cache_limits_fail_closed(self):
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", expanded_bytes=3):
            with self.assertRaises(SourceError):
                self.fetch()
        cache = self.root / "cache"
        for number in range(8):
            (cache / str(number)).mkdir()
        with self.assertRaisesRegex(SourceError, "source_cache_full"):
            self.fetch()

    def test_typed_contract_has_no_url_path_or_short_commit(self):
        for arguments in (
            {"source_commit": "a" * 8}, {"source_commit": COMMIT, "url": "https://example.test"},
            {"source_commit": COMMIT, "source_receipt": "b" * 64},
        ):
            with self.assertRaises(ValueError):
                PrepareRequest(session="c" * 32, job_id="one", project_id="fixture", test=Action.SOURCE, **arguments)


if __name__ == "__main__":
    unittest.main()
