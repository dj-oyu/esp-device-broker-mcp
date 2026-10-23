"""Compact full-hash output slots, legacy locators and no-delete storage checks."""

import json
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

from cardputer_mcp.build_locations import (
    BuildLocationError,
    build_directory,
    build_location,
    build_slot,
    check_storage_before_write,
    check_windows_path,
    claim_build_directory,
    resolve_build_manifest,
    storage_snapshot,
)


class BuildLocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.output = self.root / "short"
        self.fp = "a" * 64
        self.env = "b" * 64

    def manifest(self, compact):
        value = {"schema_version": 1, "fingerprint": self.fp, "environment_fingerprint": self.env}
        if compact:
            value["build_location"] = build_location(self.fp, self.env, self.output)
        return value

    def test_full_digest_and_matching_reuse(self):
        slot = build_slot(self.fp, self.env)
        self.assertEqual(64, len(slot))
        self.assertNotEqual(slot, self.fp[:32] + self.env[:32])
        path = claim_build_directory(self.state, self.output, self.fp, self.env)
        self.assertEqual(path.name, slot)
        self.assertEqual(path.parent, self.output.resolve())
        self.assertEqual(path, claim_build_directory(self.state, self.output, self.fp, self.env))
        (path / "broker-manifest.json").write_text(json.dumps(self.manifest(True)))
        self.assertEqual(path, resolve_build_manifest(self.state, self.output, self.manifest(True)))

    def test_different_identity_and_forced_collision(self):
        self.assertNotEqual(build_slot(self.fp, self.env), build_slot("c" * 64, self.env))
        with patch("cardputer_mcp.build_locations.build_slot", return_value="d" * 64):
            claim_build_directory(self.state, self.output, self.fp, self.env)
            with self.assertRaisesRegex(BuildLocationError, "build_output_identity_mismatch"):
                claim_build_directory(self.state, self.output, "c" * 64, self.env)

    def test_existing_unknown_directory_is_not_adopted_or_removed(self):
        path = build_directory(self.state, self.output, self.fp, self.env)
        path.mkdir(parents=True)
        (path / "user-data.txt").write_text("preserve")
        with self.assertRaisesRegex(BuildLocationError, "build_output_identity_mismatch"):
            claim_build_directory(self.state, self.output, self.fp, self.env)
        self.assertEqual("preserve", (path / "user-data.txt").read_text())

    def test_legacy_v1_locator_read_compatibility(self):
        path = self.state / "firmware-builds" / self.fp / self.env
        path.mkdir(parents=True)
        manifest = self.manifest(False)
        (path / "broker-manifest.json").write_text(json.dumps(manifest))
        self.assertEqual(path.resolve(), resolve_build_manifest(self.state, self.output, manifest))
        self.assertFalse((path / "broker-build-identity.json").exists())

    def test_compact_never_falls_back_to_legacy(self):
        path = self.state / "firmware-builds" / self.fp / self.env
        path.mkdir(parents=True)
        (path / "broker-manifest.json").write_text(json.dumps(self.manifest(False)))
        with self.assertRaises(BuildLocationError):
            resolve_build_manifest(self.state, self.output, self.manifest(True))

    def test_long_windows_path_fixed_diagnostic(self):
        check_windows_path(PureWindowsPath("C:/b") / ("a" * 64), windows=True)
        with self.assertRaisesRegex(BuildLocationError, "build_output_path_too_long"):
            check_windows_path(PureWindowsPath("C:/" + "x" * 170) / ("a" * 64), windows=True)

    def test_read_only_owned_inventory_and_no_delete(self):
        path = claim_build_directory(self.state, self.output, self.fp, self.env)
        (path / "artifact.bin").write_bytes(b"payload")
        (self.output / "user-data.txt").write_text("preserve")
        report = storage_snapshot(self.state, self.output)
        self.assertEqual(1, report["owned_slots"])
        self.assertGreaterEqual(report["owned_bytes"], 7)
        self.assertFalse(report["cleanup_enabled"])
        self.assertEqual("preserve", (self.output / "user-data.txt").read_text())

    def test_compact_requires_matching_persisted_manifest(self):
        path = claim_build_directory(self.state, self.output, self.fp, self.env)
        manifest = self.manifest(True)
        with self.assertRaises(BuildLocationError):
            resolve_build_manifest(self.state, self.output, manifest)
        (path / "broker-manifest.json").write_text(json.dumps(manifest))
        changed = {**manifest, "build_job_id": "unattested-job"}
        with self.assertRaises(BuildLocationError):
            resolve_build_manifest(self.state, self.output, changed)
        with self.assertRaises(BuildLocationError):
            resolve_build_manifest(self.state, self.root / "other", manifest)

    def test_linked_root_is_pinned_to_its_target(self):
        real = self.root / "real"
        real.mkdir()
        linked = self.root / "linked"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        for output in (linked, linked / "child"):
            path = claim_build_directory(self.state, output, self.fp, self.env)
            self.assertTrue(path.is_relative_to(real.resolve()))
        self.assertEqual(2, len(list(real.rglob("broker-build-identity.json"))))

    def test_link_inside_root_is_still_rejected(self):
        real = self.root / "real"
        outside = self.root / "outside"
        real.mkdir()
        outside.mkdir()
        slot = real / build_slot(self.fp, self.env)
        try:
            slot.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaisesRegex(BuildLocationError, "build_output_linked"):
            claim_build_directory(self.state, real, self.fp, self.env)
        self.assertEqual([], list(outside.iterdir()))

    def test_filesystem_root_is_rejected(self):
        with self.assertRaisesRegex(BuildLocationError, "build_output_root_invalid"):
            claim_build_directory(self.state, Path(self.root.anchor), self.fp, self.env)

    def test_usage_inventory_marks_directory_budget_incomplete(self):
        path = claim_build_directory(self.state, self.output, self.fp, self.env)
        for index in range(4):
            (path / str(index)).mkdir()
        report = storage_snapshot(self.state, self.output, max_files=2)
        self.assertFalse(report["usage_complete"])
        self.assertTrue(all((path / str(index)).is_dir() for index in range(4)))

    def test_reparse_root_marker_is_rejected(self):
        from cardputer_mcp.build_locations import output_base

        self.output.mkdir()
        original = Path.lstat

        def reparse(path):
            value = original(path)
            if path == self.output:
                return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=0x400)
            return value

        with patch.object(Path, "lstat", reparse):
            with self.assertRaisesRegex(BuildLocationError, "build_output_linked"):
                output_base(self.state, self.output)

    def test_walk_error_marks_inventory_incomplete(self):
        claim_build_directory(self.state, self.output, self.fp, self.env)

        def failed_walk(path, *, followlinks, onerror):
            onerror(PermissionError("synthetic"))
            return iter(())

        with patch("cardputer_mcp.build_locations.os.walk", failed_walk):
            report = storage_snapshot(self.state, self.output)
        self.assertFalse(report["usage_complete"])
        self.assertFalse(report["cleanup_enabled"])

    def test_native_slot_lock_refuses_concurrent_writer(self):
        try:
            from filelock import FileLock, Timeout
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        from cardputer_mcp.build_locations import slot_lock_path

        path = slot_lock_path(self.state, self.output, self.fp, self.env)
        path.parent.mkdir(parents=True)
        first, second = FileLock(str(path), timeout=0), FileLock(str(path), timeout=0)
        with first:
            with self.assertRaises(Timeout):
                second.acquire()
        with second:
            self.assertTrue(second.is_locked)

    def test_insufficient_space_before_writes(self):
        with patch(
            "cardputer_mcp.build_locations.shutil.disk_usage",
            return_value=SimpleNamespace(free=1, total=100),
        ):
            with self.assertRaisesRegex(BuildLocationError, "build_output_insufficient_space"):
                check_storage_before_write(self.state, self.output, 2)
        self.assertFalse(self.output.exists())


class BuildSlotOwnershipTests(unittest.TestCase):
    def test_uncertain_process_keeps_slot_lock(self):
        from cardputer_mcp.firmware_build import FirmwareBuild

        builder = FirmwareBuild(SimpleNamespace())
        lock = SimpleNamespace(released=False, is_locked=True)

        def release():
            lock.released = True
            lock.is_locked = False

        lock.release = release
        builder.process.uncertain = True

        def fail(*_):
            builder._build_lock = lock
            raise ValueError("synthetic")

        with patch.object(builder, "_execute_inner", side_effect=fail):
            with self.assertRaises(ValueError):
                builder._execute({}, lambda _: None)
        self.assertFalse(lock.released)
        self.assertIs(builder._build_lock, lock)

    def test_confirmed_stop_releases_slot_after_failure(self):
        from cardputer_mcp.firmware_build import FirmwareBuild

        builder = FirmwareBuild(SimpleNamespace())
        lock = SimpleNamespace(released=False, is_locked=True)

        def release():
            lock.released = True
            lock.is_locked = False

        lock.release = release

        def fail(*_):
            builder._build_lock = lock
            raise ValueError("synthetic")

        with patch.object(builder, "_execute_inner", side_effect=fail):
            with self.assertRaises(ValueError):
                builder._execute({}, lambda _: None)
        self.assertTrue(lock.released)
        self.assertIsNone(builder._build_lock)
