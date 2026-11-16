from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from cardputer_mcp.build_artifacts import BuildArtifacts
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_build_output import PRIVATE_MANIFEST_KEYS, PrivateBuildLogs


def private_plan():
    return {
        "job_id": "synthetic-job",
        "build_principal": "owner",
        "private_input_id": "fixture",
        "private_input_receipt": "a" * 64,
        "private_binding_sha256": "b" * 64,
    }


def test_runtime_selects_private_logs_before_any_child_output():
    builder = FirmwareBuild(SimpleNamespace())
    logs = builder._output_logs(private_plan(), "owner")
    assert isinstance(logs, PrivateBuildLogs)
    logs.durable = Mock()
    logs.append("compiler error: synthetic private model excerpt")
    assert list(logs.entries) == []
    logs.durable.append.assert_not_called()
    assert type(builder._output_logs({"job_id": "public"}, "owner")) is BuildLogs


def test_runtime_private_cache_never_probes_and_keeps_disable_flags():
    builder = FirmwareBuild(SimpleNamespace())
    run = Mock()
    env = {"IDF_CCACHE_ENABLE": "1", "ccache_remote_storage": "synthetic", "PATH": "trusted"}
    with patch("cardputer_mcp.firmware_build.probe_ccache") as probe:
        policy = builder._cache_for_plan(private_plan(), env, run, Path("unused"))
        probe.assert_not_called()
        run.assert_not_called()
        assert policy["enabled"] is False
        assert policy["reason"] == "private_inputs"
        assert env["CCACHE_DISABLE"] == "1"
        assert env["IDF_CCACHE_ENABLE"] == "0"
        assert "ccache_remote_storage" not in env
        builder._cache_for_plan({}, env, run, Path("unused"))
        probe.assert_called_once()


@pytest.mark.parametrize("marker", sorted(PRIVATE_MANIFEST_KEYS))
def test_general_artifact_store_cannot_publish_private_outputs(tmp_path, marker):
    store = BuildArtifacts()
    with pytest.raises(ValueError, match="invalid_internal_artifact_receipt"):
        store.publish("job", "owner", tmp_path, {"flash_authorized": False, marker: None})
    assert store._records == {}
    # Defense for a previously restored/malformed in-memory record, too.
    store._records["job"] = ("owner", tmp_path, {"flash_authorized": False, marker: {}})
    with pytest.raises(ValueError, match="owned_build_artifact_not_available"):
        store.resolve("job", "owner")


def test_ordinary_artifact_store_unchanged(tmp_path):
    store = BuildArtifacts()
    manifest = {"flash_authorized": False, "fingerprint": "a" * 64}
    store.publish("job", "owner", tmp_path, manifest)
    assert store.resolve("job", "owner") == (tmp_path, manifest)
    with pytest.raises(ValueError):
        store.resolve("job", "other")


def test_private_execution_gate_is_still_closed():
    builder = FirmwareBuild(SimpleNamespace())
    with patch.object(builder, "_execute_inner") as run:
        with pytest.raises(ValueError, match="workspace_unavailable"):
            builder._execute(private_plan(), lambda _: None)
        run.assert_not_called()
