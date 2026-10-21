"""Same-source build isolation and durable mode receipts; no SDK or hardware."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_build_artifact_persistence as persistence
from test_diagnostic_modeproof import image_fixture

import cardputer_mcp.diagnostic_registry as registry
from cardputer_mcp.build_locations import build_directory
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.diagnostic_modeproof import J1_COMMIT, collect_modeproof
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.models import BuildLogRequest, PrepareRequest


def test_same_head_modes_have_distinct_fingerprints_directories_and_fixed_argv(tmp_path):
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
    settings = SimpleNamespace(build_projects={"fixture": project}, state_dir=tmp_path / "state")
    builder = FirmwareBuild(settings)
    provenance = {
        "repository": "dj-oyu/cardputer-adv-pocketjs",
        "commit": J1_COMMIT,
        "source_receipt": "a" * 64,
        "tree_sha1": "b" * 40,
        "source_sha256": "c" * 64,
    }
    node = SimpleNamespace(version="v22.0.0", executable=profile, sha256="b" * 64)
    with (
        patch.object(builder, "_project", return_value=(project, provenance)),
        patch.object(builder, "_registered_tools", return_value={"node": node}),
    ):
        on = builder.prepare("fixture", "owner", "a" * 64, "ON")
        off = builder.prepare("fixture", "owner", "a" * 64, "OFF")
        assert on["diagnostic_entry_sha256"] == off["diagnostic_entry_sha256"]
        assert on["source_provenance"]["source_sha256"] == off["source_provenance"]["source_sha256"]
        for mode, plan in (("ON", on), ("OFF", off)):
            assert plan["derby_detail_mode"] == mode
            assert plan["argv_template"][-3:] == [
                "-D",
                "POCKET_DERBY_DETAIL_MEASURE=" + mode,
                "build",
            ]
            assert "SDKCONFIG=build-output-sdkconfig" in plan["argv_template"]
        on_dir = build_directory(
            settings.state_dir, project.build_root, on["fingerprint"], "e" * 64
        )
        off_dir = build_directory(
            settings.state_dir, project.build_root, off["fingerprint"], "e" * 64
        )
        assert on_dir != off_dir
        on_dir.mkdir(parents=True)
        (on_dir / "CMakeCache.txt").write_text("POCKET_DERBY_DETAIL_MEASURE:BOOL=ON")
        (on_dir / "sdkconfig").write_text("stale config")
        assert not off_dir.exists()
        assert builder.prepare("fixture", "owner", "a" * 64, "OFF") == off
        provenance["commit"] = "f" * 40
        sibling = builder.prepare("fixture", "owner", "a" * 64, "ON")
        assert sibling["source_provenance"]["commit"] == "f" * 40
        assert sibling["fingerprint"] != on["fingerprint"]


@pytest.mark.parametrize("mode", ["ON", "OFF"])
@pytest.mark.parametrize("retention", ["restart", "expired"])
@pytest.mark.parametrize("record_format", ["legacy", "bound"])
def test_modeproof_remains_verified_after_log_loss(mode, retention, record_format):
    case = persistence.DurableBuildArtifactTests()
    case.setUp()
    try:
        root, manifest = case.publish(finish=False, publish=False)
        provenance, artifacts, _ = image_fixture(root, mode)
        manifest.update(
            source_provenance=provenance,
            derby_detail_mode=mode,
            diagnostic_modeproof=collect_modeproof(root, mode, provenance, artifacts),
            artifacts=artifacts,
        )
        job = case.store.job("build-0")
        plan = case.store.plan(job["plan_hash"])
        plan.update(derby_detail_mode=mode, source_provenance=provenance)
        if record_format == "bound":
            entry_hash = registry.diagnostic_entry(provenance).sha256
            manifest["diagnostic_entry_sha256"] = entry_hash
            plan["diagnostic_entry_sha256"] = entry_hash
        plan_hash = persistence.digest(plan)
        case.store.put_plan(plan_hash, plan)
        case.store.db.execute("UPDATE jobs SET plan_hash=? WHERE id=?", (plan_hash, "build-0"))
        case.store.db.commit()
        manifest["build_plan_hash"] = plan_hash
        (root / "broker-manifest.json").write_text(json.dumps(manifest))
        case.builder.artifacts.publish("build-0", "alice", root, manifest)
        case.store.set_job("build-0", "succeeded", "firmware_build_succeeded_no_flash")
        case.restart()
        if retention == "expired":
            case.builder.logs["build-0"] = BuildLogs("alice")
            case.builder.logs["build-0"].deadline = 0
        result = case.builder.read_logs(
            BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
        )
        assert result["expired"]
        assert result["artifact_status"] == "verified"
        assert result["manifest"]["diagnostic_modeproof"] == manifest["diagnostic_modeproof"]
        (root / "diagnostic-mode-proof.json").unlink()
        result = case.builder.read_logs(
            BuildLogRequest(job_id="build-0"), "alice", project_id="fixture"
        )
        assert result["artifact_status"] == "unavailable"
        assert "manifest" not in result
    finally:
        case.doCleanups()


def test_mode_input_is_enum_and_confined_to_registered_source_build():
    base = {
        "job_id": "build",
        "test": "firmware_build",
        "project_id": "fixture",
        "source_receipt": "a" * 64,
    }
    for mode in ("ON", "OFF"):
        assert PrepareRequest(**base, derby_detail_mode=mode).derby_detail_mode == mode
    for mode in ("-DANY=1", "on", "ON;echo unsafe", True):
        with pytest.raises(ValueError):
            PrepareRequest(**base, derby_detail_mode=mode)
    with pytest.raises(ValueError):
        PrepareRequest(job_id="build", test="firmware_build", derby_detail_mode="OFF")
    with pytest.raises(ValueError):
        PrepareRequest(**base, flash_job_id="flash")


@pytest.mark.parametrize("target", ["reference", "manifest"])
def test_explicit_null_is_not_a_legacy_receipt_field(target):
    case = persistence.DurableBuildArtifactTests()
    case.setUp()
    try:
        root, manifest = case.publish()
        if target == "reference":
            row = case.store.db.execute(
                "SELECT payload FROM build_artifact_references WHERE job_id=?", ("build-0",)
            ).fetchone()
            reference = json.loads(row[0])
            reference["diagnostic_entry_sha256"] = None
            case.store.db.execute(
                "UPDATE build_artifact_references SET payload=? WHERE job_id=?",
                (json.dumps(reference), "build-0"),
            )
            case.store.db.commit()
        else:
            manifest["diagnostic_entry_sha256"] = None
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
