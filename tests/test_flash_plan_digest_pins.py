"""Stage 0.5 characterization: flash plan text and digests are pinned literals.

FLASH_EFFECTS/FLASH_STAGES/argv_template are matched by exact equality in
validate_flash_plan, and OperationPlan.digest hashes asdict(), so any wording,
field-name or serialisation change strands every stored plan. These pins make
such a change a deliberate, reviewed edit.
"""

import hashlib
import json
from dataclasses import asdict

import pytest

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.firmware_flash import (
    FLASH_EFFECTS,
    FLASH_STAGES,
    operation_from_plan,
    validate_flash_plan,
)
from cardputer_mcp.flash_lifecycle import OperationPlan
from cardputer_mcp.store import Store

FLASH_EFFECTS_SHA256 = "5a883b8a4a3e22b4308c209ff9e3186d7ac77c267d6cda8a19bf719e1f011313"
SAMPLE_OPERATION_DIGEST = "6ff7638e4c91797d6caab8ce03510bcc0345f97f131e32585459fc1a968ec0d5"
SAMPLE_PLAN_HASH = "433a8268fbfac4e30901655479e94f70a8dac580b8d16cc273ddaf6731fdc7c3"


def _operation():
    return OperationPlan(
        "flash", "flash-job", "alice", "0" * 32, "1" * 32, "2" * 64, 4194304, "3" * 64,
        "4" * 64, "5" * 64, "6" * 64, "artifact_bundle",
    )  # fmt: skip


def _plan():
    op = _operation()
    return {
        "version": 1,
        "broker_epoch": op.broker_epoch,
        "session_id": op.session_id,
        "device": "mock-cardputer",
        "backend": "mock",
        "job_id": op.job_id,
        "test": "firmware_flash",
        "stages": list(FLASH_STAGES),
        "manifest_export_sha256": "7" * 64,
        "flash_operation": op.operation,
        "lkg_receipt": "4" * 64,
        "recovery_kind": op.recovery_kind,
        "device_id": "fixture",
        "build_job_id": "build-one",
        "backup_job_id": None,
        "flash_principal": op.principal,
        "flash_bytes": op.flash_bytes,
        "device_fingerprint": op.device_fingerprint,
        "expected_device_sha256": op.expected_device_sha256,
        "backup_sha256": op.backup_sha256,
        "manifest_sha256": op.manifest_sha256,
        "expected_elf_sha256": op.expected_elf_sha256,
        "expected_app_bin_sha256": "8" * 64,
        "registration_sha256": "9" * 64,
        "fingerprint": op.digest,
        "effects": list(FLASH_EFFECTS),
        "argv_template": ["registered-python", "fixed-flash-worker", "broker-owned-operation"],
        "expected_idf": "6.0.1",
        "target": "esp32s3",
    }


def test_flash_effects_and_stages_text_is_pinned():
    compact = json.dumps(FLASH_EFFECTS, separators=(",", ":")).encode()
    assert len(FLASH_EFFECTS) == 12
    assert hashlib.sha256(compact).hexdigest() == FLASH_EFFECTS_SHA256
    assert FLASH_STAGES == [
        "flash_security_probe",
        "flash_backup_or_verify",
        "flash_write_or_restore",
        "flash_readback_verify",
        "flash_boot_observe",
        "flash_close",
    ]


def test_operation_plan_digest_and_field_names_are_pinned():
    op = _operation()
    assert list(asdict(op)) == [
        "operation",
        "job_id",
        "principal",
        "broker_epoch",
        "session_id",
        "device_fingerprint",
        "flash_bytes",
        "expected_device_sha256",
        "backup_sha256",
        "manifest_sha256",
        "expected_elf_sha256",
        "recovery_kind",
    ]
    # sort_keys with DEFAULT separators (unlike digest_plan's compact form).
    expected = hashlib.sha256(json.dumps(asdict(op), sort_keys=True).encode()).hexdigest()
    assert op.digest == expected == SAMPLE_OPERATION_DIGEST


def test_sample_flash_plan_validates_and_its_hash_is_pinned():
    plan = _plan()
    validate_flash_plan(plan)
    assert operation_from_plan(plan) == _operation()
    assert digest_plan(plan) == SAMPLE_PLAN_HASH


@pytest.mark.parametrize(
    "field,mutate",
    [
        ("effects", lambda v: [v[0] + " "] + v[1:]),
        ("effects", lambda v: v[:-1]),
        ("stages", lambda v: list(reversed(v))),
        ("argv_template", lambda v: v + ["extra"]),
        ("fingerprint", lambda v: "0" * 64),
    ],
)
def test_any_wording_or_digest_change_strands_the_plan(field, mutate):
    plan = _plan()
    plan[field] = mutate(plan[field])
    with pytest.raises(ValueError, match="^invalid_flash_plan$"):
        validate_flash_plan(plan)


def test_legacy_plan_without_manifest_export_still_validates():
    plan = _plan()
    del plan["manifest_export_sha256"]
    validate_flash_plan(plan)
    assert digest_plan(plan) != SAMPLE_PLAN_HASH


def test_stored_plan_text_differs_from_hashed_bytes_but_both_round_trip(tmp_path):
    plan = _plan() | {"note": "café", "deadline": 1761350400.125}
    plan_hash = digest_plan(plan)
    store = Store(tmp_path / "state.sqlite3")
    try:
        store.put_plan(plan_hash, plan)
        stored = store.db.execute("SELECT payload FROM plans WHERE hash=?", (plan_hash,)).fetchone()
        stored = stored[0]
        hashed = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        # Stored text uses default ", " / ": " separators; the hash uses compact ones.
        assert stored == json.dumps(plan, sort_keys=True)
        assert stored != hashed
        assert stored.startswith('{"argv_template": [') and hashed.startswith('{"argv_template":[')
        # Both ASCII-escape non-ASCII text the same way.
        assert "caf\\u00e9" in stored and "caf\\u00e9" in hashed
        for text in (stored, hashed):
            assert json.loads(text) == plan
            assert digest_plan(json.loads(text)) == plan_hash
        assert store.plan(plan_hash) == plan
        # INSERT OR IGNORE: a second put under the same hash never rewrites the row.
        store.put_plan(plan_hash, {"other": True})
        assert store.plan(plan_hash) == plan
    finally:
        store.db.close()
