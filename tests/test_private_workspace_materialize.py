import hashlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest
from cardputer_mcp.private_workspace_materialize import _supported, materialize_workspace
from cardputer_mcp.private_workspace_plan import WorkspacePlan, prepare_workspace
from cardputer_mcp.private_workspace_root import private_workspace_root


@unittest.skipUnless(_supported(), "requires tested POSIX filesystem primitives")
class MaterializeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(self.tmp.name)
        self.plan = WorkspacePlan("a" * 64, (("src/input.bin", b"synthetic"),))

    def test_copy_cleanup_and_cache_independence(self):
        with materialize_workspace(self.plan, protected_parent=self.parent) as stage:
            self.assertEqual((stage / "src/input.bin").read_bytes(), b"synthetic")
            (stage / "src/input.bin").write_bytes(b"build changed")
        self.assertFalse(stage.exists())
        self.assertEqual(self.plan.files[0][1], b"synthetic")

    def test_invalid_plans_create_nothing(self):
        for files in (
            (("../bad", b"x"),),
            (("a", b"x"), ("A", b"y")),
            (("a", b"x"), ("a/b", b"y")),
            (("a", bytearray(b"x")),),
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                with materialize_workspace(
                    WorkspacePlan("a" * 64, files), protected_parent=self.parent
                ):
                    self.fail("invalid staging yielded")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_cancel_and_write_failure_cleanup(self):
        for hooks in ({"cancelled": iter((False, False, True)).__next__}, {}):
            fault = None if hooks else OSError("sensitive path")
            with patch("cardputer_mcp.private_workspace_materialize._write", side_effect=fault):
                with self.assertRaises(ValueError) as raised:
                    with materialize_workspace(self.plan, protected_parent=self.parent, **hooks):
                        self.fail("incomplete staging yielded")
                self.assertNotIn("sensitive", str(raised.exception))
            self.assertEqual(list(self.parent.iterdir()), [])

    def test_build_exception_cleanup(self):
        with self.assertRaisesRegex(RuntimeError, "build failed"):
            with materialize_workspace(self.plan, protected_parent=self.parent):
                raise RuntimeError("build failed")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_unsupported_platform_and_symlink_parent(self):
        with patch("cardputer_mcp.private_workspace_materialize._supported", return_value=False):
            with self.assertRaisesRegex(ValueError, "platform_unsupported"):
                with materialize_workspace(self.plan, protected_parent=self.parent):
                    self.fail()
        link = self.parent / "alias"
        link.symlink_to(self.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "parent_invalid"):
            with materialize_workspace(self.plan, protected_parent=link):
                self.fail()

    def test_injected_directory_symlink_cannot_escape(self):
        from cardputer_mcp.private_workspace_materialize import _write

        outside = self.parent / "outside"
        outside.mkdir()

        def inject(root_fd, path, data):
            import os

            os.symlink(outside, "src", dir_fd=root_fd)
            _write(root_fd, path, data)

        with patch("cardputer_mcp.private_workspace_materialize._write", side_effect=inject):
            with self.assertRaisesRegex(ValueError, "io_failed"):
                with materialize_workspace(self.plan, protected_parent=self.parent):
                    self.fail("symlink staging yielded")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(list(self.parent.iterdir()), [outside])

    def test_receipt_to_synthetic_build(self):
        # Only generated fixture bytes; this is not ESP-IDF or a broker job.
        private = (("fixture.bin", b"synthetic input"),)
        inv = tuple(InputFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in private)
        reg = PrivateInputRegistration(
            local_root=self.parent / "unused",
            destination="assets",
            source_commit="a" * 40,
            recipe_sha256="b" * 64,
            inventory_sha256=inventory_digest(inv),
        )
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        store = PrivateInputStore(db)
        receipt = store.ingest(
            registration=reg, project_id="p", input_id="i", principal="owner", files=private
        )
        source = (("source.txt", b"synthetic source"),)
        plan = prepare_workspace(
            store=store,
            registration=reg,
            project_id="p",
            input_id="i",
            principal="owner",
            private_receipt=receipt,
            source_receipt="c" * 64,
            source_commit="a" * 40,
            source_bytes=source,
            source_inventory=(
                InputFile(
                    "source.txt", len(source[0][1]), hashlib.sha256(source[0][1]).hexdigest()
                ),
            ),
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as container:
            with private_workspace_root(Path(container)) as root:
                with materialize_workspace(plan, protected_parent=root) as stage:
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-I",
                            "-c",
                            "from pathlib import Path; import hashlib; "
                            "Path('output.bin').write_bytes(hashlib.sha256(Path('source.txt').read_bytes()+"
                            "Path('assets/fixture.bin').read_bytes()).digest())",
                        ],
                        cwd=stage,
                        capture_output=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(
                        (stage / "output.bin").read_bytes(),
                        hashlib.sha256(b"synthetic sourcesynthetic input").digest(),
                    )
        self.assertEqual(list(self.parent.iterdir()), [])
