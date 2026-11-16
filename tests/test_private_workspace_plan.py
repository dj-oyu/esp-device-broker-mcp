import hashlib
import sqlite3
import unittest
from pathlib import Path

from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest
from cardputer_mcp.private_workspace_plan import prepare_workspace


def inventory(files):
    return tuple(InputFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in files)


class WorkspacePlanTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = PrivateInputStore(self.db)
        private = (("synthetic.bin", b"private fixture"),)
        self.reg = PrivateInputRegistration(
            local_root=Path.cwd() / "unused",
            destination="main/private",
            source_commit="a" * 40,
            recipe_sha256="b" * 64,
            inventory_sha256=inventory_digest(inventory(private)),
        )
        receipt = self.store.ingest(
            registration=self.reg, project_id="p", input_id="i", principal="owner", files=private
        )
        source = (("main/main.c", b"public fixture"),)
        self.args = dict(
            store=self.store,
            registration=self.reg,
            project_id="p",
            input_id="i",
            principal="owner",
            private_receipt=receipt,
            source_receipt="c" * 64,
            source_commit="a" * 40,
            source_inventory=inventory(source),
            source_bytes=source,
        )

    def tearDown(self):
        self.db.close()

    def test_composition_does_not_mutate_source_or_store(self):
        before = tuple(self.db.iterdump())
        source = self.args["source_bytes"]
        plan = prepare_workspace(**self.args)
        self.assertEqual(plan, prepare_workspace(**self.args))
        self.assertEqual(source, self.args["source_bytes"])
        self.assertEqual(before, tuple(self.db.iterdump()))
        self.assertEqual(2, len(plan.files))
        self.assertNotIn("private fixture", repr(plan))
        self.assertNotIn("synthetic.bin", repr(plan))

    def test_source_tamper_and_wrong_owner_rejected(self):
        for change in (
            {"source_bytes": (("main/main.c", b"changed"),)},
            {"principal": "other"},
            {"source_commit": "d" * 40},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare_workspace(**(self.args | change))

    def test_collision_alias_and_parent_rejected(self):
        for path in ("main/private/synthetic.bin", "MAIN/PRIVATE/SYNTHETIC.BIN", "main"):
            source = ((path, b"fixture"),)
            with self.subTest(path=path), self.assertRaises(ValueError):
                prepare_workspace(
                    **(self.args | {"source_bytes": source, "source_inventory": inventory(source)})
                )

    def test_mutable_payload_rejected(self):
        with self.assertRaises(ValueError):
            prepare_workspace(**(self.args | {"source_bytes": (("a", bytearray(b"x")),)}))

    def test_identity_binds_public_receipt(self):
        first = prepare_workspace(**self.args)
        second = prepare_workspace(**(self.args | {"source_receipt": "d" * 64}))
        self.assertEqual(first.files, second.files)
        self.assertNotEqual(first.identity, second.identity)
