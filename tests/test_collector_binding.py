"""Receipt-chain isolation using synthetic images, durable rows and serial inventory."""

import copy
import hashlib
from types import SimpleNamespace

import pytest
from test_diagnostic_modeproof import image_fixture
from test_serial_backend import backend, config

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.build_artifacts import manifest_digest
from cardputer_mcp.collector_binding import (
    collector_live,
    prepare_collector,
    resolve_collector,
    validate_collector_schema,
)
from cardputer_mcp.diagnostic_modeproof import collect_modeproof
from cardputer_mcp.models import TestName as Action


@pytest.fixture
def chain(tmp_path):
    provenance, artifacts, files = image_fixture(tmp_path, "OFF")
    provenance["source_receipt"] = "c" * 64
    proof = collect_modeproof(tmp_path, "OFF", provenance, artifacts)
    device, _ = backend()
    registration = SimpleNamespace(
        selector=config().selector,
        project_id="public-project",
        model_dump_json=lambda: "registered",
    )
    build = {
        "test": "firmware_build", "job_id": "public-build", "project_id": "public-project",
        "build_principal": "owner", "source_receipt": "c" * 64,
        "source_provenance": provenance, "derby_detail_mode": "OFF",
    }
    build_hash = digest_plan(build)
    manifest = {
        "build_job_id": "public-build", "build_plan_hash": build_hash,
        "source_provenance": provenance, "derby_detail_mode": "OFF",
        "diagnostic_modeproof": proof, "artifacts": artifacts,
    }
    flash = {
        "test": "firmware_flash", "flash_operation": "flash", "job_id": "public-flash",
        "flash_principal": "owner", "device_id": "public-device", "build_job_id": "public-build",
        "device_fingerprint": device.prepare(Action.DERBY_FIXED)["device_fingerprint"],
        "manifest_sha256": manifest_digest(manifest),
        "expected_elf_sha256": hashlib.sha256(files["cardputer_pocketjs.elf"]).hexdigest(),
        "expected_app_bin_sha256": hashlib.sha256(files["cardputer_pocketjs.bin"]).hexdigest(),
        "registration_sha256": hashlib.sha256(b"registered").hexdigest(),
    }
    flash_hash = digest_plan(flash)
    jobs = {
        "public-flash": {"state": "succeeded", "plan_hash": flash_hash},
        "public-build": {"state": "succeeded", "plan_hash": build_hash},
    }
    plans = {flash_hash: flash, build_hash: build}
    current = {"completed_job": "public-flash", "receipt": "d" * 64, "kind": "artifact_bundle"}
    summary = {
        "readback_verified": True, "passive_boot_verified": True,
        "boot_log_matches_expected_artifact": True,
        "verification_basis": "full_segment_sha256_readback_and_boot_elf_prefix",
        "last_known_good_receipt": "d" * 64,
    }
    store = SimpleNamespace(
        job=jobs.get, plan=plans.get, firmware_summary=lambda *args: summary,
        firmware_current=lambda *args: current,
    )
    metadata = {
        "manifest": manifest, "expected_elf_sha256": flash["expected_elf_sha256"],
        "expected_app_bin_sha256": flash["expected_app_bin_sha256"],
    }
    resident = SimpleNamespace(
        settings=SimpleNamespace(
            serial=config(), flash_devices={"public-device": registration}
        ),
        backend=device, _store=lambda: store,
        firmware_flash=SimpleNamespace(_known=lambda *args: (current, metadata, files)),
    )
    return SimpleNamespace(
        resident=resident, jobs=jobs, plans=plans, current=current, summary=summary,
        metadata=metadata, manifest=manifest, files=files, flash=flash, build=build,
    )


def test_retained_flash_receipt_sets_elf_independently_of_old_config(chain):
    details = prepare_collector(chain.resident, "public-flash", "owner")
    assert details["expected_firmware_sha256"] == chain.flash["expected_elf_sha256"]
    assert details["expected_firmware_sha256"] != config().expected_firmware_sha256
    assert details["firmware_verification"] == "successful_flash_receipt"
    plan = {"test": "derby_fixed_workload", **details}
    validate_collector_schema(plan)
    assert collector_live(chain.resident, plan, "owner")
    assert not collector_live(chain.resident, plan, "other")
    changed = copy.deepcopy(plan)
    changed["expected_firmware_sha256"] = config().expected_firmware_sha256
    assert not collector_live(chain.resident, changed, "owner")
    with pytest.raises(ValueError):
        validate_collector_schema(changed)


@pytest.mark.parametrize("state", ["running", "failed", "cancelled", "needs_review"])
def test_unsuccessful_flash_rejected(chain, state):
    chain.jobs["public-flash"]["state"] = state
    with pytest.raises(ValueError):
        resolve_collector(chain.resident, "public-flash", "owner")


@pytest.mark.parametrize("damage", ["image", "proof", "source", "current", "device", "readback"])
def test_receipt_chain_changes_rejected(chain, damage):
    if damage == "image":
        chain.files["cardputer_pocketjs.bin"] += b"changed"
    elif damage == "proof":
        chain.manifest["diagnostic_modeproof"]["proof"]["detail_measurement"]["mode"] = "ON"
    elif damage == "source":
        chain.manifest["source_provenance"]["commit"] = "f" * 40
    elif damage == "current":
        chain.current["completed_job"] = "later-flash"
    elif damage == "device":
        chain.resident.settings.serial = config().model_copy(
            update={"selector": config().selector.model_copy(update={"serial_number": "OTHER"})}
        )
    else:
        chain.summary["readback_verified"] = False
    with pytest.raises(ValueError):
        resolve_collector(chain.resident, "public-flash", "owner")


def test_other_principal_and_missing_reference_never_use_config(chain):
    for job, principal in (("missing", "owner"), ("public-flash", "other")):
        with pytest.raises(ValueError):
            resolve_collector(chain.resident, job, principal)
    assert collector_live(chain.resident, {"test": "derby_fixed_workload"}, "owner")
    assert not collector_live(chain.resident, {"collector_binding": {}}, "owner")


def test_bound_plan_schema_and_current_receipt_guard(chain):
    from cardputer_mcp.plan_approval import PlanApprovals

    details = prepare_collector(chain.resident, "public-flash", "owner")
    plan = {
        "version": 1, "broker_epoch": "e" * 32, "session_id": "f" * 32,
        "device": "configured-cardputer", "backend": "serial", "job_id": "public-trial",
        "test": "derby_fixed_workload", **details,
    }
    PlanApprovals._schema(plan)
    chain.current["completed_job"] = "later-flash"
    assert not collector_live(chain.resident, plan, "owner")
    plan["collector_binding"]["extra"] = "not-allowed"
    with pytest.raises(ValueError):
        PlanApprovals._schema(plan)


def test_missing_modeproof_rejected(chain):
    del chain.manifest["diagnostic_modeproof"]
    with pytest.raises(ValueError):
        resolve_collector(chain.resident, "public-flash", "owner")


def test_explicit_receipt_preflight_does_not_require_or_mutate_old_config(chain):
    from cardputer_mcp.serial_backend import SerialBackend

    original = chain.resident.backend.config.model_copy(update={"expected_firmware_sha256": None})
    chain.resident.backend = SerialBackend(original, inventory=chain.resident.backend.inventory)
    details = prepare_collector(chain.resident, "public-flash", "owner")
    assert details["expected_firmware_sha256"] == chain.flash["expected_elf_sha256"]
    assert chain.resident.backend.config is original
    assert original.expected_firmware_sha256 is None


@pytest.mark.parametrize("configured_hash", [None, "a" * 64])
async def test_receipt_selected_hash_admits_and_completes_real_broker_trial(
    chain, tmp_path, configured_hash
):
    import json

    from test_derby_fixed_workload import FixedWire, backend_for

    from cardputer_mcp.broker import Broker
    from cardputer_mcp.config import Settings
    from cardputer_mcp.flash_models import FlashDevice, FlashPython
    from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
    from cardputer_mcp.plan_approval import PlanApprovals

    serial = config().model_copy(update={"expected_firmware_sha256": configured_hash})
    registration = FlashDevice(
        selector=serial.selector, flash_bytes=8388608, project_id="public-project",
        python_tool=FlashPython(
            executable=tmp_path / "python.exe", sha256="b" * 64, version="Python 3.12.0"
        ),
        esptool_directory=tmp_path / "esptool", esptool_sha256="b" * 64,
        backup_directory=tmp_path / "backups",
    )
    wire = FixedWire()
    resident = Broker(
        Settings(
            backend="serial", serial=serial, state_dir=tmp_path / "broker",
            flash_devices={"public-device": registration},
        ),
        backend=backend_for(wire, serial), serial_enabled=True,
        lock_path=tmp_path / "device.lock", principal_valid=lambda *args: True,
    )
    await resident.start()
    try:
        token = resident.acquire(
            AcquireRequest(owner=Origin.LOCAL, lease_seconds=300), principal_id="owner"
        )["session"]
        chain.flash["registration_sha256"] = hashlib.sha256(
            registration.model_dump_json().encode()
        ).hexdigest()
        metadata = {
            **chain.metadata, "kind": "artifact_bundle", "device_id": "public-device",
            "principal": "owner", "flash_bytes": registration.flash_bytes,
            "selector_sha256": hashlib.sha256(
                registration.selector.model_dump_json().encode()
            ).hexdigest(),
        }
        receipt = resident.firmware_flash._snapshots(registration).stage(metadata, chain.files)
        store = resident._store()
        for saved in (chain.build, chain.flash):
            digest = digest_plan(saved)
            store.put_plan(digest, saved)
            store.enqueue_requested(saved["job_id"], digest, "owner")
            store.set_job(saved["job_id"], "succeeded", "synthetic_verified_receipt")
        summary = {**chain.summary, "last_known_good_receipt": receipt}
        store.db.execute(
            "INSERT INTO firmware_job_summaries VALUES (?,?,?)",
            ("public-flash", "owner", json.dumps(summary)),
        )
        row = (receipt, "public-device", "owner", "artifact_bundle", "", "public-flash")
        store.db.execute(
            "INSERT INTO firmware_lkg VALUES (?,?,?,?,?,?)",
            ("public-device", "owner", receipt, "artifact_bundle", "", "public-flash"),
        )
        store.db.execute("INSERT INTO firmware_lkg_history VALUES (?,?,?,?,?,?)", row)
        store.db.commit()
        prepared = resident.prepare(PrepareRequest(
            session=token, job_id="public-trial", test=Action.DERBY_FIXED,
            flash_job_id="public-flash",
        ))
        assert prepared["plan"]["expected_firmware_sha256"] == chain.flash["expected_elf_sha256"]
        assert wire.writes == []
        assert PlanApprovals(resident).validate_current(
            prepared["plan_hash"], resident.session
        ) == prepared["plan"]
        admitted = resident.execute(ExecuteRequest(session=token, plan_hash=prepared["plan_hash"]))
        assert admitted["state"] == "queued"
        await resident.queue.join()
        assert resident.job("public-trial")["state"] == "succeeded"
        assert resident.settings.serial.expected_firmware_sha256 == configured_hash
        assert not wire.is_open
    finally:
        await resident.close()
