import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardputer_mcp.private_workspace_materialize import _supported, materialize_workspace
from cardputer_mcp.private_workspace_plan import WorkspacePlan
from cardputer_mcp.private_workspace_root import _check_directory, private_workspace_root


@unittest.skipUnless(_supported(), "POSIX primitives required")
class PrivateRootTests(unittest.TestCase):
    def setUp(self):
        # CWD must itself have trusted ancestry; do not weaken the checker for /tmp.
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.container = Path(self.temp.name)

    def test_root_and_materializer_composition(self):
        plan = WorkspacePlan("a" * 64, (("fixture.txt", b"generated only"),))
        with private_workspace_root(self.container) as root:
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(root.stat().st_uid, os.geteuid())
            with materialize_workspace(plan, protected_parent=root) as stage:
                self.assertEqual((stage / "fixture.txt").read_bytes(), b"generated only")
        self.assertEqual(list(self.container.iterdir()), [])

    def test_caller_failure_cleans_root(self):
        with self.assertRaisesRegex(RuntimeError, "fixture failure"):
            with private_workspace_root(self.container) as root:
                (root / "synthetic").write_bytes(b"fixture")
                raise RuntimeError("fixture failure")
        self.assertEqual(list(self.container.iterdir()), [])

    def test_symlink_ancestor_rejected(self):
        link = self.container / "alias"
        link.symlink_to(self.container, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "io_failed"):
            with private_workspace_root(link):
                self.fail()
        self.assertEqual(list(self.container.iterdir()), [link])

    def test_writable_ancestry_rejected_without_mutation(self):
        # Synthetic stat fixtures avoid altering real directory permissions.
        actual = self.container.stat()
        for mode in (stat.S_IFDIR | 0o777, stat.S_IFDIR | 0o1777, stat.S_IFDIR | 0o775):
            fields = list(actual)
            fields[0] = mode
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "unsafe_mode"):
                _check_directory(os.stat_result(fields))
        self.assertEqual(self.container.stat().st_mode, actual.st_mode)

    def test_foreign_owner_and_private_readability_rejected(self):
        fields = list(self.container.stat())
        fields[4] = os.geteuid() + 12345
        with self.assertRaisesRegex(ValueError, "wrong_owner"):
            _check_directory(os.stat_result(fields))
        fields[4] = os.geteuid()
        fields[0] = stat.S_IFDIR | 0o750
        with self.assertRaisesRegex(ValueError, "unsafe_mode"):
            _check_directory(os.stat_result(fields), private=True)

    def test_unsupported_and_relative_rejected(self):
        with patch("cardputer_mcp.private_workspace_root._supported", return_value=False):
            with self.assertRaisesRegex(ValueError, "platform_unsupported"):
                with private_workspace_root(self.container):
                    self.fail()
        with self.assertRaisesRegex(ValueError, "invalid_path"):
            with private_workspace_root(Path("relative")):
                self.fail()
