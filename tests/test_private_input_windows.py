"""Native synthetic tests. All mutations are inside TemporaryDirectory."""

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path

from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import inventory_digest
from cardputer_mcp.private_input_windows import (
    PrivateInputReadError,
    _Native,
    _root_parts,
    read_registered_windows,
)


class RootSyntaxTests(unittest.TestCase):
    def test_rejected_roots(self):
        for value in (
            "relative",
            "C:relative",
            "\\\\server\\share",
            "\\\\?\\C:\\x",
            "C:/x",
            "C:\\x\\..\\y",
            "C:\\x\\.\\y",
            "C:\\x ",
            "C:\\x:ads",
        ):
            with self.subTest(value=value), self.assertRaises(PrivateInputReadError):
                _root_parts(value)


@unittest.skipUnless(sys.platform == "win32", "native Windows required")
class NativeReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="broker-reader-fixture-")
        self.root = Path(self.temp.name) / "input"
        self.root.mkdir()
        self.file = self.root / "asset.bin"
        self.file.write_bytes(b"good")
        self.manifest = (InputFile("asset.bin", 4, hashlib.sha256(b"good").hexdigest()),)
        self.registration = PrivateInputRegistration(
            local_root=self.root,
            destination="private-input",
            source_commit="a" * 40,
            inventory_sha256=inventory_digest(self.manifest),
            recipe_sha256="b" * 64,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_root_ancestors_and_read(self):
        self.assertEqual(
            list(read_registered_windows(self.registration, self.manifest)),
            [("asset.bin", b"good")],
        )

    def test_junction_as_registered_root_rejected(self):
        link = Path(self.temp.name) / "junction"
        result = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(self.root)], capture_output=True
        )
        self.assertEqual(result.returncode, 0, "synthetic junction setup failed")
        try:
            reg = self.registration.model_copy(update={"local_root": link})
            with self.assertRaises(PrivateInputReadError):
                list(read_registered_windows(reg, self.manifest))
        finally:
            os.rmdir(link)

    def test_hardlink_rejected(self):
        os.link(self.file, self.root / "other.bin")
        with self.assertRaisesRegex(PrivateInputReadError, "links_rejected"):
            list(read_registered_windows(self.registration, self.manifest))

    def test_size_or_hash_change_rejected(self):
        for value in (b"evil", b"go", b"goodmore"):
            self.file.write_bytes(value)
            with self.subTest(value=value), self.assertRaises(PrivateInputReadError):
                list(read_registered_windows(self.registration, self.manifest))

    def test_cancellation_closes_ancestors(self):
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 4

        with self.assertRaisesRegex(PrivateInputReadError, "cancelled"):
            list(read_registered_windows(self.registration, self.manifest, cancelled=cancelled))
        moved = self.root.with_name("moved")
        self.root.rename(moved)
        moved.rename(self.root)

    def test_early_consumer_close_releases_ancestors(self):
        with closing(read_registered_windows(self.registration, self.manifest)) as reader:
            self.assertEqual(next(reader), ("asset.bin", b"good"))
            with self.assertRaises(PermissionError):
                self.root.rename(self.root.with_name("moved"))
        moved = self.root.with_name("moved")
        self.root.rename(moved)
        moved.rename(self.root)

    def test_held_file_blocks_write_and_ancestor_rename(self):
        native = _Native()
        with ExitStack() as stack:
            parts = _root_parts(str(self.root))
            current = native.anchor(parts[0], stack)
            for part in parts[1:]:
                current = native.child(current, part, True, stack)
            native.child(current, "asset.bin", False, stack)
            with self.assertRaises(PermissionError):
                self.file.write_bytes(b"evil")
            with self.assertRaises(PermissionError):
                self.root.rename(self.root.with_name("moved"))
        self.file.write_bytes(b"good")


if __name__ == "__main__":
    unittest.main()
