"""Durable build references remain inert and revalidate all local bytes."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from cardputer_mcp.build_locations import (
    build_directory,
    build_location,
    claim_build_directory,
    identity_receipt,
)
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.store import Store


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DurableBuildArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.history = self.state / "history.sqlite3"
        self.store = Store(self.history)
        self.addCleanup(lambda: self.store.close())
        self.project = SimpleNamespace(build_root=self.root / "output", target="esp32s3")
        self.settings = SimpleNamespace(
            state_dir=self.state, build_projects={"fixture": self.project}
        )
        self.builder = FirmwareBuild(self.settings, self.history)

    def publish(self, job="build-0", *, finish=True, publish=True, worker=False, elf_bytes=None):
        fingerprint = hashlib.sha256(job.encode()).hexdigest()
        environment = "e" * 64
        plan = dict(
            job_id=job,
            test="firmware_build",
            build_principal="alice",
            project_id="fixture",
            fingerprint=fingerprint,
        )
        plan_hash = digest(plan)
        self.store.put_plan(plan_hash, plan)
        self.store.add_job(job, plan_hash)
        self.store.set_job(job, "running")
        if self.project.build_root is None:
            # Reconstruct an existing legacy slot; current Windows path limits
            # apply to new builds, not read-only resolution of old receipts.
            root = build_directory(self.state, None, fingerprint, environment)
            root.mkdir(parents=True)
            (root / "broker-build-identity.json").write_text(
                json.dumps(identity_receipt(self.state, None, fingerprint, environment))
            )
        else:
            root = claim_build_directory(
                self.state, self.project.build_root, fingerprint, environment
            )

        def receipt(name, data):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return dict(name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())

        artifacts = [
            receipt("app.bin", b"firmware"),
            receipt("app.elf", b"ELF-fixture" if elf_bytes is None else elf_bytes),
        ]
        manifest = dict(
            schema_version=1,
            build_job_id=job,
            build_plan_hash=plan_hash,
            idf_version="6.0.1",
            target="esp32s3",
            fingerprint=fingerprint,
            environment_fingerprint=environment,
            artifacts=artifacts,
            flash_layout=receipt("flasher_args.json", b'{"fixture":"layout"}'),
            flash_security=receipt("config/sdkconfig.json", b'{"fixture":"security"}'),
            flash_authorized=False,
            build_location=build_location(fingerprint, environment, self.project.build_root),
        )
        (root / "broker-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        if publish:
            if worker:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(
                        self.builder.artifacts.publish, job, "alice", root, manifest
                    ).result()
            else:
                self.builder.artifacts.publish(job, "alice", root, manifest)
        if finish:
            self.store.set_job(job, "succeeded", "firmware_build_succeeded_no_flash")
        return root, manifest

    def context(self, job="build-0", owner="alice"):
        return self.builder.resolve_flash_manifest(job, owner, "fixture")

    def restart(self):
        self.store.close()
        self.store = Store(self.history)
        self.store.recover()
        self.builder = FirmwareBuild(self.settings, self.history)

    def test_worker_publication_and_more_than_eight_builds_survive_restart(self):
        root, manifest = self.publish(worker=True)
        for index in range(1, 10):
            self.publish(f"build-{index}")
        self.assertNotIn("build-0", self.builder.artifacts._records)
        self.assertEqual((root, manifest), self.context())
        self.restart()
        self.assertFalse(self.builder.artifacts._records)
        self.assertEqual((root, manifest), self.context())
        self.assertEqual(
            10,
            self.store.db.execute("SELECT count(*) FROM build_artifact_references").fetchone()[0],
        )
        self.assertEqual("succeeded", self.store.job("build-0")["state"])

    def test_flash_owned_context_uses_durable_reference_after_restart(self):
        try:
            from cardputer_mcp.firmware_flash import FirmwareFlash
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        root, manifest = self.publish()
        self.restart()
        flash = SimpleNamespace(store=lambda: self.store, build=self.builder, resolver=None)
        self.assertEqual((root, manifest), FirmwareFlash._build_context(flash, "build-0", "alice"))
        with self.assertRaises(ValueError):
            FirmwareFlash._build_context(flash, "build-0", "bob")

    def test_lookup_does_not_create_missing_history_or_follow_relocated_output(self):
        from cardputer_mcp.build_artifacts import BuildArtifacts

        missing = self.root / "absent.sqlite3"
        with self.assertRaises(sqlite3.DatabaseError):
            BuildArtifacts(missing).reference("build-0", "alice", "fixture")
        self.assertFalse(missing.exists())
        root, _manifest = self.publish()
        moved = root.with_name(root.name + "-moved")
        root.rename(moved)
        try:
            root.symlink_to(moved, target_is_directory=True)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        with self.assertRaises(ValueError):
            self.context()

    def test_large_elf_remains_owned_and_hash_verified_after_restart(self):
        data = b"E" * (33 * 1024 * 1024)
        root, manifest = self.publish(elf_bytes=data)
        self.restart()
        self.assertEqual((root, manifest), self.context())
        with self.assertRaises(ValueError):
            self.context(owner="bob")
        with (root / "app.elf").open("r+b") as handle:
            handle.write(b"X")
        with self.assertRaises(ValueError):
            self.context()

    def test_original_default_layout_survives_restart(self):
        self.project.build_root = None
        root, manifest = self.publish()
        self.restart()
        self.assertEqual((root, manifest), self.context())

    def test_no_reference_is_adopted_from_a_success_manifest_on_disk(self):
        self.publish(publish=False)
        self.restart()
        with self.assertRaises(ValueError):
            self.context()

    def test_other_principal_project_and_removed_registration_are_rejected(self):
        self.publish()
        self.restart()
        with self.assertRaises(ValueError):
            self.context(owner="bob")
        with self.assertRaises(ValueError):
            self.builder.resolve_flash_manifest("build-0", "alice", "other")
        self.settings.build_projects.clear()
        with self.assertRaises(ValueError):
            self.context()

    def test_registered_output_root_or_target_change_is_rejected(self):
        self.publish()
        self.project.target = "esp32"
        with self.assertRaises(ValueError):
            self.context()
        self.project.target = "esp32s3"
        self.project.build_root = self.root / "other-output"
        with self.assertRaises(ValueError):
            self.context()
        # A compact receipt never falls back to the old default output layout.
        self.project.build_root = None
        with self.assertRaises(ValueError):
            self.context()

    def test_manifest_and_each_artifact_are_rehashed_after_restart(self):
        root, manifest = self.publish()
        self.restart()
        path = root / "broker-manifest.json"
        original = path.read_bytes()
        changed = dict(manifest, target="esp32")
        path.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            self.context()
        path.write_bytes(original)
        for receipt in [
            *manifest["artifacts"],
            manifest["flash_layout"],
            manifest["flash_security"],
        ]:
            with self.subTest(name=receipt["name"]):
                path = root / receipt["name"]
                data = path.read_bytes()
                path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
                with self.assertRaises(ValueError):
                    self.context()
                path.write_bytes(data)
        self.assertEqual((root, manifest), self.context())

    def test_missing_and_linked_artifacts_are_rejected_without_adoption(self):
        root, manifest = self.publish()
        self.restart()
        path = root / "app.bin"
        data = path.read_bytes()
        path.unlink()
        with self.assertRaises(ValueError):
            self.context()
        outside = self.root / "outside.bin"
        outside.write_bytes(data)
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        with self.assertRaises(ValueError):
            self.context()
        self.assertEqual(data, outside.read_bytes())

    def test_incomplete_cancelled_and_reviewed_builds_cannot_resolve(self):
        for state in ("running", "cancelled", "needs_review", "failed"):
            with self.subTest(state=state):
                job = "build-" + state
                self.publish(job, finish=False)
                self.store.set_job(job, state)
                with self.assertRaises(ValueError):
                    self.context(job)
        self.restart()
        with self.assertRaises(ValueError):
            self.context("build-running")

    def test_reference_is_bound_to_successful_job_plan_and_owner(self):
        self.publish()
        self.restart()
        self.store.db.execute("UPDATE build_artifact_references SET principal='bob'")
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.context()
        self.store.db.execute("UPDATE build_artifact_references SET principal='alice'")
        self.store.db.execute(
            "UPDATE plans SET payload=json_set(payload,'$.fingerprint',?)", ("f" * 64,)
        )
        self.store.db.commit()
        with self.assertRaises(ValueError):
            self.context()

    def test_reference_write_failure_cannot_publish_a_success_reference(self):
        self.store.db.execute(
            "CREATE TRIGGER reject_reference BEFORE INSERT ON build_artifact_references "
            "BEGIN SELECT RAISE(ABORT,'synthetic storage refusal'); END"
        )
        self.store.db.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            self.publish(finish=False)
        self.assertFalse(self.builder.artifacts._records)
        self.assertEqual(
            0, self.store.db.execute("SELECT count(*) FROM build_artifact_references").fetchone()[0]
        )
        self.restart()
        self.assertEqual("needs_review", self.store.job("build-0")["state"])
        with self.assertRaises(ValueError):
            self.context()

    def test_references_contain_only_locators_and_digests_without_absolute_paths(self):
        self.publish()
        payload = self.store.db.execute("SELECT payload FROM build_artifact_references").fetchone()[
            0
        ]
        self.assertLess(len(payload.encode()), 4096)
        self.assertNotIn(str(self.root), payload)
        self.assertNotIn("artifacts", json.loads(payload))
        self.assertFalse(json.loads(payload)["build_location"].get("flash_authorized", False))


if __name__ == "__main__":
    unittest.main()
