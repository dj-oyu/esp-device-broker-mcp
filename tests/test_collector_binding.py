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
        "test": "firmware_build",
        "job_id": "public-build",
        "project_id": "public-project",
        "build_principal": "owner",
        "source_receipt": "c" * 64,
        "source_provenance": provenance,
        "derby_detail_mode": "OFF",
    }
    build_hash = digest_plan(build)
    manifest = {
        "build_job_id": "public-build",
        "build_plan_hash": build_hash,
        "source_provenance": provenance,
        "derby_detail_mode": "OFF",
        "diagnostic_modeproof": proof,
        "artifacts": artifacts,
    }
    flash = {
        "test": "firmware_flash",
        "flash_operation": "flash",
        "job_id": "public-flash",
        "flash_principal": "owner",
        "device_id": "public-device",
        "build_job_id": "public-build",
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
        "readback_verified": True,
        "passive_boot_verified": True,
        "boot_log_matches_expected_artifact": True,
        "verification_basis": "full_segment_sha256_readback_and_boot_elf_prefix",
        "last_known_good_receipt": "d" * 64,
    }
    store = SimpleNamespace(
        job=jobs.get,
        plan=plans.get,
        firmware_summary=lambda *args: summary,
        firmware_current=lambda *args: current,
    )
    metadata = {
        "manifest": manifest,
        "expected_elf_sha256": flash["expected_elf_sha256"],
        "expected_app_bin_sha256": flash["expected_app_bin_sha256"],
    }
    resident = SimpleNamespace(
        settings=SimpleNamespace(serial=config(), flash_devices={"public-device": registration}),
        backend=device,
        _store=lambda: store,
        firmware_flash=SimpleNamespace(_known=lambda *args: (current, metadata, files)),
    )
    return SimpleNamespace(
        resident=resident,
        jobs=jobs,
        plans=plans,
        current=current,
        summary=summary,
        metadata=metadata,
        manifest=manifest,
        files=files,
        flash=flash,
        build=build,
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
        "version": 1,
        "broker_epoch": "e" * 32,
        "session_id": "f" * 32,
        "device": "configured-cardputer",
        "backend": "serial",
        "job_id": "public-trial",
        "test": "derby_fixed_workload",
        **details,
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
@pytest.mark.parametrize("mutation_phase", ["none", "admission", "final"])
async def test_receipt_selected_hash_admits_and_completes_real_broker_trial(
    chain, tmp_path, configured_hash, mutation_phase, monkeypatch
):
    import json

    from test_derby_fixed_workload import FixedWire, backend_for

    from cardputer_mcp.broker import Broker, BrokerError
    from cardputer_mcp.config import Settings
    from cardputer_mcp.flash_models import FlashDevice, FlashPython
    from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
    from cardputer_mcp.plan_approval import PlanApprovals

    serial = config().model_copy(update={"expected_firmware_sha256": configured_hash})
    registration = FlashDevice(
        selector=serial.selector,
        flash_bytes=8388608,
        project_id="public-project",
        python_tool=FlashPython(
            executable=tmp_path / "python.exe", sha256="b" * 64, version="Python 3.12.0"
        ),
        esptool_directory=tmp_path / "esptool",
        esptool_sha256="b" * 64,
        backup_directory=tmp_path / "backups",
        private_backup_acl_confirmed=True,
    )
    wire = FixedWire()
    resident = Broker(
        Settings(
            backend="serial",
            serial=serial,
            state_dir=tmp_path / "broker",
            flash_devices={"public-device": registration},
        ),
        backend=backend_for(wire, serial),
        serial_enabled=True,
        lock_path=tmp_path / "device.lock",
        principal_valid=lambda *args: True,
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
            **chain.metadata,
            "kind": "artifact_bundle",
            "device_id": "public-device",
            "principal": "owner",
            "flash_bytes": registration.flash_bytes,
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
        prepared = resident.prepare(
            PrepareRequest(
                session=token,
                job_id="public-trial",
                test=Action.DERBY_FIXED,
                flash_job_id="public-flash",
            )
        )
        assert prepared["plan"]["expected_firmware_sha256"] == chain.flash["expected_elf_sha256"]
        assert wire.writes == []
        assert (
            PlanApprovals(resident).validate_current(prepared["plan_hash"], resident.session)
            == prepared["plan"]
        )
        changed = []
        preflight_complete = []
        original_validate = PlanApprovals.validate_current

        def validate_then_arm(approvals, *args, **kwargs):
            result = original_validate(approvals, *args, **kwargs)
            preflight_complete.append(True)
            return result

        monkeypatch.setattr(PlanApprovals, "validate_current", validate_then_arm)

        def valid(*args):
            if not changed and (
                (mutation_phase == "admission" and preflight_complete)
                or (mutation_phase == "final" and wire.writes and not wire.is_open)
            ):
                store.db.execute(
                    "UPDATE firmware_lkg SET completed_job=? WHERE device_id=?",
                    ("later-flash", "public-device"),
                )
                store.db.commit()
                changed.append(True)
            return True

        resident.principal_valid = valid
        execution = ExecuteRequest(session=token, plan_hash=prepared["plan_hash"])
        if mutation_phase == "admission":
            with pytest.raises(BrokerError):
                resident.execute(execution)
            assert changed and wire.writes == []
            assert store.job("public-trial") is None
            return
        admitted = resident.execute(execution)
        assert admitted["state"] == "queued"
        await resident.queue.join()
        if mutation_phase == "final":
            assert changed
            assert resident.job("public-trial")["state"] != "succeeded"
        else:
            assert resident.job("public-trial")["state"] == "succeeded"
        assert resident.settings.serial.expected_firmware_sha256 == configured_hash
        assert not wire.is_open
    finally:
        await resident.close()


def test_legacy_receipt_entry_binding_is_stable_without_admission_catalogue(chain):
    import cardputer_mcp.diagnostic_registry as registry

    original = prepare_collector(chain.resident, "public-flash", "owner")
    binding = original["collector_binding"]
    expected = registry.diagnostic_entry(chain.manifest["source_provenance"]).sha256
    assert binding["diagnostic_entry_sha256"] == expected
    assert "diagnostic_entry_sha256" not in chain.manifest
    legacy = {"test": "derby_fixed_workload", **copy.deepcopy(original)}
    legacy["collector_binding"].pop("diagnostic_entry_sha256")
    validate_collector_schema(legacy)
    assert collector_live(chain.resident, legacy, "owner")
    assert prepare_collector(chain.resident, "public-flash", "owner") == original
    assert collector_live(chain.resident, {"test": "derby_fixed_workload", **original}, "owner")
    changed = copy.deepcopy(original)
    changed["collector_binding"]["diagnostic_entry_sha256"] = "e" * 64
    assert not collector_live(chain.resident, {"test": "derby_fixed_workload", **changed}, "owner")


@pytest.mark.parametrize("commit", ["f" * 40, "7" * 40])
def test_compatible_new_commit_uses_owned_chain_without_broker_registration(chain, commit):
    from cardputer_mcp.diagnostic_registry import diagnostic_entry

    source = {**chain.build["source_provenance"], "commit": commit}
    entry = diagnostic_entry(source)
    for record in (chain.build, chain.manifest):
        record["source_provenance"] = source
        record["diagnostic_entry_sha256"] = entry.sha256
    build_hash = digest_plan(chain.build)
    chain.jobs["public-build"]["plan_hash"] = build_hash
    chain.plans[build_hash] = chain.build
    chain.manifest["build_plan_hash"] = build_hash
    chain.flash["manifest_sha256"] = manifest_digest(chain.manifest)
    flash_hash = digest_plan(chain.flash)
    chain.jobs["public-flash"]["plan_hash"] = flash_hash
    chain.plans[flash_hash] = chain.flash
    prepared = prepare_collector(chain.resident, "public-flash", "owner")
    assert prepared["collector_binding"]["source_commit"] == commit
    assert prepared["collector_binding"]["diagnostic_entry_sha256"] == entry.sha256
    plan = {"test": "derby_fixed_workload", **prepared}
    validate_collector_schema(plan)
    assert collector_live(chain.resident, plan, "owner")
    assert not collector_live(chain.resident, plan, "peer")
    plan["collector_binding"]["source_commit"] = "8" * 40
    assert not collector_live(chain.resident, plan, "owner")


def test_legacy_missing_digest_cannot_acquire_a_dma_axis(chain):
    prepared = prepare_collector(chain.resident, "public-flash", "owner")
    prepared["collector_binding"].pop("diagnostic_entry_sha256")
    prepared["collector_binding"]["derby_dma_mode"] = "ON"
    with pytest.raises(ValueError, match="diagnostic_entry_binding_required"):
        validate_collector_schema({"test": "derby_fixed_workload", **prepared})


@pytest.mark.parametrize("corrupt", [False, True])
def test_new_bound_build_manifest_and_collector_agree_on_entry(chain, corrupt):
    import cardputer_mcp.diagnostic_registry as registry

    entry_hash = registry.diagnostic_entry(chain.manifest["source_provenance"]).sha256
    chain.build["diagnostic_entry_sha256"] = entry_hash
    chain.manifest["diagnostic_entry_sha256"] = "e" * 64 if corrupt else entry_hash
    build_hash = digest_plan(chain.build)
    chain.jobs["public-build"]["plan_hash"] = build_hash
    chain.plans[build_hash] = chain.build
    chain.manifest["build_plan_hash"] = build_hash
    chain.flash["manifest_sha256"] = manifest_digest(chain.manifest)
    flash_hash = digest_plan(chain.flash)
    chain.jobs["public-flash"]["plan_hash"] = flash_hash
    chain.plans[flash_hash] = chain.flash
    if corrupt:
        with pytest.raises(ValueError):
            resolve_collector(chain.resident, "public-flash", "owner")
    else:
        assert (
            resolve_collector(chain.resident, "public-flash", "owner")["diagnostic_entry_sha256"]
            == entry_hash
        )


@pytest.mark.parametrize("damage", ["wrong_digest", "null_digest", "unregistered_legacy"])
def test_collector_history_rejects_invalid_entry_evidence(chain, damage):
    plan = {
        "test": "derby_fixed_workload",
        **prepare_collector(chain.resident, "public-flash", "owner"),
    }
    if damage == "unregistered_legacy":
        plan["collector_binding"].pop("diagnostic_entry_sha256")
        plan["collector_binding"]["source_commit"] = "f" * 40
    else:
        plan["collector_binding"]["diagnostic_entry_sha256"] = (
            None if damage == "null_digest" else "e" * 64
        )
    if damage == "wrong_digest":
        validate_collector_schema(plan)  # Syntactic shape, not a trusted source lookup.
    else:
        with pytest.raises(ValueError):
            validate_collector_schema(plan)
    assert not collector_live(chain.resident, plan, "owner")


@pytest.mark.parametrize("dma_axis", [None, "ON", "OFF", "wrong"])
def test_j1_collector_schema_rejects_present_dma_axis(chain, dma_axis):
    plan = {
        "test": "derby_fixed_workload",
        **prepare_collector(chain.resident, "public-flash", "owner"),
    }
    plan["collector_binding"]["derby_dma_mode"] = dma_axis
    if dma_axis in ("ON", "OFF"):
        validate_collector_schema(plan)  # Stored source/recipe semantics checked by live guard.
    else:
        with pytest.raises(ValueError):
            validate_collector_schema(plan)
    assert not collector_live(chain.resident, plan, "owner")


@pytest.mark.parametrize("dma_axis", [None, "ON", "OFF"])
def test_j1_receipt_cannot_acquire_dma_axis_even_when_hashes_rebound(chain, dma_axis):
    chain.build["derby_dma_mode"] = dma_axis
    chain.manifest["derby_dma_mode"] = dma_axis
    build_hash = digest_plan(chain.build)
    chain.jobs["public-build"]["plan_hash"] = build_hash
    chain.plans[build_hash] = chain.build
    chain.manifest["build_plan_hash"] = build_hash
    chain.flash["manifest_sha256"] = manifest_digest(chain.manifest)
    flash_hash = digest_plan(chain.flash)
    chain.jobs["public-flash"]["plan_hash"] = flash_hash
    chain.plans[flash_hash] = chain.flash
    with pytest.raises(ValueError):
        resolve_collector(chain.resident, "public-flash", "owner")
