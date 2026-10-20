"""Two-axis preparation/durable tests use a test-only registry entry and no SDK."""

import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_build_artifact_persistence as persistence
from test_dma_modeproof import DMA_ENTRY, dma_fixture

import cardputer_mcp.diagnostic_registry as registry
from cardputer_mcp.build_locations import _json, build_directory
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.diagnostic_dma_modeproof import PROOF_NAME, collect_dma_modeproof
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.models import BuildLogRequest, PrepareRequest
from cardputer_mcp.plan_approval import PlanApprovals


def build_fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "CMakeLists.txt").write_text("project(synthetic)")
    profile = tmp_path / "profile.ps1"
    profile.write_text("synthetic profile")
    project = SimpleNamespace(
        project_dir=source,
        trusted_profile=profile,
        expected_idf="6.0.1",
        target="esp32s3",
        build_root=tmp_path / "output",
        build_min_free_bytes=0,
    )
    builder = FirmwareBuild(
        SimpleNamespace(
            build_projects={"fixture": project},
            state_dir=tmp_path / "state",
        )
    )
    provenance = {
        "repository": DMA_ENTRY.repository,
        "commit": DMA_ENTRY.commit,
        "source_receipt": "a" * 64,
        "tree_sha1": "b" * 40,
        "source_sha256": "c" * 64,
        "dependency_policy": "vendored_components_no_fetch",
    }
    node = SimpleNamespace(version="v22.0.0", executable=profile, sha256="b" * 64)
    return builder, project, provenance, node


def test_four_plans_bind_both_axes_and_isolate_cache_sdkconfig(tmp_path):
    builder, project, source, node = build_fixture(tmp_path)
    plans, directories = [], []
    with (
        patch.object(registry, "_ENTRIES", (*registry._ENTRIES, DMA_ENTRY)),
        patch.object(builder, "_project", return_value=(project, source)),
        patch.object(builder, "_registered_tools", return_value={"node": node}),
    ):
        for detail in ("ON", "OFF"):
            for dma in ("ON", "OFF"):
                plan = builder.prepare("fixture", "owner", "a" * 64, detail, dma)
                assert plan["diagnostic_entry_sha256"] == DMA_ENTRY.sha256
                assert plan["argv_template"][-5:] == [
                    "-D",
                    "POCKET_DERBY_DETAIL_MEASURE=" + detail,
                    "-D",
                    "KASANE_DMA_OVERLAP=" + dma,
                    "build",
                ]
                assert "SDKCONFIG=build-output-sdkconfig" in plan["argv_template"]
                directory = build_directory(
                    builder.settings.state_dir,
                    project.build_root,
                    plan["fingerprint"],
                    "e" * 64,
                )
                assert not directory.exists()
                directory.mkdir(parents=True)
                (directory / "CMakeCache.txt").write_text("stale other axis")
                (directory / "sdkconfig").write_text("stale other axis")
                plans.append(plan)
                directories.append(directory)
        assert len({p["fingerprint"] for p in plans}) == len(set(directories)) == 4
        unrelated = registry.DiagnosticEntry("fixture/other", "f" * 40)
        with patch.object(registry, "_ENTRIES", (*registry._ENTRIES, unrelated)):
            assert builder.prepare("fixture", "owner", "a" * 64, "ON", "ON") == plans[0]
        for detail, dma in ((None, None), ("ON", None), (None, "OFF"), (True, "OFF")):
            with pytest.raises(ValueError):
                builder.prepare("fixture", "owner", "a" * 64, detail, dma)
    # Neither the new source nor DMA on the original admitted J1 is enabled.
    assert registry._ENTRIES == (registry._J1_ENTRY,)
    with (
        patch.object(builder, "_project", return_value=(project, source)),
        patch.object(builder, "_registered_tools", return_value={"node": node}),
    ):
        with pytest.raises(ValueError):
            builder.prepare("fixture", "owner", "a" * 64, "ON", "OFF")
        source["commit"] = registry.J1_COMMIT
        with pytest.raises(ValueError):
            builder.prepare("fixture", "owner", "a" * 64, "ON", "OFF")


def test_request_and_approval_schema_require_explicit_typed_dma_axes(tmp_path):
    request = dict(
        job_id="build", test="firmware_build", project_id="fixture", source_receipt="a" * 64
    )
    source_request = PrepareRequest(
        job_id="source", test="firmware_fetch_source", project_id="fixture", source_commit="a" * 40
    )
    for ordinary in (source_request, PrepareRequest(**request)):
        payload = ordinary.model_dump(mode="json")
        assert "derby_dma_mode" not in payload
        assert PrepareRequest.model_validate(payload) == ordinary
    for detail in ("ON", "OFF"):
        for dma in ("ON", "OFF"):
            parsed = PrepareRequest(**request, derby_detail_mode=detail, derby_dma_mode=dma)
            assert parsed.derby_dma_mode == dma
    for bad in (None, True, False, "AUTO", "on", "-DANY=ON"):
        with pytest.raises(ValueError):
            PrepareRequest(**request, derby_detail_mode="ON", derby_dma_mode=bad)
    with pytest.raises(ValueError):
        PrepareRequest(**request, derby_dma_mode="ON")
    with pytest.raises(ValueError):
        PrepareRequest(
            job_id="build", test="firmware_build", derby_detail_mode="ON", derby_dma_mode="OFF"
        )
    builder, project, source, node = build_fixture(tmp_path)
    with (
        patch.object(registry, "_ENTRIES", (DMA_ENTRY,)),
        patch.object(builder, "_project", return_value=(project, source)),
        patch.object(builder, "_registered_tools", return_value={"node": node}),
    ):
        plan = dict(
            version=1,
            broker_epoch="e" * 32,
            session_id="a" * 32,
            device="mock-cardputer",
            backend="mock",
            job_id="build",
            test="firmware_build",
            **builder.prepare("fixture", "owner", "a" * 64, "ON", "OFF"),
        )
        PlanApprovals._schema(plan)
        for key in ("derby_detail_mode", "derby_dma_mode", "diagnostic_entry_sha256"):
            for value in (None, "missing", "bad"):
                changed = copy.deepcopy(plan)
                if value == "missing":
                    del changed[key]
                else:
                    changed[key] = value
                with pytest.raises((ValueError, TypeError)):
                    PlanApprovals._schema(changed)


@pytest.mark.parametrize("detail", ["ON", "OFF"])
@pytest.mark.parametrize("dma", ["ON", "OFF"])
def test_dma_durable_evidence_after_restart_and_log_expiry(detail, dma):
    case = persistence.DurableBuildArtifactTests()
    case.setUp()
    try:
        root, manifest = case.publish(finish=False, publish=False)
        source, artifacts, _ = dma_fixture(root, detail, dma)
        cache = (root / "CMakeCache.txt").read_bytes()
        cache += b"PRIVATE_PATH:PATH=/private/sentinel-source\nTOKEN:STRING=sentinel-secret-value\n"
        (root / "CMakeCache.txt").write_bytes(cache)
        producer = json.loads((root / PROOF_NAME).read_text())
        cache_hash = hashlib.sha256(cache).hexdigest()
        producer["dma_overlap"]["cmake_cache_sha256"] = cache_hash
        for key in ("build_mode", "build_measure"):
            producer["diagnostic"][key]["cmake_cache_sha256"] = cache_hash
        (root / PROOF_NAME).write_text(json.dumps(producer))
        with patch.object(registry, "_ENTRIES", (*registry._ENTRIES, DMA_ENTRY)):
            envelope = collect_dma_modeproof(root, detail, dma, source, artifacts)
            manifest.update(
                source_provenance=source,
                derby_detail_mode=detail,
                derby_dma_mode=dma,
                diagnostic_entry_sha256=DMA_ENTRY.sha256,
                diagnostic_modeproof=envelope,
                artifacts=artifacts,
            )
            job = case.store.job("build-0")
            plan = case.store.plan(job["plan_hash"])
            plan.update(
                derby_detail_mode=detail,
                derby_dma_mode=dma,
                source_provenance=source,
                diagnostic_entry_sha256=DMA_ENTRY.sha256,
            )
            digest = persistence.digest(plan)
            case.store.put_plan(digest, plan)
            case.store.db.execute("UPDATE jobs SET plan_hash=? WHERE id=?", (digest, "build-0"))
            case.store.db.commit()
            manifest["build_plan_hash"] = digest
            (root / "broker-manifest.json").write_text(json.dumps(manifest))
            case.builder.artifacts.publish("build-0", "alice", root, manifest)
            case.store.set_job("build-0", "succeeded", "firmware_build_succeeded_no_flash")
            case.restart()
            case.builder.logs["build-0"] = BuildLogs("alice")
            case.builder.logs["build-0"].deadline = 0
            result = case.builder.read_logs(
                BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
            )
            assert result["expired"] and result["artifact_status"] == "verified"
            public_envelope = result["manifest"]["diagnostic_modeproof"]
            assert "cmake_cache" not in public_envelope
            assert "sentinel-source" not in json.dumps(result)
            assert "sentinel-secret-value" not in json.dumps(result)
            assert public_envelope["proof"] == envelope["proof"]
            assert public_envelope["cache_evidence"]["retained_private"]
            assert case.context()[1]["diagnostic_modeproof"] == envelope
            for filename in ("CMakeCache.txt", PROOF_NAME):
                saved = (root / filename).read_bytes()
                (root / filename).unlink()
                result = case.builder.read_logs(
                    BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
                )
                assert result["artifact_status"] == "unavailable"
                (root / filename).write_bytes(saved)
    finally:
        case.doCleanups()


def test_large_cache_manifest_has_its_own_bound_without_relaxing_small_receipts(tmp_path):
    path = tmp_path / "broker-manifest.json"
    value = {"cache": "x" * 100000}
    path.write_text(json.dumps(value))
    assert _json(path, limit=1024 * 1024) == value
    with pytest.raises(ValueError):
        _json(path)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        _json(path, limit=1024 * 1024)


@pytest.mark.parametrize("key", ["derby_detail_mode", "derby_dma_mode"])
def test_j1_durable_manifest_null_axis_is_not_legacy_omission(key):
    from test_diagnostic_modeproof import collect_modeproof, image_fixture

    case = persistence.DurableBuildArtifactTests()
    case.setUp()
    try:
        root, manifest = case.publish(finish=False, publish=False)
        source, artifacts, _ = image_fixture(root, "ON")
        manifest.update(
            source_provenance=source,
            derby_detail_mode="ON",
            diagnostic_modeproof=collect_modeproof(root, "ON", source, artifacts),
            artifacts=artifacts,
        )
        job = case.store.job("build-0")
        plan = case.store.plan(job["plan_hash"])
        plan.update(derby_detail_mode="ON", source_provenance=source)
        digest = persistence.digest(plan)
        case.store.put_plan(digest, plan)
        case.store.db.execute("UPDATE jobs SET plan_hash=? WHERE id=?", (digest, "build-0"))
        case.store.db.commit()
        manifest["build_plan_hash"] = digest
        (root / "broker-manifest.json").write_text(json.dumps(manifest))
        case.builder.artifacts.publish("build-0", "alice", root, manifest)
        case.store.set_job("build-0", "succeeded", "firmware_build_succeeded_no_flash")
        case.restart()
        assert case.context()[1] == manifest
        manifest[key] = None
        (root / "broker-manifest.json").write_text(json.dumps(manifest))
        row = case.store.db.execute(
            "SELECT payload FROM build_artifact_references WHERE job_id=?", ("build-0",)
        ).fetchone()
        reference = json.loads(row[0])
        reference["manifest_sha256"] = persistence.digest(manifest)
        case.store.db.execute(
            "UPDATE build_artifact_references SET payload=? WHERE job_id=?",
            (json.dumps(reference), "build-0"),
        )
        case.store.db.commit()
        with pytest.raises(ValueError):
            case.context()
    finally:
        case.doCleanups()
