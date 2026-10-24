"""Registration digests bind approved content, not storage locations."""

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_source_lifecycle import CacheFixture

from cardputer_mcp.config import BuildProject, RegisteredBuildTool, SourceRepository
from cardputer_mcp.flash_models import FlashDevice
from cardputer_mcp.registration_digest import (
    flash_digest,
    flash_digests,
    project_digest,
    project_digests,
    source_digest,
    source_digests,
)
from cardputer_mcp.source_snapshot import SourceError, SourceSnapshots, registration_digest

BASE = Path(tempfile.gettempdir()).resolve() / "registration-digest-fixture"
SHA_A, SHA_B = "a" * 64, "b" * 64


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def source(root="cache", **kwargs):
    values = {"github_repository": "owner/repo", "cache_root": BASE / root, **kwargs}
    return SourceRepository(**values)


def tool(name="node.exe", digest=SHA_A):
    return RegisteredBuildTool(executable=BASE / name, sha256=digest, version="v1")


def project(**kwargs):
    values = {
        "project_dir": BASE / "project",
        "trusted_profile": BASE / "profile.ps1",
        "build_root": BASE / "out",
        "node_tool": tool(),
        "source_repository": source(),
        **kwargs,
    }
    return BuildProject(**values)


def flash(**kwargs):
    values = {
        "selector": {"vid": 1, "pid": 2, "serial_number": "serial"},
        "flash_bytes": 8388608,
        "project_id": "project",
        "python_tool": {
            "executable": BASE / "python.exe",
            "sha256": SHA_A,
            "version": "Python 3.13.1",
        },
        "esptool_directory": BASE / "esptool",
        "esptool_sha256": SHA_A,
        "backup_directory": BASE / "backups",
        "allow_flash": True,
        **kwargs,
    }
    return FlashDevice(**values)


def old_source_json(repository, root, acl, submodules):
    value = {"github_repository": repository, "cache_root": str(root), "private_acl_confirmed": acl}
    if submodules:
        value["submodule_repositories"] = submodules
    return json.dumps(value, separators=(",", ":"))


class SourceDigestTests(unittest.TestCase):
    def test_location_fields_do_not_change_the_digest(self):
        base = source_digest(source())
        self.assertEqual(base, source_digest(source("elsewhere")))
        self.assertEqual(base, source_digest(source(private_acl_confirmed=True)))
        self.assertEqual(base, source_digest(source(previous_cache_roots=[BASE / "old"])))
        self.assertEqual(base, registration_digest(source("moved")))
        base = project_digest(project())
        self.assertEqual(base, project_digest(project(build_root=BASE / "elsewhere")))
        self.assertEqual(base, project_digest(project(source_repository=source("moved"))))
        base = flash_digest(flash())
        self.assertEqual(base, flash_digest(flash(backup_directory=BASE / "elsewhere")))
        self.assertEqual(base, flash_digest(flash(private_backup_acl_confirmed=True)))

    def test_content_fields_still_change_the_digest(self):
        base = source_digest(source())
        self.assertNotEqual(base, source_digest(source(github_repository="owner/other")))
        self.assertNotEqual(base, source_digest(source(submodule_repositories=["a/b"])))
        self.assertNotEqual(
            source_digest(source(submodule_repositories=["a/b"])),
            source_digest(source(submodule_repositories=["a/c"])),
        )

    def test_old_style_digests_are_reproduced_exactly(self):
        for submodules in ([], ["a/b"]):
            for acl in (False, True):
                registration = source(private_acl_confirmed=acl, submodule_repositories=submodules)
                expected = sha(old_source_json("owner/repo", BASE / "cache", acl, submodules))
                self.assertIn(expected, source_digests(registration))
                self.assertIn(expected, source_digests(registration, schema_version=2))
        current = project()
        old = sha(current.model_dump_json(exclude={"source_repository": {"previous_cache_roots"}}))
        self.assertIn(old, project_digests(current))
        self.assertNotIn(sha("unknown"), project_digests(current))
        self.assertIn(sha(flash().model_dump_json()), flash_digests(flash()))

    def test_only_listed_previous_roots_are_trusted(self):
        old = sha(old_source_json("owner/repo", BASE / "old", False, []))
        self.assertNotIn(old, source_digests(source()))
        self.assertNotIn(old, source_digests(source(previous_cache_roots=[BASE / "other"])))
        self.assertIn(old, source_digests(source(previous_cache_roots=[BASE / "old"])))
        for roots in ([Path("relative")], [BASE / ".." / "x"], [BASE / "cache"]):
            with self.assertRaises(ValueError):
                source(previous_cache_roots=roots)
        with self.assertRaises(ValueError):
            source(previous_cache_roots=[BASE / "o", BASE / "o"])


class BuildAndFlashDigestTests(unittest.TestCase):
    def test_anything_that_decides_what_runs_still_changes_it(self):
        base = project_digest(project())
        for change in (
            {"node_tool": tool(digest=SHA_B)},
            {"node_tool": tool("other.exe")},
            {"trusted_profile": BASE / "other.ps1"},
            {"project_dir": BASE / "other-project"},
            {"profile_execution_policy": "RemoteSigned"},
            {"source_repository": source(github_repository="owner/other")},
            {"source_repository": source(submodule_repositories=["a/b"])},
        ):
            with self.subTest(change=sorted(change)):
                self.assertNotEqual(base, project_digest(project(**change)))

    def test_flash_identity_and_permissions_still_change_it(self):
        base = flash_digest(flash())
        for change in (
            {"esptool_sha256": SHA_B},
            {"esptool_directory": BASE / "other-esptool"},
            {"allow_flash": False},
            {"allow_backup": True},
            {"project_id": "other"},
            {"selector": {"vid": 1, "pid": 3, "serial_number": "serial"}},
        ):
            with self.subTest(change=sorted(change)):
                self.assertNotEqual(base, flash_digest(flash(**change)))


class MovedCacheRootTests(CacheFixture, unittest.TestCase):
    """Real receipts survive a folder move; old-style ones need an explicit listing."""

    def setUp(self):
        self.setup_cache()
        self.cache = self.root / "cache"
        self.use_root(self.cache)

    def use_root(self, root, previous=()):
        registration = SourceRepository(
            github_repository="fixture/project",
            cache_root=root,
            private_acl_confirmed=True,
            previous_cache_roots=list(previous),
        )
        self.settings.build_projects["fixture"].source_repository = registration
        self.snapshots = SourceSnapshots(self.settings)
        self.snapshots.retention = self.evidence

    def move_to(self, name, previous=()):
        target = self.root / name
        shutil.move(self.cache, target)
        self.cache = target
        self.use_root(target, previous)

    def old_style_fetch(self):
        """Write a receipt bound to the pre-change digest (cache root included)."""
        from unittest.mock import patch

        old_root = self.cache
        legacy = sha(old_source_json("fixture/project", old_root, True, []))
        with patch("cardputer_mcp.source_snapshot.registration_digest", lambda *_a, **_k: legacy):
            return self.fetch(1)

    def test_new_receipt_survives_a_move_without_any_listing(self):
        result = self.fetch(1)
        self.move_to("moved-cache")
        path, value = self.snapshots.resolve("fixture", result["source_receipt"], "owner")
        self.assertTrue(path.is_dir())
        self.assertEqual(value["registration_sha256"], source_digest(self.registration_for_now()))

    def registration_for_now(self):
        return self.settings.build_projects["fixture"].source_repository

    def test_old_style_receipt_is_rejected_after_a_move_unless_listed(self):
        result = self.old_style_fetch()
        old_root = self.cache
        self.move_to("moved-cache")
        with self.assertRaisesRegex(SourceError, "source_receipt_not_owned"):
            self.snapshots.resolve("fixture", result["source_receipt"], "owner")
        self.use_root(self.cache, previous=[old_root])
        path, _ = self.snapshots.resolve("fixture", result["source_receipt"], "owner")
        self.assertTrue(path.is_dir())

    def test_changed_repository_still_invalidates_a_receipt(self):
        result = self.fetch(1)
        self.settings.build_projects["fixture"].source_repository = SourceRepository(
            github_repository="fixture/other",
            cache_root=self.cache,
            private_acl_confirmed=True,
        )
        with self.assertRaises(SourceError):
            self.snapshots.resolve("fixture", result["source_receipt"], "owner")


if __name__ == "__main__":
    unittest.main()
