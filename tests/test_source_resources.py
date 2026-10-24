"""Synthetic cache metadata only: no network, retained cache, build or device."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cardputer_mcp import source_snapshot as module
from cardputer_mcp.source_resources import resource_report
from cardputer_mcp.source_snapshot import CacheScanLimit, SourceError, SourceSnapshots


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Registered roots are pinned to their resolved path (Windows temp may use 8.3 names).
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "cache"
        self.root.mkdir(mode=0o700)
        registration = SimpleNamespace(cache_root=self.root, private_acl_confirmed=True)
        project = SimpleNamespace(
            source_repository=registration, project_dir=self.base / "work", build_root=None
        )
        self.snapshots = SourceSnapshots(
            SimpleNamespace(build_projects={"fixture": project}, state_dir=self.base / "state")
        )

    def test_execute_rechecks_capacity_once_and_timeout_publishes_no_receipt(self):
        for i in range(13):
            self.root.joinpath(f"snapshot-{i}").mkdir()
        self.assertEqual(self.snapshots.usage("fixture")["remaining_snapshots"], 3)
        expected = {
            "project_id": "fixture",
            "source_commit": "a" * 40,
            "source_principal": "owner",
        }
        clock = SimpleNamespace(now=0.0)
        original = os.scandir
        scans = []

        def delayed(path):
            scans.append(path)
            if path != self.root:
                clock.now = module.CACHE_SCAN_SECONDS + 1.0
            return original(path)

        with (
            patch.object(module, "time", SimpleNamespace(monotonic=lambda: clock.now)),
            patch.object(os, "scandir", delayed),
            patch.object(self.snapshots, "prepare", return_value=expected),
            patch.object(self.snapshots, "transport") as transport,
        ):
            with self.assertRaises(CacheScanLimit) as caught:
                self.snapshots.fetch(expected, lambda: True, lambda stage: None)
            transport.assert_not_called()
        self.assertEqual(caught.exception.scan["reason"], "deadline")
        self.assertEqual(scans.count(self.root), 1)
        self.assertEqual(len([p for p in self.root.iterdir() if p.is_dir()]), 13)
        self.assertFalse(list(self.root.rglob("receipt.json")))
        self.assertFalse(self.snapshots.uncertain)

    def test_link_and_pending_directory_swap_are_rejected(self):
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "private").write_bytes(b"untouched")
        try:
            (snapshot / "escape").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory links unavailable")
        with self.assertRaisesRegex(SourceError, "source_linked_path_rejected"):
            self.snapshots.inventory("fixture")
        (snapshot / "escape").unlink()
        nested = snapshot / "nested"
        nested.mkdir()
        original = module._not_link

        def swap(path):
            if path == nested:
                nested.rmdir()
                nested.symlink_to(outside, target_is_directory=True)
            original(path)

        with patch.object(module, "_not_link", swap):
            with self.assertRaisesRegex(SourceError, "source_linked_path_rejected"):
                self.snapshots.inventory("fixture")
        self.assertEqual((outside / "private").read_bytes(), b"untouched")


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(
            "CREATE TABLE jobs(id,created,finished,state,diagnostic,plan_hash);"
            "CREATE TABLE plans(hash,payload);"
            "CREATE TABLE firmware_lkg(completed_job,principal,receipt,kind);"
            "CREATE TABLE firmware_lkg_history(completed_job,principal,receipt,kind);"
        )
        self.store = SimpleNamespace(db=db)

    def job(self, name, plan, diagnostic=None):
        plan = {"job_id": name, **plan}
        self.store.db.execute("INSERT INTO plans VALUES(?,?)", (name, json.dumps(plan)))
        self.store.db.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?)",
            (name, "created", "finished", "succeeded", json.dumps(diagnostic or {}), name),
        )

    def test_owned_references_protect_build_lkg_history_without_exposing_other_owner(self):
        receipt = "a" * 64
        self.job(
            "source",
            {"project_id": "fixture", "source_principal": "owner"},
            {"source": {"source_receipt": receipt}},
        )
        self.job(
            "build",
            {"project_id": "fixture", "build_principal": "owner", "source_receipt": receipt},
        )
        self.job("flash", {"flash_principal": "owner", "build_job_id": "build"})
        self.store.db.execute(
            "INSERT INTO firmware_lkg VALUES('restore','owner','bundle','artifact_bundle')"
        )
        self.store.db.execute(
            "INSERT INTO firmware_lkg_history VALUES('flash','owner','bundle','artifact_bundle')"
        )
        inventory = {
            "usage": {"snapshots": 2, "bytes": 100},
            "records": [
                {"name": receipt, "bytes": 40, "entries": 3},
                {"name": "private", "bytes": 60, "entries": 4},
            ],
        }
        result = resource_report(self.store, "fixture", "owner", inventory)
        self.assertFalse(result["cleanup_authorized"])
        self.assertTrue(result["reference_inventory_complete"])
        row = result["owned_snapshots"][0]
        self.assertFalse(row["cleanup_eligible"])
        self.assertIn("current_lkg_evidence", row["protection_reasons"])
        self.assertIn("recovery_history_evidence", row["protection_reasons"])
        self.assertEqual(result["other_or_unclassified_snapshots"], 1)
        self.assertNotIn("private", json.dumps(result))
        other = resource_report(self.store, "fixture", "different-owner", inventory)
        self.assertEqual(other["owned_snapshots"], [])
        self.assertNotIn(receipt, json.dumps(other))

    def test_unresolved_lkg_is_incomplete_but_other_project_and_backup_are_not(self):
        db = self.store.db
        db.execute("INSERT INTO firmware_lkg VALUES('missing','owner','bundle','artifact_bundle')")
        inventory = {"usage": {"snapshots": 0}, "records": []}
        result = resource_report(self.store, "fixture", "owner", inventory)
        self.assertFalse(result["reference_inventory_complete"])
        self.assertEqual(result["unresolved_reference_count"], 1)
        self.assertNotIn("missing", json.dumps(result))
        self.job("missing", {"flash_principal": "owner", "build_job_id": "other-build"})
        self.job(
            "other-build",
            {"project_id": "other", "build_principal": "owner", "source_receipt": "b" * 64},
        )
        result = resource_report(self.store, "fixture", "owner", inventory)
        self.assertTrue(result["reference_inventory_complete"])
        self.assertEqual(result["unresolved_reference_count"], 0)
        db.execute("INSERT INTO firmware_lkg VALUES('backup','owner','original','full_backup')")
        result = resource_report(self.store, "fixture", "owner", inventory)
        self.assertTrue(result["reference_inventory_complete"])

    def test_truncated_reference_inventory_never_authorizes_cleanup(self):
        self.job("source", {"source_principal": "owner"})
        with patch("cardputer_mcp.source_resources.REFERENCE_LIMIT", 0):
            result = resource_report(
                self.store, "fixture", "owner", {"usage": {"snapshots": 0}, "records": []}
            )
        self.assertFalse(result["reference_inventory_complete"])
        self.assertFalse(result["cleanup_authorized"])


if __name__ == "__main__":
    unittest.main()
