"""Two-axis receipt-chain isolation; all compatible commits use actual image bindings."""

import copy
import hashlib

import pytest
from test_collector_binding import chain as chain
from test_dma_modeproof import DMA_ENTRY, dma_image_fixture, resign

from cardputer_mcp import diagnostic_registry as registry
from cardputer_mcp.broker import digest_plan
from cardputer_mcp.build_artifacts import manifest_digest
from cardputer_mcp.collector_binding import (
    collector_live,
    prepare_collector,
    resolve_collector,
    validate_collector_schema,
)
from cardputer_mcp.diagnostic_dma_modeproof import collect_dma_modeproof
from cardputer_mcp.manifest_export import make_manifest_export, manifest_export_sha256


def rebind(chain):
    """Model durable rehashing so corruption tests reach semantic receipt guards."""
    build_hash = digest_plan(chain.build)
    chain.jobs["public-build"]["plan_hash"] = build_hash
    chain.plans[build_hash] = chain.build
    chain.manifest["build_plan_hash"] = build_hash
    chain.flash["manifest_sha256"] = manifest_digest(chain.manifest)
    flash_hash = digest_plan(chain.flash)
    chain.jobs["public-flash"]["plan_hash"] = flash_hash
    chain.plans[flash_hash] = chain.flash


@pytest.fixture
def make_dma_chain(chain, tmp_path):

    def make(detail="OFF", dma="ON"):
        provenance, artifacts, files = dma_image_fixture(tmp_path, detail, dma)
        provenance["source_receipt"] = "c" * 64
        envelope = collect_dma_modeproof(tmp_path, detail, dma, provenance, artifacts)
        for record in (chain.build, chain.manifest):
            record.update(
                source_provenance=provenance,
                derby_detail_mode=detail,
                derby_dma_mode=dma,
                diagnostic_entry_sha256=DMA_ENTRY.sha256,
            )
        chain.manifest.update(diagnostic_modeproof=envelope, artifacts=artifacts)
        chain.files.clear()
        chain.files.update(files)
        elf = hashlib.sha256(files["cardputer_pocketjs.elf"]).hexdigest()
        app = hashlib.sha256(files["cardputer_pocketjs.bin"]).hexdigest()
        chain.flash.update(expected_elf_sha256=elf, expected_app_bin_sha256=app)
        chain.metadata.update(expected_elf_sha256=elf, expected_app_bin_sha256=app)
        rebind(chain)
        return chain

    return make


@pytest.mark.parametrize("detail", ["ON", "OFF"])
@pytest.mark.parametrize("dma", ["ON", "OFF"])
def test_four_axis_choices_bind_to_verified_retained_flash(make_dma_chain, detail, dma):
    chain = make_dma_chain(detail, dma)
    details = prepare_collector(chain.resident, "public-flash", "owner")
    binding = details["collector_binding"]
    assert binding["source_commit"] == DMA_ENTRY.commit
    assert binding["derby_detail_mode"] == detail
    assert binding["derby_dma_mode"] == dma
    assert binding["diagnostic_entry_sha256"] == DMA_ENTRY.sha256
    assert binding["build_plan_hash"] == chain.jobs["public-build"]["plan_hash"]
    assert binding["flash_plan_hash"] == chain.jobs["public-flash"]["plan_hash"]
    assert binding["modeproof_sha256"] == chain.manifest["diagnostic_modeproof"]["proof_sha256"]
    plan = {"test": "derby_fixed_workload", **details}
    validate_collector_schema(plan)
    assert collector_live(chain.resident, plan, "owner")
    # Retained receipt selection does not adopt a prior configured image expectation.
    assert details["expected_firmware_sha256"] == chain.flash["expected_elf_sha256"]
    assert (
        details["expected_firmware_sha256"]
        != chain.resident.settings.serial.expected_firmware_sha256
    )


@pytest.mark.parametrize("detail", ["ON", "OFF"])
@pytest.mark.parametrize("dma", ["ON", "OFF"])
@pytest.mark.parametrize("damage", ["none", "wrong", "null", "bool"])
def test_collector_rechecks_present_export_commitment(make_dma_chain, detail, dma, damage):
    chain = make_dma_chain(detail, dma)
    chain.manifest["schema_version"] = 1
    rebind(chain)
    export = make_manifest_export(chain.manifest)
    chain.flash["manifest_export_sha256"] = {
        "none": manifest_export_sha256(export),
        "wrong": "0" * 64,
        "null": None,
        "bool": True,
    }[damage]
    rebind(chain)
    if damage != "none":
        with pytest.raises(ValueError):
            resolve_collector(chain.resident, "public-flash", "owner")
        return
    details = prepare_collector(chain.resident, "public-flash", "owner")
    binding = details["collector_binding"]
    assert binding["manifest_sha256"] == export["original_manifest_sha256"]
    assert binding["flash_plan_hash"] == digest_plan(chain.flash)
    plan = {"test": "derby_fixed_workload", **details}
    assert collector_live(chain.resident, plan, "owner")
    chain.flash.pop("manifest_export_sha256")
    # Removing the commitment changes the fixed trusted flash plan hash.
    assert not collector_live(chain.resident, plan, "owner")


@pytest.mark.parametrize("record", ["build", "manifest"])
@pytest.mark.parametrize("axis", ["derby_detail_mode", "derby_dma_mode"])
@pytest.mark.parametrize("damage", ["missing", "null", "opposite"])
def test_build_manifest_axes_must_match_even_after_rehash(make_dma_chain, record, axis, damage):
    chain = make_dma_chain()
    target = getattr(chain, record)
    if damage == "missing":
        target.pop(axis)
    else:
        target[axis] = None if damage == "null" else ("ON" if target[axis] == "OFF" else "OFF")
    rebind(chain)
    with pytest.raises(ValueError):
        resolve_collector(chain.resident, "public-flash", "owner")


@pytest.mark.parametrize("damage", ["missing", "null", "wrong", "opposite"])
def test_collector_dma_axis_has_no_legacy_default(make_dma_chain, damage):
    chain = make_dma_chain()
    plan = {
        "test": "derby_fixed_workload",
        **prepare_collector(chain.resident, "public-flash", "owner"),
    }
    if damage == "missing":
        plan["collector_binding"].pop("derby_dma_mode")
    else:
        plan["collector_binding"]["derby_dma_mode"] = {
            "null": None,
            "wrong": "automatic",
            "opposite": "OFF",
        }[damage]
    if damage in {"null", "wrong"}:
        with pytest.raises(ValueError):
            validate_collector_schema(plan)
    else:
        # Shape alone cannot establish which recipe the stored build requested.
        validate_collector_schema(plan)
    assert not collector_live(chain.resident, plan, "owner")


@pytest.mark.parametrize("record", ["build", "manifest", "collector"])
@pytest.mark.parametrize("damage", ["missing", "null", "different"])
def test_new_contract_entry_digest_is_required_at_every_boundary(make_dma_chain, record, damage):
    chain = make_dma_chain()
    plan = {
        "test": "derby_fixed_workload",
        **prepare_collector(chain.resident, "public-flash", "owner"),
    }
    target = plan["collector_binding"] if record == "collector" else getattr(chain, record)
    if damage == "missing":
        target.pop("diagnostic_entry_sha256")
    else:
        target["diagnostic_entry_sha256"] = None if damage == "null" else "e" * 64
    if record == "collector":
        if damage == "different":
            validate_collector_schema(plan)
        else:
            with pytest.raises(ValueError):
                validate_collector_schema(plan)
        assert not collector_live(chain.resident, plan, "owner")
    else:
        rebind(chain)
        with pytest.raises(ValueError):
            resolve_collector(chain.resident, "public-flash", "owner")


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_receipt", "e" * 64),
        ("source_commit", registry.J1_COMMIT),
        ("device_id", "other-device"),
        ("principal_id", "other-principal"),
        ("build_job_id", "other-build"),
        ("firmware_receipt", "e" * 64),
        ("manifest_sha256", "e" * 64),
        ("expected_app_bin_sha256", "e" * 64),
    ],
)
def test_callback_free_binding_rejects_other_source_image_device_or_owner(
    make_dma_chain, field, value
):
    chain = make_dma_chain()
    plan = {
        "test": "derby_fixed_workload",
        **prepare_collector(chain.resident, "public-flash", "owner"),
    }
    plan["collector_binding"][field] = value
    assert not collector_live(chain.resident, plan, "owner")


@pytest.mark.parametrize("damage", ["image", "cache", "contract", "downgrade", "axes"])
def test_receipt_proof_rejects_modified_images_cache_contract_and_downgrade(make_dma_chain, damage):
    chain = make_dma_chain()
    envelope = chain.manifest["diagnostic_modeproof"]
    if damage == "image":
        chain.files["cardputer_pocketjs.bin"] += b"changed"
    elif damage == "cache":
        envelope["cmake_cache"] = envelope["cmake_cache"].replace(
            "KASANE_DMA_OVERLAP:BOOL=ON", "KASANE_DMA_OVERLAP:BOOL=OFF"
        )
    elif damage == "contract":
        envelope["proof"]["proof_contract"] = "j1-fixed-v1"
        resign(envelope)
    elif damage == "axes":
        envelope["requested_dma_mode"] = "OFF"
    else:
        nested = copy.deepcopy(envelope["proof"]["diagnostic"])
        chain.manifest["diagnostic_modeproof"] = {
            "requested_mode": "OFF",
            "proof": nested,
            "proof_sha256": digest_plan(nested),
        }
    rebind(chain)
    with pytest.raises(ValueError):
        resolve_collector(chain.resident, "public-flash", "owner")


@pytest.mark.parametrize("target", ["source", "principal", "device", "failed_flash"])
def test_dma_receipt_rejects_wrong_source_principal_device_or_unsuccessful_flash(
    make_dma_chain, target
):
    chain = make_dma_chain()
    if target == "source":
        chain.build["source_provenance"]["commit"] = "f" * 40
        rebind(chain)
    elif target == "device":
        chain.resident.settings.serial = chain.resident.settings.serial.model_copy(
            update={
                "selector": chain.resident.settings.serial.selector.model_copy(
                    update={"serial_number": "OTHER-SYNTHETIC"}
                )
            }
        )
    elif target == "failed_flash":
        chain.jobs["public-flash"]["state"] = "failed"
    with pytest.raises(ValueError):
        resolve_collector(
            chain.resident, "public-flash", "other-principal" if target == "principal" else "owner"
        )
