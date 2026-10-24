"""Compact full-hash output slots, legacy locators and no-delete storage checks."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cardputer_mcp.build_locations import (
    BuildLocationError,
    build_directory,
    build_location,
    build_slot,
    claim_build_directory,
    resolve_build_manifest,
)


class BuildLocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Registered roots are pinned to their resolved path (Windows temp may use 8.3 names).
        self.root = Path(self.temporary.name).resolve()
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

    def test_existing_unknown_directory_is_not_adopted_or_removed(self):
        path = build_directory(self.state, self.output, self.fp, self.env)
        path.mkdir(parents=True)
        (path / "user-data.txt").write_text("preserve")
        with self.assertRaisesRegex(BuildLocationError, "build_output_identity_mismatch"):
            claim_build_directory(self.state, self.output, self.fp, self.env)
        self.assertEqual("preserve", (path / "user-data.txt").read_text())

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
