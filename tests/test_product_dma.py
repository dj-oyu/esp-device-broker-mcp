"""Ordinary DMA builds need linked output evidence, never diagnostic producers or hardware."""

import copy
import hashlib
import json
from unittest.mock import patch

import pytest
import test_build_artifact_persistence as persistence
from test_dma_modeproof import dma_fixture

from cardputer_mcp.build_ccache import configure_ccache
from cardputer_mcp.models import PrepareRequest
from cardputer_mcp.product_dma import collect_dma_evidence, same_dma_selection


def ordinary_outputs(root, mode, **kwargs):
    source, _, files = dma_fixture(root, dma_mode=mode, **kwargs)
    source["source_receipt"] = "a" * 64
    # Remove both diagnostic symbols and their embedded strings from the synthetic ELF.
    elf = files["cardputer_pocketjs.elf"]
    for name in (b"df_detail_enabled", b"pocket_derby_comparison_hw_seed", b"DERBY_DETAIL_EXIT"):
        elf = elf.replace(name, b"x" * len(name))
    image = bytearray(files["cardputer_pocketjs.bin"])
    image[176:208] = hashlib.sha256(elf).digest()
    files = {"cardputer_pocketjs.bin": bytes(image), "cardputer_pocketjs.elf": elf}
    artifacts = []
    for name, raw in files.items():
        (root / name).write_bytes(raw)
        artifacts.append(
            {"name": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        )
    for name in ("diagnostic-mode-proof.json", "dma-diagnostic-mode-proof.json"):
        (root / name).unlink()
    (root / "CMakeCache.txt").write_text(f"KASANE_DMA_OVERLAP:BOOL={mode}\n")
    return source, artifacts


@pytest.mark.parametrize("mode", ["OFF", "ON"])
def test_ordinary_plan_schema_identity_and_cache_are_separate(tmp_path, mode):
    from test_dma_build_modes import build_fixture

    from cardputer_mcp.plan_approval import PlanApprovals

    builder, project, source, _ = build_fixture(tmp_path)
    request = PrepareRequest(
        job_id="build",
        test="firmware_build",
        project_id="fixture",
        source_receipt="a" * 64,
        dma_overlap_mode=mode,
    )
    assert request.model_dump(mode="json")["dma_overlap_mode"] == mode
    with patch.object(builder, "_project", return_value=(project, source)):
        plan = builder.prepare("fixture", "owner", "a" * 64, dma_overlap_mode=mode)
        ordinary = builder.prepare("fixture", "owner", "a" * 64)
        opposite = builder.prepare(
            "fixture", "owner", "a" * 64, dma_overlap_mode="ON" if mode == "OFF" else "OFF"
        )
        assert len({p["fingerprint"] for p in (plan, ordinary, opposite)}) == 3
        assert "diagnostic_entry_sha256" not in plan
        assert plan["argv_template"][-3:] == ["-D", "KASANE_DMA_OVERLAP=" + mode, "build"]
        assert not any("DETAIL" in arg for arg in plan["argv_template"])
        schema_plan = dict(
            version=1,
            broker_epoch="e" * 32,
            session_id="a" * 32,
            device="mock-cardputer",
            backend="mock",
            job_id="build",
            test="firmware_build",
            **plan,
        )
        PlanApprovals._schema(schema_plan)
        for key, value in (
            ("dma_overlap_mode", None),
            ("dma_overlap_mode", True),
            ("derby_detail_mode", "ON"),
            ("source_receipt", None),
            ("argv_template", ordinary["argv_template"]),
        ):
            changed = copy.deepcopy(schema_plan)
            changed[key] = value
            with pytest.raises((ValueError, TypeError)):
                PlanApprovals._schema(changed)
        policy = {"enabled": True}
        namespaces = []
        for index, selected in enumerate((mode, "ON" if mode == "OFF" else "OFF")):
            output = tmp_path / str(index)
            output.mkdir()
            receipt = configure_ccache(
                policy,
                {},
                tmp_path / "cache",
                output,
                "fixture",
                "owner",
                "e" * 64,
                {"dma_overlap_mode": selected},
            )
            namespaces.append(receipt["namespace"])
        assert namespaces[0] != namespaces[1]
        (project.project_dir / "CMakeLists.txt").write_text("changed source")
        assert (
            builder.prepare("fixture", "owner", "a" * 64, dma_overlap_mode=mode)["fingerprint"]
            != plan["fingerprint"]
        )


def test_ordinary_request_rejects_invalid_or_ambiguous_selection():
    fields = dict(job_id="b", test="firmware_build", project_id="fixture", source_receipt="a" * 64)
    for value in (None, True, False, "AUTO", "on", "-DANY=ON"):
        with pytest.raises(ValueError):
            PrepareRequest(**fields, dma_overlap_mode=value)
    for extra in (
        {"derby_detail_mode": "OFF"},
        {"derby_dma_mode": "ON"},
        {"source_receipt": None},
        {"test": "firmware_fetch_source"},
    ):
        with pytest.raises(ValueError):
            PrepareRequest(**(fields | extra), dma_overlap_mode="ON")
    assert "dma_overlap_mode" not in PrepareRequest(**fields).model_dump(mode="json")
    assert same_dma_selection({}, {})
    assert not same_dma_selection({"dma_overlap_mode": "OFF"}, {})
    assert not same_dma_selection({"dma_overlap_mode": None}, {"dma_overlap_mode": None})


@pytest.mark.parametrize("mode", ["OFF", "ON"])
def test_actual_outputs_verify_without_diagnostic_producers(tmp_path, mode):
    source, artifacts = ordinary_outputs(tmp_path, mode)
    evidence = collect_dma_evidence(tmp_path, mode, source, artifacts)
    assert evidence["mode"] == mode
    assert "cmake_cache" not in evidence
    assert "hardware_tested" not in evidence
    assert evidence["native_symbols"] == (
        ["board_present_finish", "board_present_submit"] if mode == "ON" else []
    )
    for cache in (
        "",
        "KASANE_DMA_OVERLAP:BOOL=AUTO\n",
        "KASANE_DMA_OVERLAP:UNINITIALIZED=" + mode,
        f"KASANE_DMA_OVERLAP:BOOL={mode}\nKASANE_DMA_OVERLAP:BOOL={mode}\n",
        "KASANE_DMA_OVERLAP:BOOL=" + ("ON" if mode == "OFF" else "OFF"),
    ):
        (tmp_path / "CMakeCache.txt").write_text(cache)
        with pytest.raises(ValueError, match="ordinary_dma_output_invalid"):
            collect_dma_evidence(tmp_path, mode, source, artifacts)


@pytest.mark.parametrize("mode", ["OFF", "ON"])
@pytest.mark.parametrize("damage", ["symbols", "elf", "bin", "pair", "receipt", "missing", "duplicate"])
def test_mismatched_linked_artifacts_are_rejected(tmp_path, mode, damage):
    kwargs = (
        {"native_symbols": () if mode == "ON" else ("board_present_submit",)}
        if damage == "symbols"
        else {}
    )
    source, artifacts = ordinary_outputs(tmp_path, mode, **kwargs)
    if damage in ("elf", "bin"):
        path = tmp_path / ("cardputer_pocketjs." + damage)
        path.write_bytes(path.read_bytes() + b"stale")
    elif damage == "pair":
        path = tmp_path / "cardputer_pocketjs.bin"
        raw = bytearray(path.read_bytes())
        raw[176:208] = bytes(32)
        path.write_bytes(raw)
        artifacts[0]["sha256"] = hashlib.sha256(raw).hexdigest()
    elif damage == "receipt":
        artifacts[0]["sha256"] = "f" * 64
    elif damage == "missing":
        (tmp_path / "cardputer_pocketjs.elf").unlink()
    elif damage == "duplicate":
        artifacts.append(dict(artifacts[0]))
    with pytest.raises(ValueError, match="ordinary_dma_output_invalid"):
        collect_dma_evidence(tmp_path, mode, source, artifacts)


@pytest.mark.parametrize("mode", ["OFF", "ON"])
def test_restart_rechecks_cache_and_owned_manifest_mode(tmp_path, mode):
    case = persistence.DurableBuildArtifactTests()
    case.setUp()
    try:
        root, manifest = case.publish(finish=False, publish=False)
        source, artifacts = ordinary_outputs(root, mode)
        evidence = collect_dma_evidence(root, mode, source, artifacts)
        job = case.store.job("build-0")
        plan = case.store.plan(job["plan_hash"])
        plan.update(dma_overlap_mode=mode, source_provenance=source)
        digest = persistence.digest(plan)
        case.store.put_plan(digest, plan)
        case.store.db.execute("UPDATE jobs SET plan_hash=? WHERE id=?", (digest, "build-0"))
        case.store.db.commit()
        manifest.update(
            build_plan_hash=digest,
            source_provenance=source,
            dma_overlap_mode=mode,
            dma_overlap_evidence=evidence,
            artifacts=artifacts,
        )
        changed = copy.deepcopy(manifest)
        changed["dma_overlap_mode"] = "ON" if mode == "OFF" else "OFF"
        with pytest.raises(ValueError, match="invalid_internal_artifact_receipt"):
            case.builder.artifacts.publish("build-0", "alice", root, changed)
        (root / "broker-manifest.json").write_text(json.dumps(manifest))
        case.builder.artifacts.publish("build-0", "alice", root, manifest)
        case.store.set_job("build-0", "succeeded", "firmware_build_succeeded_no_flash")
        case.restart()
        assert case.context() == (root, manifest)
        with pytest.raises(ValueError):
            case.context(owner="another-owner")
        (root / "CMakeCache.txt").write_text(f"KASANE_DMA_OVERLAP:BOOL={mode}\n# stale cache\n")
        with pytest.raises(ValueError, match="build_output_identity_mismatch"):
            case.context()
    finally:
        case.doCleanups()
