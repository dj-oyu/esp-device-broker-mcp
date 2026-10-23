"""Registered source roots are pinned to their resolved directory; links below stay rejected."""

import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from test_source_lifecycle import CacheFixture

from cardputer_mcp.source_snapshot import SourceError, pin_root


def make_dir_link(link: Path, target: Path) -> None:
    """Directory symlink, or an unprivileged Windows junction; skip if neither works."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError:
        pass
    if os.name == "nt":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        if done.returncode == 0:
            return
    raise unittest.SkipTest("directory links are unavailable")


def remove_dir_link(link: Path) -> None:
    # Never recurse through a link: unlink the link itself.
    try:
        os.rmdir(link)
    except OSError:
        link.unlink()


class PinnedRootTests(CacheFixture, unittest.TestCase):
    def setUp(self):
        self.setup_cache()
        self.real = self.root / "real-cache"
        self.real.mkdir(mode=0o700)
        self.cache = self.root / "cache"
        make_dir_link(self.cache, self.real)
        self.addCleanup(remove_dir_link, self.cache)

    def test_registered_root_that_is_a_link_works_for_fetch_reuse_and_reclaim(self):
        first = self.fetch(1)
        self.assertTrue((self.real / first["source_receipt"] / "source").is_dir())
        reused = self.fetch(1, job="source-1-again")
        self.assertTrue(reused["reused"])
        self.assertEqual(self.downloads, 1)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            second = self.fetch(2)
        self.assertFalse((self.real / first["source_receipt"] / "source").exists())
        self.assertTrue((self.real / first["source_receipt"] / "receipt.json").exists())
        self.assertTrue((self.real / second["source_receipt"] / "source").is_dir())

    def test_two_paths_to_one_directory_share_one_owner_lock(self):
        self.fetch(1)
        alias = self.root / "alias"
        make_dir_link(alias, self.real)
        self.addCleanup(remove_dir_link, alias)
        project = self.settings.build_projects["fixture"]
        project.source_repository.cache_root = alias
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 1)

    def test_link_inside_the_root_is_still_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("keep")
        first = self.fetch(1)
        make_dir_link(self.real / ("0" * 64), outside)
        self.addCleanup(remove_dir_link, self.real / ("0" * 64))
        with self.assertRaisesRegex(SourceError, "source_linked_path_rejected"):
            self.snapshots.usage("fixture")
        self.assertEqual((outside / "secret.txt").read_text(), "keep")
        self.assertTrue((self.real / first["source_receipt"] / "source").is_dir())

    def test_reclaim_never_follows_a_link_inside_a_payload(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("keep")
        first = self.fetch(1)
        payload = self.real / first["source_receipt"] / "source"
        make_dir_link(payload / "escape", outside)
        self.addCleanup(remove_dir_link, payload / "escape")
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            with self.assertRaises(SourceError):
                self.fetch(2)
        self.assertEqual((outside / "secret.txt").read_text(), "keep")

    def test_retargeting_the_root_during_an_operation_fails_closed(self):
        self.fetch(1)
        other = self.root / "other-real"
        other.mkdir(mode=0o700)
        registration = self.settings.build_projects["fixture"].source_repository
        with self.snapshots.cache_guard("fixture"):
            self.assertEqual(self.snapshots.cache_root(registration), self.real.resolve())
            remove_dir_link(self.cache)
            make_dir_link(self.cache, other)
            with self.assertRaisesRegex(SourceError, "source_cache_root_retargeted"):
                self.snapshots.cache_root(registration)
        remove_dir_link(self.cache)
        make_dir_link(self.cache, self.real)

    def test_overlap_is_checked_on_resolved_paths(self):
        project = self.settings.build_projects["fixture"]
        # The link target is inside the worktree even though the link path is not.
        project.project_dir.mkdir(exist_ok=True)
        inside = project.project_dir / "cache-target"
        inside.mkdir(mode=0o700)
        remove_dir_link(self.cache)
        make_dir_link(self.cache, inside)
        with self.assertRaisesRegex(SourceError, "source_cache_root_conflict"):
            self.snapshots.usage("fixture")
        remove_dir_link(self.cache)
        make_dir_link(self.cache, self.real)

    def test_filesystem_root_is_rejected(self):
        with self.assertRaisesRegex(SourceError, "source_cache_root_conflict"):
            pin_root(Path(self.root.anchor))


class PlainRootTests(CacheFixture, unittest.TestCase):
    def setUp(self):
        self.setup_cache()

    def test_non_link_root_behaves_as_before(self):
        registration = self.settings.build_projects["fixture"].source_repository
        first = self.fetch(1)
        self.assertEqual(self.snapshots.cache_root(registration), (self.root / "cache").resolve())
        self.assertTrue(self.payload(first).is_dir())
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 1)


if __name__ == "__main__":
    unittest.main()
