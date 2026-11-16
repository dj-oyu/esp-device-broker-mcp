import hashlib
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest


class PrivateInputStoreTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.store = PrivateInputStore(self.db)
        self.files = (("a.c", b"synthetic"), ("b.h", b"fixture"))
        self.registration = PrivateInputRegistration(
            local_root=Path.cwd() / "unused",
            destination="main/private_input",
            source_commit="a" * 40,
            recipe_sha256="b" * 64,
            inventory_sha256=inventory_digest(
                tuple(
                    InputFile(path, len(data), hashlib.sha256(data).hexdigest())
                    for path, data in self.files
                )
            ),
        )
        self.args = dict(
            registration=self.registration,
            project_id="project",
            input_id="fixture",
            principal="owner",
        )

    def tearDown(self):
        self.db.close()

    def test_round_trip_restart_and_idempotent_ingest(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.db"
            with closing(sqlite3.connect(path)) as db:
                store = PrivateInputStore(db)
                receipt = store.ingest(**self.args, files=self.files)
            with closing(sqlite3.connect(path)) as db:
                store = PrivateInputStore(db)
                self.assertEqual(self.files, store.resolve(**self.args, receipt=receipt))
                self.assertEqual(receipt, store.ingest(**self.args, files=reversed(self.files)))
                self.assertEqual(
                    1, db.execute("SELECT COUNT(*) FROM private_input_receipts").fetchone()[0]
                )

    def test_owner_and_registration_isolation(self):
        receipt = self.store.ingest(**self.args, files=self.files)
        changes = (
            {"principal": "other"},
            {"project_id": "other"},
            {"input_id": "other"},
            {"registration": self.registration.model_copy(update={"source_commit": "c" * 40})},
        )
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.resolve(**(self.args | change), receipt=receipt)

    def test_failed_ingest_leaves_no_receipt(self):
        for files in ((("a.c", b"changed"),), (("../x", b"fixture"),), (("a", b"x"), ("A", b"x"))):
            with self.subTest(files=files), self.assertRaises(ValueError):
                self.store.ingest(**self.args, files=files)
        self.assertEqual(
            0, self.db.execute("SELECT COUNT(*) FROM private_input_receipts").fetchone()[0]
        )

    def test_cancel_rolls_back_both_tables(self):
        calls = 0

        def cancel_at_commit():
            nonlocal calls
            calls += 1
            return calls == 3

        with self.assertRaisesRegex(ValueError, "cancelled"):
            self.store.ingest(**self.args, files=self.files, cancelled=cancel_at_commit)
        for table in ("private_input_receipts", "private_input_bytes"):
            self.assertEqual(0, self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_tamper_and_forged_receipt(self):
        receipt = self.store.ingest(**self.args, files=self.files)
        self.db.execute("UPDATE private_input_bytes SET payload=? WHERE path='a.c'", (b"tampered",))
        self.db.commit()
        for value in (receipt, "f" * 64):
            with self.assertRaises(ValueError):
                self.store.resolve(**self.args, receipt=value)
        with self.assertRaises(ValueError):
            self.store.ingest(**self.args, files=self.files)

    def test_no_interference_with_active_transaction(self):
        self.db.execute("CREATE TABLE unrelated(value TEXT)")
        self.db.execute("INSERT INTO unrelated VALUES ('uncommitted')")
        with self.assertRaisesRegex(ValueError, "transaction_active"):
            PrivateInputStore(self.db)
        with self.assertRaisesRegex(ValueError, "transaction_active"):
            self.store.ingest(**self.args, files=self.files)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(0, self.db.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0])

    def test_write_failure_rolls_back_receipt(self):
        self.db.execute("""
            CREATE TRIGGER fail_synthetic_write BEFORE INSERT ON private_input_bytes
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest(**self.args, files=self.files)
        self.assertEqual(
            0, self.db.execute("SELECT COUNT(*) FROM private_input_receipts").fetchone()[0]
        )

    def test_registered_limits(self):
        for limits in ({"max_files": 1}, {"max_bytes": 1}):
            registration = self.registration.model_copy(update=limits)
            with self.assertRaisesRegex(ValueError, "limit"):
                self.store.ingest(**(self.args | {"registration": registration}), files=self.files)

    @unittest.skipUnless(hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"), "Python 3.12 API")
    def test_explicit_autocommit_mode_commits_and_rolls_back(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "autocommit.db"
            db = sqlite3.connect(path, autocommit=True)
            store = PrivateInputStore(db)
            receipt = store.ingest(**self.args, files=self.files)
            self.assertFalse(db.in_transaction)
            db.close()
            db = sqlite3.connect(path, autocommit=True)
            store = PrivateInputStore(db)
            self.assertEqual(self.files, store.resolve(**self.args, receipt=receipt))
            calls = 0

            def cancel_at_commit():
                nonlocal calls
                calls += 1
                return calls == 3

            with self.assertRaisesRegex(ValueError, "cancelled"):
                store.ingest(
                    **(self.args | {"principal": "another"}),
                    files=self.files,
                    cancelled=cancel_at_commit,
                )
            self.assertFalse(db.in_transaction)
            self.assertEqual(
                1, db.execute("SELECT COUNT(*) FROM private_input_receipts").fetchone()[0]
            )
            db.close()
