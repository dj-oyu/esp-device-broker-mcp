import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cardputer_mcp.config import BuildProject
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_build_binding import plan_kwargs
from cardputer_mcp.private_input_contract import InputFile
from cardputer_mcp.private_input_registration import PrivateInputRegistration
from cardputer_mcp.private_input_store import PrivateInputStore, inventory_digest


class PrivateBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "CMakeLists.txt").write_text("# synthetic fixture")
        profile = root / "profile.ps1"
        profile.write_text("# synthetic profile, never executed")
        private = (("fixture", b"generated"),)
        self.reg = PrivateInputRegistration(
            local_root=root / "unused",
            destination="assets",
            source_commit="a" * 40,
            recipe_sha256="b" * 64,
            inventory_sha256=inventory_digest(
                (InputFile("fixture", 9, hashlib.sha256(b"generated").hexdigest()),)
            ),
        )
        self.project = BuildProject(
            project_dir=root, trusted_profile=profile, private_inputs={"fixture": self.reg}
        )
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.store = PrivateInputStore(self.db)
        self.receipt = self.store.ingest(
            registration=self.reg,
            project_id="p",
            input_id="fixture",
            principal="owner",
            files=private,
        )
        self.builder = FirmwareBuild(SimpleNamespace(build_projects={"p": self.project}))
        self.builder.private_store_factory = lambda: nullcontext(self.store)
        self.args = dict(
            source_receipt="c" * 64, private_input_id="fixture", private_input_receipt=self.receipt
        )
        # SourceSnapshots owns public receipt verification; the fixture supplies
        # that verified boundary, rather than treating arbitrary bytes as Git.
        self.project_patch = patch.object(
            self.builder, "_project", return_value=(self.project, {"commit": "a" * 40})
        )
        self.project_patch.start()
        self.addCleanup(self.project_patch.stop)

    def test_private_binding_changes_fingerprint_and_no_paths_escape(self):
        public = self.builder.prepare("p", "owner", source_receipt="c" * 64)
        private = self.builder.prepare("p", "owner", **self.args)
        self.assertNotEqual(public["fingerprint"], private["fingerprint"])
        self.assertEqual(private, self.builder.prepare("p", "owner", **self.args))
        self.assertNotIn(str(self.reg.local_root), repr(private))
        self.assertNotIn("private_binding_sha256", public)
        self.assertEqual(
            public,
            self.builder.prepare(
                "p",
                "owner",
                source_receipt="c" * 64,
                private_input_id=None,
                private_input_receipt=None,
            ),
        )

    def test_owner_and_payload_revalidated(self):
        with self.assertRaises(ValueError):
            self.builder.prepare("p", "other", **self.args)
        self.db.execute("UPDATE private_input_bytes SET payload=?", (b"tampered",))
        self.db.commit()
        with self.assertRaises(ValueError):
            self.builder.prepare("p", "owner", **self.args)

    def test_commit_mismatch_and_missing_adapter_fail_closed(self):
        with patch.object(
            self.builder, "_project", return_value=(self.project, {"commit": "d" * 40})
        ):
            with self.assertRaisesRegex(ValueError, "commit_not_allowed"):
                self.builder.prepare("p", "owner", **self.args)
        self.builder.private_store_factory = None
        with self.assertRaisesRegex(ValueError, "registration_unavailable"):
            self.builder.prepare("p", "owner", **self.args)

    def test_source_receipt_and_registration_bind_identity(self):
        one = self.builder.prepare("p", "owner", **self.args)
        two = self.builder.prepare("p", "owner", **(self.args | {"source_receipt": "d" * 64}))
        self.assertNotEqual(one["fingerprint"], two["fingerprint"])
        changed = self.reg.model_copy(update={"recipe_sha256": "e" * 64})
        self.project.private_inputs["fixture"] = changed
        with self.assertRaises(ValueError):
            self.builder.prepare("p", "owner", **self.args)

    def test_half_binding_and_no_source_rejected(self):
        for args in ({"private_input_id": "fixture"}, {"private_input_receipt": self.receipt}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.builder.prepare("p", "owner", **args)
        with self.assertRaises(ValueError):
            plan_kwargs({"private_input_id": "fixture"})

    def test_execution_never_falls_back_to_public_only_build(self):
        plan = self.builder.prepare("p", "owner", **self.args)
        with patch.object(self.builder, "_execute_inner") as run:
            with self.assertRaisesRegex(ValueError, "workspace_unavailable"):
                self.builder._execute(plan, lambda _: None)
            run.assert_not_called()
        self.assertEqual(
            plan_kwargs(plan),
            {"private_input_id": "fixture", "private_input_receipt": self.receipt},
        )
