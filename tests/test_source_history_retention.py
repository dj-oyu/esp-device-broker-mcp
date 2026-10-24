"""Real cache/SQLite/binary lifecycle with synthetic compiler and ROM driver.

No SDK, repository download, installed cache or physical device is accessed.
"""

import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from test_flash_lifecycle import FakeDriver
from test_source_lifecycle import CacheFixture

from cardputer_mcp.build_artifacts import BuildArtifacts
from cardputer_mcp.flash_lifecycle import FlashLifecycle, OperationPlan
from cardputer_mcp.flash_lkg_bundle import bundle_from_record
from cardputer_mcp.flash_lkg_store import FirmwareSnapshots
from cardputer_mcp.flash_manifest import FlashBundle, FlashSegment
from cardputer_mcp.source_payloads import _reclaim
from cardputer_mcp.source_resources import resource_report
from cardputer_mcp.source_snapshot import SourceError, SourceSnapshots, canonical, digest
from cardputer_mcp.store import Store


class HistoryFixture(CacheFixture):
    def setup_history(self):
        self.setup_cache()
        self.artifacts = BuildArtifacts(self.root / "state.sqlite3")
        self.binaries = FirmwareSnapshots(
            self.root / "firmware", windows_private_acl_confirmed=True
        )

    def publish_generation(self, number, principal="owner"):
        source = self.fetch(number, principal=principal)
        # The compiler is synthetic; publication, immutable references, ROM
        # lifecycle checks and atomic LKG/history commit are production code.
        self.snapshots.resolve("fixture", source["source_receipt"], principal)
        build_job = f"build-{number}"
        fingerprint = digest(canonical(source))
        key = self.add(
            {
                "job_id": build_job,
                "test": "firmware_build",
                "project_id": "fixture",
                "build_principal": principal,
                "source_receipt": source["source_receipt"],
                "fingerprint": fingerprint,
            },
            "running",
        )
        root = self.root / "build" / str(number)
        root.mkdir(parents=True)
        image = f"synthetic firmware {number}".encode()
        (root / "app.bin").write_bytes(image)
        manifest = {
            "flash_authorized": False,
            "build_job_id": build_job,
            "build_plan_hash": key,
            "fingerprint": fingerprint,
            "environment_fingerprint": "e" * 64,
        }
        (root / "broker-manifest.json").write_bytes(canonical(manifest))
        self.artifacts.publish(build_job, principal, root, manifest)
        self.store.set_job(build_job, "succeeded")

        flash_job = f"flash-{number}"
        self.add(
            {
                "job_id": flash_job,
                "test": "firmware_flash",
                "flash_principal": principal,
                "build_job_id": build_job,
            },
            "running",
        )
        driver = FakeDriver()
        backup = bytes(driver.data)
        bundle = FlashBundle(
            digest(canonical(manifest)),
            "b" * 64,
            (FlashSegment(4096, "app.bin", len(image), digest(image)),),
            "dio",
            "8MB",
            "80m",
        )
        plan = OperationPlan(
            "flash",
            flash_job,
            principal,
            "epoch",
            "session",
            "f" * 64,
            len(backup),
            "a" * 64,
            digest(backup),
            bundle.manifest_sha256,
            "c" * 64,
        )
        engine = FlashLifecycle(driver)
        _, _, summary = engine.run(
            plan,
            plan.digest,
            live=lambda: True,
            reader_closed=lambda: True,
            stage=lambda _: None,
            bundle=bundle,
            images=(image,),
            backup=backup,
        )
        self.assertTrue(summary["readback_verified"])
        self.assertTrue(driver.closed)
        metadata = {
            "kind": "artifact_bundle",
            "device_id": "fixture",
            "principal": principal,
            "selector_sha256": digest(b"synthetic selector"),
            "flash_bytes": len(backup),
            "expected_device_sha256": "a" * 64,
            "expected_app_bin_sha256": digest(image),
            "expected_elf_sha256": "c" * 64,
            "bundle": asdict(bundle),
        }
        receipt = self.binaries.stage(metadata, {"app.bin": image})
        self.store.finish_firmware(
            flash_job,
            "synthetic compiler and ROM completion",
            {
                "receipt": receipt,
                "device_id": "fixture",
                "principal": principal,
                "kind": "artifact_bundle",
                "baseline": "synthetic",
            },
            lambda: driver.closed and not engine.active and not engine.uncertain,
            summary,
            principal,
        )
        return source, receipt, image


class SourceHistoryRetentionTests(HistoryFixture, unittest.TestCase):
    def setUp(self):
        self.setup_history()

    def test_nineteen_fetch_build_flash_generations_preserve_evidence_and_binaries(self):
        sources, receipts, binaries, histories, references = [], {}, {}, {}, {}
        for number in range(1, 20):
            source, receipt, image = self.publish_generation(number)
            sources.append(source)
            receipts[source["source_receipt"]] = (
                self.payload(source).parent / "receipt.json"
            ).read_bytes()
            binaries[receipt] = image
            histories[receipt] = self.store.firmware_known("fixture", "owner", receipt)
            references[number] = self.artifacts.reference(f"build-{number}", "owner", "fixture")
            self.assertLessEqual(self.snapshots.usage("fixture")["snapshots"], 16)
            if number == 9:
                self.store.db.close()
                self.store = Store(self.root / "state.sqlite3")
                self.addCleanup(self.store.db.close)
                self.snapshots = SourceSnapshots(self.settings)
                self.snapshots.retention = self.evidence
                self.artifacts = BuildArtifacts(self.root / "state.sqlite3")

        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 16)
        self.assertEqual(self.snapshots.usage("fixture")["evidence_snapshots"], 19)
        self.assertEqual(self.store.firmware_current("fixture", "owner")["receipt"], receipt)
        self.assertEqual(len(self.evidence()["provenance"]), 19)
        self.assertEqual(
            self.evidence()["protected"],
            {sources[-1]["source_receipt"]: ["current_lkg_evidence"]},
        )
        for source_receipt, raw in receipts.items():
            self.assertEqual(
                (self.root / "cache" / source_receipt / "receipt.json").read_bytes(), raw
            )
        for receipt, image in binaries.items():
            self.assertEqual(
                self.store.firmware_known("fixture", "owner", receipt), histories[receipt]
            )
            metadata, files = self.binaries.load(receipt)
            bundle = bundle_from_record(metadata["bundle"])
            self.assertEqual(files[bundle.segments[0].name], image)
            self.assertEqual(digest(image), bundle.segments[0].sha256)
        for number, reference in references.items():
            self.assertEqual(
                self.artifacts.reference(f"build-{number}", "owner", "fixture"), reference
            )
            self.assertEqual(self.store.job(f"source-{number}")["state"], "succeeded")
            self.assertEqual(self.store.job(f"build-{number}")["state"], "succeeded")
            self.assertEqual(self.store.job(f"flash-{number}")["state"], "succeeded")
        old = next(item for item in sources if not self.payload(item).exists())
        with self.assertRaisesRegex(SourceError, "source_snapshot_evicted_fetch_required"):
            self.snapshots.resolve("fixture", old["source_receipt"], "owner")
        report = resource_report(
            self.store, "fixture", "owner", self.snapshots.inventory("fixture"), epoch="epoch"
        )
        rows = {row["source_receipt"]: row for row in report["owned_snapshots"]}
        self.assertEqual(rows[old["source_receipt"]]["payload_protection_reasons"], [])
        self.assertIn(
            "recovery_history_evidence", rows[old["source_receipt"]]["provenance_reasons"]
        )
        refreshed = self.fetch(int(old["commit"], 16), "refetch-old")
        self.assertEqual(refreshed["source_receipt"], old["source_receipt"])
        self.assertEqual(self.snapshots.usage("fixture")["snapshots"], 16)
        self.assertEqual(self.snapshots.usage("fixture")["evidence_snapshots"], 19)
        self.assertEqual(
            (self.payload(old).parent / "receipt.json").read_bytes(),
            receipts[old["source_receipt"]],
        )

    def test_history_source_still_protected_by_prepared_and_unfinished_jobs(self):
        old, _, _ = self.publish_generation(1)
        self.publish_generation(2)
        for number, state in enumerate((None, "queued", "running", "needs_review", "unknown")):
            with self.subTest(state=state):
                plan = {
                    "job_id": f"active-{number}",
                    "project_id": "fixture",
                    "broker_epoch": "epoch",
                    "source_receipt": old["source_receipt"],
                }
                self.add(plan, state)
                self.assertIn(old["source_receipt"], self.evidence()["protected"])
                with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
                    with self.assertRaisesRegex(SourceError, "source_cache_protected"):
                        self.fetch(3, f"blocked-{number}")
                self.assertTrue(self.payload(old).exists())
                if state is None:
                    self.store.add_job(plan["job_id"], digest(canonical(plan)))
                self.store.set_job(plan["job_id"], "failed")
                self.assertNotIn(old["source_receipt"], self.evidence()["protected"])
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
            self.fetch(3)
        self.assertFalse(self.payload(old).exists())

    def test_other_owner_history_payload_is_reclaimed_but_provenance_is_retained(self):
        old, _, _ = self.publish_generation(1, principal="other")
        self.publish_generation(2)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
            self.fetch(3)
        self.assertFalse(self.payload(old).exists())
        self.assertTrue((self.root / "cache" / old["source_receipt"] / "receipt.json").exists())

    def test_prepared_flash_indirectly_protects_historical_build_source(self):
        old, _, _ = self.publish_generation(1)
        self.publish_generation(2)
        plan = {
            "job_id": "prepared-flash",
            "test": "firmware_flash",
            "flash_principal": "owner",
            "broker_epoch": "epoch",
            "build_job_id": "build-1",
        }
        key = self.add(plan)
        self.assertEqual(
            self.evidence()["protected"][old["source_receipt"]],
            ["executable_firmware_operation"],
        )
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
            with self.assertRaisesRegex(SourceError, "source_cache_protected"):
                self.fetch(3)
        self.assertTrue(self.payload(old).exists())
        self.store.add_job(plan["job_id"], key)
        self.store.set_job(plan["job_id"], "succeeded")
        self.assertNotIn(old["source_receipt"], self.evidence()["protected"])

    def test_malformed_history_lineage_still_disables_reclamation(self):
        old, receipt, _ = self.publish_generation(1)
        self.publish_generation(2)
        for column, value in (
            ("principal", "other"),
            ("completed_job", "missing"),
            ("kind", "unknown"),
        ):
            with self.subTest(column=column):
                self.store.db.execute(
                    f"UPDATE firmware_lkg_history SET {column}=? WHERE receipt=?", (value, receipt)
                )
                self.assertFalse(self.evidence()["complete"])
                with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
                    with self.assertRaisesRegex(SourceError, "source_cache_references_incomplete"):
                        self.fetch(3, f"blocked-{column}")
                self.assertTrue(self.payload(old).exists())
                self.store.db.execute(
                    "UPDATE firmware_lkg_history SET principal='owner',"
                    "completed_job='flash-1',kind='artifact_bundle' WHERE receipt=?",
                    (receipt,),
                )

    def test_new_current_or_prepared_reference_at_final_check_keeps_payload(self):
        old, receipt, _ = self.publish_generation(1)
        self.publish_generation(2)
        value = self.snapshots.resolve("fixture", old["source_receipt"], "owner")[1]
        for change in ("prepared", "current"):
            with self.subTest(change=change):
                calls = 0
                raced_plan = {
                    "job_id": "race",
                    "project_id": "fixture",
                    "broker_epoch": "epoch",
                    "source_receipt": old["source_receipt"],
                }

                def changed(change=change, raced_plan=raced_plan):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        if change == "prepared":
                            self.add(raced_plan)
                        else:
                            self.store.db.execute("UPDATE firmware_lkg SET receipt=?", (receipt,))
                    return self.evidence()

                self.snapshots.retention = changed
                with self.snapshots.cache_guard("fixture"):
                    with self.assertRaisesRegex(SourceError, "source_cache_protected"):
                        _reclaim(
                            self.snapshots, self.plan(3), old["source_receipt"], value, lambda: True
                        )
                self.assertTrue(self.payload(old).exists())
                self.assertEqual(calls, 2)
                if change == "prepared":
                    self.store.db.execute(
                        "DELETE FROM plans WHERE hash=?", (digest(canonical(raced_plan)),)
                    )


class SourceHistoryFlashAdapterTests(HistoryFixture, unittest.TestCase):
    def test_retained_binary_preparation_after_source_eviction(self):
        # Requires the repository's normal frozen dependencies, unlike the
        # stdlib-only lifecycle tests above. No driver is invoked by prepare.
        from cardputer_mcp.firmware_flash import FirmwareFlash
        from cardputer_mcp.models import TestName

        self.setup_history()
        old, receipt, image = self.publish_generation(1)
        with patch.dict("cardputer_mcp.source_snapshot.LIMITS", cache_snapshots=2):
            self.publish_generation(2)
            self.publish_generation(3)
        self.assertFalse(self.payload(old).exists())
        registration = SimpleNamespace(
            backup_directory=self.root,
            private_backup_acl_confirmed=True,
            selector=SimpleNamespace(model_dump_json=lambda **_: "synthetic selector"),
            flash_bytes=8192,
            allow_restore=True,
            model_dump_json=lambda **_: '{"synthetic":true}',
        )
        # Use the same immutable object store location as the production adapter.
        self.binaries.root.rename(self.root / "lkg")
        manager = FirmwareFlash(
            SimpleNamespace(flash_devices={"fixture": registration}),
            SimpleNamespace(device="fixture"),
            None,
            lambda: self.store,
        )
        manager.driver_factory = FakeDriver
        prepared = manager.prepare(
            SimpleNamespace(
                test=TestName.RESTORE,
                job_id="historical",
                device_id="fixture",
                build_job_id=None,
                backup_job_id=None,
                lkg_receipt=receipt,
            ),
            "owner",
            "epoch",
            "session",
        )
        self.assertEqual(prepared["lkg_receipt"], receipt)
        self.assertEqual(prepared["expected_app_bin_sha256"], digest(image))
        self.assertEqual(prepared["recovery_kind"], "artifact_bundle")
