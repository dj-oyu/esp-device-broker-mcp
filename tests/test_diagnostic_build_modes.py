"""Same-source build isolation and durable mode receipts; no SDK or hardware."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_build_artifact_persistence import DurableBuildArtifactTests, digest
from test_diagnostic_modeproof import image_fixture

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
    }
    node = SimpleNamespace(version="v22.0.0", executable=profile, sha256="b" * 64)
    with (
        patch.object(builder, "_project", return_value=(project, provenance)),
        patch.object(builder, "_registered_tools", return_value={"node": node}),
    ):
        on = builder.prepare("fixture", "owner", "a" * 64, "ON")
        off = builder.prepare("fixture", "owner", "a" * 64, "OFF")
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
        with pytest.raises(ValueError, match="allowlisted_source"):
            builder.prepare("fixture", "owner", "a" * 64, "ON")


@pytest.mark.parametrize("mode", ["ON", "OFF"])
@pytest.mark.parametrize("retention", ["restart", "expired"])
def test_modeproof_remains_verified_after_log_loss(mode, retention):
    case = DurableBuildArtifactTests()
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
        plan_hash = digest(plan)
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
