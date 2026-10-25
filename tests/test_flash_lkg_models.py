"""Read-compat pins for the LKG envelope and the LKG metadata inside it.

Synthetic files only. The envelope cases drive FirmwareSnapshots.load directly;
the metadata cases go through the real broker with the fake driver from
test_flash_policy, so they pin what a caller actually observes.
"""

import hashlib
import json

import pytest
import test_flash_policy

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.flash_lkg_models import (
    LkgEnvelope,
    LkgMetadata,
    bound_lkg_metadata,
    lkg_compat,
)
from cardputer_mcp.flash_lkg_store import FirmwareSnapshots
from cardputer_mcp.models import TestName as Action

env = test_flash_policy.env

METADATA = {"kind": "full_backup", "device_id": "fixture", "z_first": 1, "a_last": 2}


@pytest.fixture
def snapshots(tmp_path):
    return FirmwareSnapshots(tmp_path / "lkg", windows_private_acl_confirmed=True)


def _put_envelope(snapshots, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return snapshots.put(raw)


def _tree(root):
    return {path.name: path.read_bytes() for path in sorted(root.iterdir())}


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["schema_version", "metadata", "files"])
def test_envelope_requires_every_key(snapshots, missing):
    body = {"schema_version": 1, "metadata": METADATA, "files": {}}
    del body[missing]
    receipt = _put_envelope(snapshots, body)
    with pytest.raises(FlashFailure, match="^lkg_snapshot_missing_or_changed$"):
        snapshots.load(receipt)


@pytest.mark.parametrize("version", [2, 0, "1", None, [1], {"v": 1}, 1.5])
def test_envelope_rejects_other_schema_versions(snapshots, version):
    receipt = _put_envelope(snapshots, {"schema_version": version, "metadata": {}, "files": {}})
    with pytest.raises(FlashFailure, match="^lkg_snapshot_missing_or_changed$"):
        snapshots.load(receipt)


@pytest.mark.parametrize("version", [1, 1.0, True])
def test_envelope_version_check_is_equality_with_one(snapshots, version):
    # Today's reader compares with `!= 1`, so 1.0 and true pass as well.
    receipt = _put_envelope(snapshots, {"schema_version": version, "metadata": {}, "files": {}})
    assert snapshots.load(receipt) == ({}, {})


@pytest.mark.parametrize(
    "raw",
    [
        b'["schema_version","metadata","files"]',
        b'"schema_version"',
        b"1",
        b"null",
        b"{not json",
        b"\xff\xfe",
    ],
)
def test_envelope_must_be_a_json_object(snapshots, raw):
    with pytest.raises(FlashFailure, match="^lkg_snapshot_missing_or_changed$"):
        snapshots.load(_put_envelope(snapshots, raw))


def test_envelope_over_one_mebibyte_is_rejected(snapshots):
    body = {"schema_version": 1, "metadata": {"pad": "x" * 1048576}, "files": {}}
    with pytest.raises(FlashFailure, match="^lkg_snapshot_missing_or_changed$"):
        snapshots.load(_put_envelope(snapshots, body))


@pytest.mark.parametrize(
    "files",
    [
        [],
        "digest",
        None,
        {"app.bin": 1},
        {"app.bin": None},
        {"app.bin": "A" * 64},
        {"app.bin": "0" * 64},
        {"app.bin": "../" + "0" * 61},
    ],
)
def test_envelope_files_must_name_present_objects(snapshots, files):
    receipt = _put_envelope(snapshots, {"schema_version": 1, "metadata": {}, "files": files})
    with pytest.raises(FlashFailure, match="^lkg_snapshot_missing_or_changed$"):
        snapshots.load(receipt)


def test_receipt_that_is_not_a_digest_is_reported_as_missing(snapshots):
    with pytest.raises(FlashFailure, match="^lkg_snapshot_missing_or_changed$"):
        snapshots.load("not-a-digest")


@pytest.mark.parametrize("metadata", [None, [1, 2], "text", 7, {}, {"device": "synthetic"}])
def test_store_returns_metadata_of_any_json_shape_unchanged(snapshots, metadata):
    # The store itself does not interpret metadata; firmware_flash does.
    image = snapshots.put(b"image")
    body = {"schema_version": 1, "metadata": metadata, "files": {"app.bin": image}}
    receipt = _put_envelope(snapshots, body)
    loaded, files = snapshots.load(receipt)
    assert loaded == metadata and type(loaded) is type(metadata)
    assert files == {"app.bin": b"image"}


def test_loading_keeps_metadata_key_order_and_file_order(snapshots):
    one, two = snapshots.put(b"one"), snapshots.put(b"two")
    body = {"schema_version": 1, "metadata": METADATA, "files": {"z.bin": one, "a.bin": two}}
    loaded, files = snapshots.load(_put_envelope(snapshots, body))
    assert list(loaded) == list(METADATA)
    assert list(files) == ["z.bin", "a.bin"]


def test_reading_never_rewrites_stored_objects(snapshots):
    receipt = snapshots.stage(METADATA | {"compat": None}, {"app.bin": b"image"})
    # A non-canonical envelope stays non-canonical: the receipt is over its bytes.
    loose = _put_envelope(
        snapshots,
        b'{ "files": {}, "metadata": {"b": 1, "a": 2},\n "schema_version": 1 }',
    )
    before = _tree(snapshots.root)
    times = {path.name: path.stat().st_mtime_ns for path in snapshots.root.iterdir()}
    for _ in range(2):
        snapshots.load(receipt)
        assert snapshots.load(loose) == ({"b": 1, "a": 2}, {})
    assert _tree(snapshots.root) == before
    assert {path.name: path.stat().st_mtime_ns for path in snapshots.root.iterdir()} == times
    for name, data in before.items():
        assert hashlib.sha256(data).hexdigest() == name


# ---------------------------------------------------------------------------
# LKG metadata read by firmware_flash (real broker, fake driver)
# ---------------------------------------------------------------------------


def _lkg(env):
    root = env.broker.settings.flash_devices["fixture"].backup_directory / "lkg"
    return FirmwareSnapshots(root, windows_private_acl_confirmed=True)


def _record(env, metadata, files, kind="artifact_bundle", current=False):
    """Stage `metadata` with `files` and record it as a device LKG for alice."""
    receipt = _lkg(env).stage(metadata, files)
    baseline = env.store.firmware_current("fixture")["baseline"]
    env.store.db.execute(
        "INSERT OR IGNORE INTO firmware_lkg_history VALUES (?,?,?,?,?,?)",
        (receipt, "fixture", "alice", kind, baseline, "flash-one"),
    )
    if current:
        env.store.db.execute(
            "UPDATE firmware_lkg SET receipt=?, kind=? WHERE device_id=?",
            (receipt, kind, "fixture"),
        )
    env.store.db.commit()
    return receipt


def _selected(env):
    return _lkg(env).load(env.selected)


async def test_artifact_bundle_lkg_with_compat_dict_is_a_flash_baseline(env):
    metadata, files = _selected(env)
    assert metadata["kind"] == "artifact_bundle"
    assert isinstance(metadata["compat"], dict) and "table_offset" in metadata["compat"]
    before = _tree(_lkg(env).root)
    prepared = env.prepare("after-bundle", build_job_id="build-one", lkg_receipt=env.selected)
    assert (prepared["plan"]["lkg_receipt"], prepared["plan"]["recovery_kind"]) == (
        env.selected,
        "artifact_bundle",
    )
    # Preparing reads the LKG without touching any stored object.
    assert _tree(_lkg(env).root) == before
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("after-bundle")["state"] == "succeeded", env.broker.job("after-bundle")
    # The previous objects are still byte-identical; the reader never re-dumped them.
    after = _tree(_lkg(env).root)
    assert {name: after[name] for name in before} == before
    assert hashlib.sha256(after[env.selected]).hexdigest() == env.selected


async def test_restore_of_artifact_bundle_lkg_uses_its_compat_table_offset(env):
    prepared = env.prepare("restore-bundle", Action.RESTORE, lkg_receipt=env.selected)
    assert prepared["plan"]["recovery_kind"] == "artifact_bundle"
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("restore-bundle")["state"] == "succeeded"


async def test_extra_metadata_key_is_accepted_and_kept(env):
    metadata, files = _selected(env)
    receipt = _record(env, metadata | {"future_key": {"nested": [1]}}, files)
    assert _lkg(env).load(receipt)[0]["future_key"] == {"nested": [1]}
    prepared = env.prepare("extra-key", build_job_id="build-one", lkg_receipt=receipt)
    assert prepared["plan"]["lkg_receipt"] == receipt
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("extra-key")["state"] == "succeeded", env.broker.job("extra-key")


async def test_artifact_bundle_lkg_without_compat_skips_the_layout_check(env):
    # No such writer exists; today the reader treats it like compat: null.
    metadata, files = _selected(env)
    receipt = _record(env, {k: v for k, v in metadata.items() if k != "compat"}, files)
    prepared = env.prepare("no-compat", build_job_id="build-one", lkg_receipt=receipt)
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("no-compat")["state"] == "succeeded", env.broker.job("no-compat")


async def test_changed_compat_dict_refuses_the_flash_before_any_write(env):
    metadata, files = _selected(env)
    receipt = _record(env, metadata | {"compat": metadata["compat"] | {"mode": "other"}}, files)
    prepared = env.prepare("changed-compat", build_job_id="build-one", lkg_receipt=receipt)
    writes = env.driver.writes
    env.execute(prepared)
    await env.broker.queue.join()
    job = env.broker.job("changed-compat")
    assert job["state"] == "failed"
    assert job["result"] == "lkg_partition_or_security_change_not_allowed", job
    assert env.driver.writes == writes


@pytest.mark.parametrize(
    "change,expected",
    [
        ("drop:device_id", KeyError("device_id")),
        ("drop:principal", KeyError("principal")),
        ("drop:selector_sha256", KeyError("selector_sha256")),
        ("drop:flash_bytes", KeyError("flash_bytes")),
        ("drop:kind", BrokerError("lkg_receipt_kind_changed")),
        ({"kind": "full_backup"}, BrokerError("lkg_receipt_kind_changed")),
        ({"device_id": "other"}, BrokerError("lkg_device_binding_changed")),
        ({"principal": "bob"}, BrokerError("lkg_device_binding_changed")),
        ({"flash_bytes": "4194304"}, BrokerError("lkg_device_binding_changed")),
        # The binding comparison is ordered: an earlier mismatch wins over a
        # later missing key, and a later mismatch loses to an earlier missing key.
        ("other-device-and-drop:principal", BrokerError("lkg_device_binding_changed")),
        ("drop:device_id-and-other-principal", KeyError("device_id")),
        # A missing kind is only noticed after the binding passed.
        ("drop:kind-and-other-device", BrokerError("lkg_device_binding_changed")),
    ],
)
async def test_named_lkg_metadata_damage_is_observed_by_prepare(env, change, expected):
    metadata, files = _selected(env)
    if isinstance(change, dict):
        damaged = metadata | change
    else:
        damaged = dict(metadata)
        if change.startswith("other-device"):
            damaged["device_id"] = "other"
        if change.endswith("other-principal"):
            damaged["principal"] = "bob"
        if change == "drop:kind-and-other-device":
            damaged["device_id"] = "other"
        for name in ("device_id", "principal", "selector_sha256", "flash_bytes", "kind"):
            if f"drop:{name}" in change:
                damaged.pop(name)
    receipt = _record(env, damaged, files)
    before = _tree(_lkg(env).root)
    with pytest.raises(type(expected)) as raised:
        env.prepare("damaged", build_job_id="build-one", lkg_receipt=receipt)
    # Exactly today's escape: a bare KeyError, or a BrokerError with the code.
    assert type(raised.value) is type(expected)
    assert raised.value.args == expected.args
    assert _tree(_lkg(env).root) == before


@pytest.mark.parametrize("metadata", [[1, 2], "text", None])
async def test_named_lkg_with_non_object_metadata_escapes_as_type_error(env, metadata):
    _, files = _selected(env)
    receipt = _record(env, metadata, files)
    with pytest.raises(TypeError):
        env.prepare("non-object", build_job_id="build-one", lkg_receipt=receipt)


async def test_current_lkg_damage_found_at_execution_needs_review_without_writes(env):
    # A flash naming only a backup reads the *current* LKG's compat at execution.
    metadata, files = _selected(env)
    prepared = env.prepare("previous", build_job_id="build-one", backup_job_id="backup-one")
    _record(env, {k: v for k, v in metadata.items() if k != "device_id"}, files, current=True)
    writes = env.driver.writes
    env.execute(prepared)
    await env.broker.queue.join()
    job = env.broker.job("previous")
    # The bare KeyError from the binding check surfaces as an unclassified failure.
    assert (job["state"], job["result"], job["stage"]) == (
        "needs_review",
        "backend_error_no_retry",
        None,
    )
    assert env.driver.writes == writes


@pytest.mark.parametrize("compat", ["same", "changed"])
async def test_current_lkg_compat_dict_is_matched_at_execution(env, compat):
    metadata, files = _selected(env)
    prepared = env.prepare("previous", build_job_id="build-one", backup_job_id="backup-one")
    if compat == "changed":
        changed = metadata | {"compat": metadata["compat"] | {"mode": "other"}}
        _record(env, changed, files, current=True)
    writes = env.driver.writes
    env.execute(prepared)
    await env.broker.queue.join()
    job = env.broker.job("previous")
    expected = {
        # Layout matched; the run then stops at the stale backup fence (the
        # device now holds flash-one, not the backup-one image).
        "same": ("failed", "flash_backup_stale_needs_review", "flash_backup_prewrite_check"),
        "changed": ("failed", "lkg_partition_or_security_change_not_allowed", None),
    }[compat]
    assert (job["state"], job["result"], job["stage"]) == expected
    assert env.driver.writes == writes


# ---------------------------------------------------------------------------
# The models themselves (validate-only)
# ---------------------------------------------------------------------------

BOUND = dict(device_id="fixture", principal="alice", selector_sha256="b" * 64, flash_bytes=4)


def test_metadata_view_keeps_compat_absent_distinct_from_null_and_keeps_extras():
    compat = {"table_offset": 32768}
    absent = LkgMetadata.model_validate(BOUND | {"kind": "full_backup"})
    null = LkgMetadata.model_validate(BOUND | {"kind": "full_backup", "compat": None})
    stored = LkgMetadata.model_validate(BOUND | {"compat": compat, "future": 1})
    assert (absent.compat, absent.compat_stored) == (None, False)
    assert (null.compat, null.compat_stored) == (None, True)
    assert stored.compat is compat and stored.compat_stored
    assert stored.model_extra == {"future": 1}


def test_bound_metadata_is_not_copied_or_changed():
    metadata = BOUND | {"kind": "artifact_bundle", "compat": {"table_offset": 1}, "x": [1]}
    before = json.dumps(metadata)
    view = bound_lkg_metadata(metadata, BOUND, "artifact_bundle")
    assert view.compat is metadata["compat"]
    assert json.dumps(metadata) == before
    assert lkg_compat(metadata) is metadata["compat"]


def test_envelope_model_keeps_the_parsed_metadata_object():
    metadata = {"b": 1, "a": [2]}
    envelope = LkgEnvelope.model_validate(
        {"schema_version": 1, "metadata": metadata, "files": {"x": "0" * 64}}
    )
    assert envelope.metadata is metadata
    assert list(envelope.files) == ["x"]
