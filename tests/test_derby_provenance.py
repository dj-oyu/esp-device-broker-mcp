"""Read-only bounded request receipts; no artifact, configuration-path or USB IO."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.derby_provenance import (
    MAX_SCOPE_BYTES,
    DerbyAssociation,
    DerbyProvenance,
    capture_provenance,
    finish_provenance,
)
from cardputer_mcp.flash_models import FlashDevice, FlashPython
from cardputer_mcp.ports import DeviceSelector
from cardputer_mcp.store import Store

OWNER = "key:synthetic-owner"
EPOCH = "e" * 32
FINGERPRINT = "f" * 64
EXPECTED = "a" * 64
PRIVATE = "SYNTHETIC_PRIVATE_SENTINEL"


def registration(selector, root):
    root = root / "synthetic-private"
    return FlashDevice(
        selector=selector,
        flash_bytes=8388608,
        project_id="project",
        python_tool=FlashPython(
            executable=root / "python.exe",
            sha256="b" * 64,
            version="Python 3.12.1",
        ),
        esptool_directory=root / "esptool",
        esptool_sha256="c" * 64,
        backup_directory=root / "backup",
    )


@pytest.fixture
def context(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    serial = SerialSettings(
        selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number=PRIVATE),
        expected_firmware_sha256=EXPECTED,
    )
    settings = Settings(state_dir=tmp_path / "state", backend="serial", serial=serial)
    plan = {
        "version": 1,
        "backend": "serial",
        "test": "derby_smoke",
        "job_id": "demo",
        "broker_epoch": EPOCH,
        "session_id": "d" * 32,
        "device_fingerprint": FINGERPRINT,
        "expected_firmware_sha256": EXPECTED,
        "port": PRIVATE,
        "effects": [PRIVATE],
    }
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    store.enqueue_requested("demo", digest, OWNER)

    def forbidden(*args, **kwargs):
        pytest.fail("provenance must not invoke authority, backend or artifact readers")

    broker = SimpleNamespace(
        epoch=EPOCH,
        settings=settings,
        _store=lambda: store,
        principal_valid=forbidden,
        backend=SimpleNamespace(prepare=forbidden, inventory=forbidden, execute=forbidden),
        firmware_build=SimpleNamespace(resolve_flash_manifest=forbidden),
        firmware_source=SimpleNamespace(snapshots=SimpleNamespace(resolve=forbidden)),
        firmware_flash=SimpleNamespace(prepare_plan=forbidden, _known=forbidden),
    )
    try:
        yield broker, store, plan
    finally:
        store.close()


def assert_safe(scope):
    encoded = scope.model_dump_json(exclude_none=True)
    assert len(encoded.encode()) <= MAX_SCOPE_BYTES
    assert PRIVATE not in encoded and "synthetic-private" not in encoded
    assert "installed" not in encoded and "source_commit" not in encoded
    assert DerbyProvenance.model_validate_json(encoded) == scope


def test_capture_binds_exact_owned_request_and_reads_database_only(context, monkeypatch):
    broker, store, plan = context
    statements = []
    store.db.set_trace_callback(statements.append)
    changes = store.db.total_changes

    def no_python_file_io(*args, **kwargs):
        pytest.fail("filesystem IO in provenance")

    monkeypatch.setattr(Path, "open", no_python_file_io)
    scope = capture_provenance(broker, plan, "demo", OWNER)
    assert scope.binding_status == "captured"
    assert scope.job_id == "demo" and scope.broker_epoch == EPOCH
    assert scope.plan_hash == digest_plan(plan) and scope.principal_id == OWNER
    assert scope.device_fingerprint == FINGERPRINT
    assert scope.configured_expected_firmware_sha256 == EXPECTED
    assert scope.serial_configuration_sha256 is not None
    assert scope.association == DerbyAssociation(reason="registered_device_not_bound")
    assert store.db.total_changes == changes
    assert statements and all(sql.startswith("SELECT ") for sql in statements)
    assert_safe(scope)


def test_duplicate_selector_registrations_are_ambiguous_without_first_choice(context, tmp_path):
    broker, _, plan = context
    candidate = registration(broker.settings.serial.selector, tmp_path)
    broker.settings = broker.settings.model_copy(
        update={"flash_devices": {"one": candidate, "two": candidate}}
    )
    scope = capture_provenance(broker, plan, "demo", OWNER)
    assert scope.association.status == "ambiguous"
    assert scope.association.reason == "registered_selector_ambiguous"
    assert "device_id" not in scope.model_dump_json()
    assert_safe(scope)


def test_matching_hash_lkg_and_build_reference_never_automatically_link(context, tmp_path):
    broker, store, plan = context
    broker.settings = broker.settings.model_copy(
        update={
            "flash_devices": {"device": registration(broker.settings.serial.selector, tmp_path)}
        }
    )
    # These hash references alone omit the actual manifest/snapshot proofs.
    with store.db:
        store.db.execute(
            "INSERT INTO firmware_lkg VALUES(?,?,?,?,?,?)",
            ("device", OWNER, "b" * 64, "firmware", "c" * 64, "old-flash"),
        )
        store.db.execute(
            "INSERT INTO build_artifact_references VALUES(?,?,?)",
            ("old-build", OWNER, json.dumps({"manifest_sha256": "d" * 64, "private": PRIVATE})),
        )
        store.db.execute(
            "INSERT INTO firmware_job_summaries VALUES(?,?,?)",
            (
                "old-flash",
                OWNER,
                json.dumps({"expected_app_bin_sha256": EXPECTED, "private": PRIVATE}),
            ),
        )
    scope = capture_provenance(broker, plan, "demo", OWNER)
    assert scope.association.status == "missing"
    assert scope.association.reason == "durable_receipt_chain_unavailable"
    assert "old-build" not in scope.model_dump_json() and "old-flash" not in scope.model_dump_json()
    assert_safe(scope)


@pytest.mark.parametrize(
    "mutation", ["owner", "job", "plan", "epoch", "saved_hash", "request_hash"]
)
def test_core_binding_differences_fail_closed(context, mutation):
    broker, store, plan = context
    owner, job = OWNER, "demo"
    if mutation == "owner":
        owner = "key:other"
    elif mutation == "job":
        job = "other"
    elif mutation == "plan":
        plan = {**plan, "device_fingerprint": "1" * 64}
    elif mutation == "epoch":
        broker.epoch = "1" * 32
    elif mutation == "saved_hash":
        store.db.execute("UPDATE jobs SET plan_hash=?", ("1" * 64,))
    else:
        store.db.execute("UPDATE execution_requests SET plan_hash=?", ("1" * 64,))
    scope = capture_provenance(broker, plan, job, owner)
    assert scope.binding_status == "missing" and scope.association.status == "missing"
    assert scope.association.reason == "core_binding_unavailable"
    assert_safe(scope)


def test_missing_explicit_owner_receipt_is_not_inferred_from_session_or_plan(context):
    broker, store, plan = context
    store.db.execute("DELETE FROM execution_requests")
    scope = capture_provenance(broker, plan, "demo", OWNER)
    assert scope.binding_status == "missing"
    assert scope.association.reason == "core_binding_unavailable"


def test_configured_expected_hash_is_separate_from_installed_identity(context):
    broker, _, plan = context
    broker.settings = broker.settings.model_copy(
        update={
            "serial": broker.settings.serial.model_copy(
                update={"expected_firmware_sha256": "1" * 64}
            )
        }
    )
    scope = capture_provenance(broker, plan, "demo", OWNER)
    assert scope.binding_status == "missing"
    assert scope.association.reason == "configured_firmware_mismatch"
    assert_safe(scope)


def test_finish_freezes_same_scope_and_detects_config_selector_change(context):
    broker, _, plan = context
    before = capture_provenance(broker, plan, "demo", OWNER)
    same = capture_provenance(broker, plan, "demo", OWNER)
    assert finish_provenance(before, same) == before
    serial = broker.settings.serial
    changed_selector = serial.selector.model_copy(update={"serial_number": "other-synthetic"})
    broker.settings = broker.settings.model_copy(
        update={"serial": serial.model_copy(update={"selector": changed_selector})}
    )
    after = capture_provenance(broker, plan, "demo", OWNER)
    assert before.configured_expected_firmware_sha256 == after.configured_expected_firmware_sha256
    assert before.association == after.association
    assert before.serial_configuration_sha256 != after.serial_configuration_sha256
    final = finish_provenance(before, after)
    assert final.binding_status == "changed" and final.association.reason == "provenance_changed"
    assert final.serial_configuration_sha256 == before.serial_configuration_sha256
    assert (
        final.plan_hash == before.plan_hash
        and final.device_fingerprint == before.device_fingerprint
    )
    assert_safe(final)


def test_finish_never_adopts_later_job_owner_or_receipt_association(context):
    broker, _, plan = context
    before = capture_provenance(broker, plan, "demo", OWNER)
    later = DerbyProvenance.model_validate(
        {
            **before.model_dump(),
            "job_id": "later",
            "principal_id": "key:other",
            "plan_hash": "1" * 64,
        }
    )
    final = finish_provenance(before, later)
    assert final.binding_status == "changed"
    assert (
        final.job_id == "demo"
        and final.principal_id == OWNER
        and final.plan_hash == before.plan_hash
    )
    assert finish_provenance(before, {"private": PRIVATE}) == final
    assert_safe(final)
