"""Stage 0.5 characterization: persisted flash/build formats that must stay readable.

Covers LKG envelope metadata with `compat` absent vs null, the backup JSON
sidecar, and build manifests without `build_location`. Synthetic files only.
"""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest
import test_flash_policy

from cardputer_mcp.build_locations import (
    BuildLocationError,
    build_directory,
    build_location,
    resolve_build_manifest,
)
from cardputer_mcp.flash_backup import BackupStore
from cardputer_mcp.flash_lifecycle import BackupReceipt, FlashFailure
from cardputer_mcp.flash_lkg_store import FirmwareSnapshots
from cardputer_mcp.models import TestName as Action

# Reuse the real-broker/fake-driver fixture (prior backup + flashed LKG).
env = test_flash_policy.env

# ---------------------------------------------------------------------------
# LKG envelope: `compat` absent vs null
# ---------------------------------------------------------------------------

FULL_BACKUP_METADATA = dict(
    kind="full_backup",
    device_id="fixture",
    principal="owner",
    expected_device_sha256="a" * 64,
    flash_bytes=4194304,
    selector_sha256="b" * 64,
    expected_elf_sha256="c" * 64,
    expected_app_bin_sha256="d" * 64,
    backup_sha256="e" * 64,
    backup_job_id="backup-one",
)


def test_envelope_keeps_compat_absent_distinct_from_null(tmp_path):
    snapshots = FirmwareSnapshots(tmp_path / "lkg", windows_private_acl_confirmed=True)
    absent = snapshots.stage(dict(FULL_BACKUP_METADATA), {})
    null = snapshots.stage(FULL_BACKUP_METADATA | {"compat": None}, {})
    assert absent != null
    absent_metadata, absent_files = snapshots.load(absent)
    null_metadata, null_files = snapshots.load(null)
    assert "compat" not in absent_metadata
    assert "compat" in null_metadata and null_metadata["compat"] is None
    assert absent_files == null_files == {}
    # Readers use .get("compat"): both shapes mean "no layout evidence".
    assert absent_metadata.get("compat") is None and null_metadata.get("compat") is None
    # Canonical compact envelope bytes; the receipt is their sha256.
    raw = (snapshots.root / null).read_bytes()
    expected = {"files": {}, "metadata": FULL_BACKUP_METADATA | {"compat": None}}
    expected["schema_version"] = 1
    assert raw == json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(raw).hexdigest() == null


@pytest.mark.parametrize("envelope", [{"schema_version": 2}, {"extra": 1}])
def test_envelope_schema_is_strict(tmp_path, envelope):
    snapshots = FirmwareSnapshots(tmp_path / "lkg", windows_private_acl_confirmed=True)
    body = {"schema_version": 1, "metadata": FULL_BACKUP_METADATA, "files": {}} | envelope
    receipt = snapshots.put(json.dumps(body, sort_keys=True).encode())
    with pytest.raises(FlashFailure, match="^lkg_snapshot_missing_or_changed$"):
        snapshots.load(receipt)


@pytest.mark.parametrize("compat", ["null", "absent"])
async def test_full_backup_lkg_with_compat_absent_or_null_is_a_flash_baseline(env, compat):
    root = env.broker.settings.flash_devices["fixture"].backup_directory / "lkg"
    snapshots = FirmwareSnapshots(root, windows_private_acl_confirmed=True)
    restored = env.prepare("restore-full", Action.RESTORE, backup_job_id="backup-one")
    env.execute(restored)
    await env.broker.queue.join()
    assert env.broker.job("restore-full")["state"] == "succeeded"
    current = env.store.firmware_current("fixture")
    assert (current["kind"], current["completed_job"]) == ("full_backup", "restore-full")
    metadata, files = snapshots.load(current["receipt"])
    # A restore-written full_backup snapshot carries an explicit null.
    assert "compat" in metadata and metadata["compat"] is None and files == {}
    receipt = current["receipt"]
    if compat == "absent":
        # Shape written by the (fallback) flash-time full_backup branch.
        legacy = {key: value for key, value in metadata.items() if key != "compat"}
        receipt = snapshots.stage(legacy, {})
        env.store.db.execute(
            "INSERT INTO firmware_lkg_history VALUES (?,?,?,?,?,?)",
            (receipt, "fixture", "alice", "full_backup", current["baseline"], "restore-full"),
        )
        env.store.db.execute(
            "UPDATE firmware_lkg SET receipt=? WHERE device_id=?", (receipt, "fixture")
        )
        env.store.db.commit()
    seq = env.store.device_write_seq()
    prepared = env.prepare("after-full", build_job_id="build-one")
    plan = prepared["plan"]
    assert (plan["lkg_receipt"], plan["recovery_kind"]) == (receipt, "full_backup")
    assert plan["backup_sha256"] == metadata["backup_sha256"]
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("after-full")["state"] == "succeeded", env.broker.job("after-full")
    after = env.store.firmware_current("fixture")
    assert (after["kind"], after["completed_job"]) == ("artifact_bundle", "after-full")
    assert after["baseline"] == current["baseline"]
    assert env.store.device_write_seq() == seq + 1


# ---------------------------------------------------------------------------
# Backup sidecar `<job>.json`
# ---------------------------------------------------------------------------

DATA = b"synthetic-backup-data"
RECEIPT = BackupReceipt("a" * 64, "b" * 64, len(DATA), hashlib.sha256(DATA).hexdigest())


@pytest.fixture
def backups(tmp_path):
    store = BackupStore(tmp_path / "private", windows_private_acl_confirmed=True)
    retained = store.save("backup-job", "owner", RECEIPT, DATA)
    return store, retained


def test_sidecar_bytes_are_sorted_default_separator_json(backups):
    store, retained = backups
    raw = (store.directory / "backup-job.json").read_bytes()
    assert raw == json.dumps(asdict(retained), sort_keys=True).encode()
    assert list(json.loads(raw)) == [
        "device_fingerprint",
        "device_sha256",
        "flash_bytes",
        "job_id",
        "principal",
        "sha256",
    ]
    assert b"schema_version" not in raw


def test_sidecar_reader_accepts_any_json_layout_of_the_same_fields(backups):
    store, retained = backups
    path = store.directory / "backup-job.json"
    reordered = dict(reversed(list(asdict(retained).items())))
    path.write_text(json.dumps(reordered, separators=(",", ":")), "utf-8")
    assert store.load("backup-job", "owner") == (retained, DATA)
    path.write_text(json.dumps(asdict(retained), indent=2), "utf-8")
    assert store.load("backup-job", "owner") == (retained, DATA)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"schema_version": 1}, "backup_missing_or_changed"),
        ({"principal": None}, "backup_owner_mismatch"),
        ({"job_id": "other-job"}, "backup_owner_mismatch"),
        ({"flash_bytes": "21"}, "backup_missing_or_changed"),
        ({"sha256": "0" * 64}, "backup_missing_or_changed"),
        ("drop:device_fingerprint", "backup_missing_or_changed"),
        ("padding", "backup_missing_or_changed"),
    ],
)
def test_sidecar_reader_is_strict(backups, change, code):
    store, retained = backups
    record = asdict(retained)
    if change == "drop:device_fingerprint":
        del record["device_fingerprint"]
        text = json.dumps(record)
    elif change == "padding":
        text = json.dumps(record) + " " * 4096
    else:
        text = json.dumps(record | change)
    (store.directory / "backup-job.json").write_text(text, "utf-8")
    with pytest.raises(FlashFailure, match=f"^{code}$"):
        store.load("backup-job", "owner")


# ---------------------------------------------------------------------------
# Build manifest without `build_location`
# ---------------------------------------------------------------------------

FP, ENV = "a" * 64, "b" * 64


@pytest.fixture
def roots(tmp_path):
    root = tmp_path.resolve()
    return root / "state", root / "registered-build-root"


def _legacy(state: Path, manifest: dict) -> Path:
    path = build_directory(state, None, FP, ENV)
    path.mkdir(parents=True)
    (path / "broker-manifest.json").write_text(json.dumps(manifest), "utf-8")
    return path


def _manifest(**extra):
    return {"schema_version": 1, "fingerprint": FP, "environment_fingerprint": ENV} | extra


@pytest.mark.parametrize("registered_root", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_manifest_without_build_location_resolves_to_layout_v1(roots, registered_root, explicit):
    state, build_root = roots
    manifest = _manifest(**({"build_location": {"layout_version": 1}} if explicit else {}))
    path = _legacy(state, manifest)
    assert path == state.resolve() / "firmware-builds" / FP / ENV
    # A registered build_root is ignored for layout v1: no fallback to another root.
    assert resolve_build_manifest(state, build_root if registered_root else None, manifest) == path
    assert build_location(FP, ENV, None) == {"layout_version": 1}


@pytest.mark.parametrize(
    "case",
    ["saved_differs", "saved_has_build_location", "missing_manifest", "bad_location", "schema_2"],
)
def test_legacy_manifest_mismatches_fail_closed(roots, case):
    state, _ = roots
    manifest = _manifest()
    if case == "saved_differs":
        _legacy(state, manifest | {"artifacts": []})
    elif case == "saved_has_build_location":
        _legacy(state, manifest | {"build_location": {"layout_version": 1}})
    elif case == "missing_manifest":
        build_directory(state, None, FP, ENV).mkdir(parents=True)
    elif case == "bad_location":
        _legacy(state, manifest)
        manifest = manifest | {"build_location": {"layout_version": "1"}}
    else:
        manifest = manifest | {"schema_version": 2}
        _legacy(state, manifest)
    code = (
        "build_output_identity_invalid"
        if case in ("bad_location", "schema_2")
        else "build_output_identity_mismatch"
    )
    with pytest.raises(BuildLocationError, match=f"^{code}$"):
        resolve_build_manifest(state, None, manifest)


def test_compact_location_never_falls_back_to_legacy_layout(roots):
    state, build_root = roots
    manifest = _manifest(build_location=build_location(FP, ENV, build_root))
    _legacy(state, manifest)
    with pytest.raises(BuildLocationError, match="^build_output_identity_mismatch$"):
        resolve_build_manifest(state, None, manifest)
