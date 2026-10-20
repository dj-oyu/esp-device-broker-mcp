"""Isolated real-filesystem/SQLite cache lifecycle, without SDK, USB or downloads."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cardputer_mcp.firmware_source import FirmwareSource
from cardputer_mcp.source_payloads import _reclaim
from cardputer_mcp.source_resources import resource_report
from cardputer_mcp.source_retention import retention_evidence
from cardputer_mcp.source_snapshot import (
    SourceError,
    SourceSnapshots,
    canonical,
    digest,
    file_receipt,
    tree_hash,
)
from cardputer_mcp.store import Store


class CacheFixture:
    def setup_cache(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        registration = SimpleNamespace(
            cache_root=self.root / "cache", github_repository="fixture/project",
            private_acl_confirmed=True, submodule_repositories=[],
            model_dump_json=lambda **_: '{"repository":"fixture/project"}',
        )
        self.settings = SimpleNamespace(
            state_dir=self.root / "state",
            build_projects={"fixture": SimpleNamespace(
                project_dir=self.root / "worktree", build_root=self.root / "build",
                source_repository=registration,
            )},
        )
        self.store = Store(self.root / "state.sqlite3")
        self.addCleanup(self.store.db.close)
        self.snapshots = SourceSnapshots(self.settings)
        self.snapshots.retention = self.evidence
        self.downloads = 0

        def fetch(archives, repository, commit, destination, **kwargs):
            self.downloads += 1
            destination.mkdir(mode=0o700)
            data = ("project " + commit).encode()
            (destination / "CMakeLists.txt").write_bytes(data)
            files = {"CMakeLists.txt": file_receipt(data, "100644")}
            return files, {"tree_sha1": tree_hash(files), "archive_sha256": digest(data),
                           "source_sha256": digest(canonical(files))}

        self.archives = patch("cardputer_mcp.source_snapshot.RepositoryArchives.fetch", fetch)
        self.archives.start()
        self.addCleanup(self.archives.stop)

    def evidence(self):
        return retention_evidence(self.store, "fixture", "owner", "epoch")

    def plan(self, number, job=None, principal="owner"):
        return self.snapshots.prepare("fixture", f"{number:040x}", principal) | {
            "job_id": job or f"source-{number}", "test": "firmware_fetch_source",
            "broker_epoch": "epoch",
        }

    def add(self, plan, state=None, diagnostic=None):
        key = digest(canonical(plan))
        self.store.put_plan(key, plan)
        if state is not None:
            self.store.add_job(plan["job_id"], key)
            self.store.set_job(plan["job_id"], state, diagnostic=diagnostic)
        return key

    def fetch(self, number, job=None, principal="owner", commit=True, live=lambda: True):
        plan = self.plan(number, job, principal)
        self.add(plan, "running")
        result = self.snapshots.fetch(plan, live, lambda _: None)
        if commit:
            self.store.set_job(plan["job_id"], "succeeded", diagnostic={"source": result})
        return result

    def fill(self, count=16):
        return [self.fetch(number) for number in range(1, count + 1)]

    def payload(self, result):
        return self.root / "cache" / result["source_receipt"] / "source"


class SourceLifecycleTests(CacheFixture, unittest.TestCase):
    def setUp(self):
        self.setup_cache()

    def test_sixteen_full_accepts_three_new_versions_retaining_receipts(self):
        original = self.fill()
        receipts = {item["source_receipt"]: self.payload(item).parent.joinpath("receipt.json").read_bytes()
                    for item in original}
        for number in (17, 18, 19):
            self.fetch(number)
            self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 16)
        usage = self.snapshots.usage("fixture")
        self.assertEqual(usage["evidence_snapshots"], 19)
        self.assertEqual(usage["evicted_snapshots"], 3)
        for receipt, raw in receipts.items():
            self.assertEqual((self.root / "cache" / receipt / "receipt.json").read_bytes(), raw)
        self.assertFalse((self.root / "worktree").exists())
        self.assertFalse((self.root / "build").exists())

    def test_same_commit_reused_when_full_no_download_or_duplicate(self):
        original = self.fill()
        before = self.downloads
        result = self.fetch(1, "repeat")
        self.assertTrue(result["reused"])
        self.assertEqual(result["source_receipt"], original[0]["source_receipt"])
        self.assertEqual(self.downloads, before)
        self.assertEqual(self.snapshots.usage("fixture")["evidence_snapshots"], 16)
        manager = FirmwareSource(self.settings, lambda: self.store)
        manager.authorize_receipt("fixture", result["source_receipt"], "owner")

    def test_explicit_refetch_restores_original_receipt_and_authority(self):
        original = self.fill()
        self.fetch(17)
        old = next(item for item in original if not self.payload(item).exists())
        with self.assertRaisesRegex(SourceError, "source_snapshot_evicted_fetch_required"):
            self.snapshots.resolve("fixture", old["source_receipt"], "owner")
        result = self.fetch(int(old["commit"], 16), "restore-explicit")
        self.assertEqual(result["source_receipt"], old["source_receipt"])
        self.assertEqual(self.snapshots.usage("fixture")["evidence_snapshots"], 17)
        FirmwareSource(self.settings, lambda: self.store).authorize_receipt(
            "fixture", old["source_receipt"], "owner")

    def test_current_unexecuted_legacy_plan_and_active_job_protected_terminal_released(self):
        first = self.fill(3)
        for index, state in ((0, None), (1, "running"), (2, "succeeded")):
            self.add({"job_id": f"build-{index}", "test": "firmware_build",
                      "broker_epoch": "epoch", "project_id": "fixture", "build_principal": "owner",
                      "source_receipt": first[index]["source_receipt"]}, state)
        policy = self.evidence()
        self.assertIn(first[0]["source_receipt"], policy["protected"])
        self.assertIn(first[1]["source_receipt"], policy["protected"])
        self.assertNotIn(first[2]["source_receipt"], policy["protected"])
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=3):
            self.fetch(4)
        self.assertTrue(self.payload(first[0]).exists())
        self.assertTrue(self.payload(first[1]).exists())
        self.assertFalse(self.payload(first[2]).exists())

    def test_expired_plan_old_epoch_release_and_queued_job_keeps_pin(self):
        first = self.fill(3)
        for index in range(3):
            self.add({"job_id": f"plan-{index}", "project_id": "fixture", "test": "firmware_build",
                      "build_principal": "owner", "source_receipt": first[index]["source_receipt"],
                      "broker_epoch": "old" if index == 1 else "epoch",
                      "source_plan_expires_at": time.time() - 1},
                     "queued" if index == 2 else None)
        policy = self.evidence()
        self.assertNotIn(first[0]["source_receipt"], policy["protected"])
        self.assertNotIn(first[1]["source_receipt"], policy["protected"])
        self.assertIn(first[2]["source_receipt"], policy["protected"])

    def test_recovery_history_current_lkg_and_unfinished_flash_protect_source(self):
        first = self.fill(2)
        for index in range(2):
            self.add({"job_id": f"build-{index}", "test": "firmware_build",
                      "project_id": "fixture", "build_principal": "owner",
                      "source_receipt": first[index]["source_receipt"]}, "succeeded")
            self.add({"job_id": f"flash-{index}", "test": "firmware_flash",
                      "flash_principal": "owner", "build_job_id": f"build-{index}"},
                     "succeeded" if index == 0 else "running")
        self.store.db.execute("INSERT INTO firmware_lkg_history VALUES(?,?,?,?,?,?)",
                              ("bundle", "device", "owner", "artifact_bundle", "base", "flash-0"))
        self.store.db.execute("INSERT INTO firmware_lkg VALUES(?,?,?,?,?,?)",
                              ("device", "owner", "bundle", "artifact_bundle", "base", "restored"))
        policy = self.evidence()
        self.assertTrue(policy["complete"])
        self.assertIn("current_lkg_evidence", policy["protected"][first[0]["source_receipt"]])
        self.assertIn("recovery_history_evidence", policy["protected"][first[0]["source_receipt"]])
        self.assertIn("executable_firmware_operation", policy["protected"][first[1]["source_receipt"]])

    def test_no_candidate_explained_and_other_owner_never_reclaimed(self):
        first = self.fetch(1, principal="other")
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            with self.assertRaisesRegex(SourceError, "source_cache_protected"):
                self.fetch(2)
        self.assertTrue(self.payload(first).exists())
        self.assertEqual(self.downloads, 1)

    def test_reference_truncation_unresolved_recovery_fail_closed(self):
        first = self.fetch(1)
        with patch("cardputer_mcp.source_retention.REFERENCE_LIMIT", 0):
            with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
                with self.assertRaisesRegex(SourceError, "source_cache_references_incomplete"):
                    self.fetch(2)
        self.assertTrue(self.payload(first).exists())
        self.store.db.execute("INSERT INTO firmware_lkg VALUES(?,?,?,?,?,?)",
                              ("device", "owner", "bundle", "artifact_bundle", "base", "missing"))
        self.assertFalse(self.evidence()["complete"])

    def test_evidence_quota_and_disk_preflight_do_not_evict(self):
        first = self.fetch(1)
        with patch("cardputer_mcp.source_payloads.EVIDENCE_SNAPSHOTS", 1):
            with self.assertRaisesRegex(SourceError, "source_cache_evidence_full"):
                self.fetch(2)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            with patch("cardputer_mcp.source_payloads.shutil.disk_usage",
                       return_value=SimpleNamespace(free=0)):
                with self.assertRaisesRegex(SourceError, "source_disk_reserve_required"):
                    self.fetch(3)
        self.assertTrue(self.payload(first).exists())

    def test_failed_download_leaves_no_pending_and_old_receipt_is_retained(self):
        first = self.fetch(1)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            with patch("cardputer_mcp.source_snapshot.RepositoryArchives.fetch",
                       side_effect=SourceError("source_download_failed")):
                with self.assertRaisesRegex(SourceError, "source_download_failed"):
                    self.fetch(2)
        self.assertTrue((self.payload(first).parent / "receipt.json").exists())
        self.assertEqual(self.snapshots.usage("fixture")["pending_snapshots"], 0)
        self.fetch(3)

    def test_published_without_durable_success_is_neither_reused_nor_reclaimed(self):
        first = self.fetch(1, commit=False)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            with self.assertRaisesRegex(SourceError, "source_cache_protected"):
                self.fetch(1, "repeat")
        self.assertTrue(self.payload(first).exists())
        with self.assertRaisesRegex(SourceError, "successful_owned_source_fetch_required"):
            FirmwareSource(self.settings, lambda: self.store).authorize_receipt(
                "fixture", first["source_receipt"], "owner")

    def test_restart_recovers_interrupted_reclamation_preserving_receipt(self):
        first = self.fetch(1)
        receipt = first["source_receipt"]
        value = self.snapshots.resolve("fixture", receipt, "owner")[1]
        raw = self.payload(first).parent.joinpath("receipt.json").read_bytes()
        with patch("cardputer_mcp.source_payloads.shutil.rmtree", side_effect=OSError("synthetic")):
            with self.assertRaisesRegex(SourceError, "source_cleanup_unconfirmed"):
                _reclaim(self.snapshots, self.plan(2), receipt, value, lambda: True)
        self.assertTrue(self.snapshots.uncertain)
        self.snapshots = SourceSnapshots(self.settings)
        self.snapshots.retention = self.evidence
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            self.fetch(2)
        self.assertEqual(self.payload(first).parent.joinpath("receipt.json").read_bytes(), raw)
        self.assertFalse((self.payload(first).parent / "reclaiming").exists())

    def test_cancel_before_reclaim_keeps_payload(self):
        first = self.fetch(1)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            with self.assertRaisesRegex(SourceError, "source_fetch_interrupted"):
                self.fetch(2, live=lambda: False)
        self.assertTrue(self.payload(first).exists())

    def test_tampered_payload_and_unknown_entries_are_never_deleted(self):
        first = self.fetch(1)
        (self.payload(first) / "extra").write_text("unknown")
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=1):
            with self.assertRaisesRegex(SourceError, "source_snapshot_changed"):
                self.fetch(2)
        self.assertTrue((self.payload(first) / "extra").exists())

    def test_thread_and_other_process_cannot_enter_owned_cache(self):
        self.fetch(1)
        errors = []
        def contender():
            try:
                SourceSnapshots(self.settings).usage("fixture")
            except SourceError as failure:
                errors.append(failure.code)
        with self.snapshots.cache_guard("fixture"):
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            script = ("from pathlib import Path; import sys; "
                      "from cardputer_mcp.source_cache_lock import CacheLock; "
                      "lock=CacheLock(); "
                      "\ntry:\n with lock.hold(Path(sys.argv[1]),Path(sys.argv[2]),write=False): pass"
                      "\nexcept ValueError as e:\n print(str(e))")
            result = subprocess.run([sys.executable, "-c", script, str(self.root / "cache"),
                                     str(self.settings.state_dir)], capture_output=True, text=True,
                                    timeout=10, env=os.environ.copy())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "source_fetch_busy")
        self.assertEqual(errors, ["source_fetch_busy"])
        self.snapshots.usage("fixture")

    def test_another_state_cannot_reclaim_shared_root(self):
        self.fetch(1)
        other = SimpleNamespace(state_dir=self.root / "other-state",
                                build_projects=self.settings.build_projects)
        with self.assertRaisesRegex(SourceError, "source_cache_owner_conflict"):
            SourceSnapshots(other).usage("fixture")

    def test_report_distinguishes_payload_and_receipt_only(self):
        self.fill(2)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
            self.fetch(3)
        report = resource_report(self.store, "fixture", "owner",
                                 self.snapshots.inventory("fixture"), epoch="epoch")
        self.assertTrue(report["reference_inventory_complete"])
        self.assertEqual(report["other_or_unclassified_snapshots"], 0)
        self.assertEqual(sum(row["cleanup_eligible"] for row in report["owned_snapshots"]), 2)
        self.assertFalse(report["cleanup_authorized"])
        self.assertTrue(report["automatic_payload_reclamation"])


if __name__ == "__main__":
    unittest.main()
