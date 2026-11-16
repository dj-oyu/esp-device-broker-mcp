from unittest.mock import Mock, patch

import pytest

from cardputer_mcp.manifest_export import public_manifest, validate_manifest_export
from cardputer_mcp.private_build_output import (
    PRIVATE_MANIFEST_KEYS,
    PrivateBuildLogs,
    no_private_compiler_cache,
    private_artifact_provenance,
)


def test_raw_output_never_reaches_any_sink():
    callback = Mock()
    logs = PrivateBuildLogs("owner", job_id="synthetic", on_change=callback)
    durable = Mock()
    logs.durable = durable
    with patch("cardputer_mcp.build_logs.console_line") as console:
        for text in (
            "error: SECRET_MODEL_BYTES",
            "-- path/private/model",
            "token=synthetic",
            "x" * 10000,
        ):
            logs.append(text, "stderr")
        assert list(logs.entries) == []
        assert logs.sequence == 0
        callback.assert_not_called()
        durable.append.assert_not_called()
        console.assert_not_called()
        logs.event("compiling")
        assert logs.entries[-1]["text"] == "Private build: compiling"
        durable.append.assert_called_once()
        console.assert_not_called()
    with pytest.raises(ValueError, match="event_invalid"):
        logs.event("SECRET_MODEL_BYTES")


def test_cache_policy_copies_and_removes_remote_or_local_cache_settings():
    env = {
        "PATH": "trusted",
        "IDF_CCACHE_ENABLE": "1",
        "idf_ccache_enable": "1",
        "CCACHE_REMOTE_STORAGE": "synthetic",
        "ccache_dir": "synthetic",
        "SCCACHE_BUCKET": "synthetic",
        "CMAKE_C_COMPILER_LAUNCHER": "ccache",
        "CMAKE_CXX_COMPILER_LAUNCHER": "sccache",
    }
    before = dict(env)
    result = no_private_compiler_cache(env)
    assert result == {
        "PATH": "trusted",
        "IDF_CCACHE_ENABLE": "0",
        "CCACHE_DISABLE": "1",
        "SCCACHE_DISABLE": "1",
    }
    assert env == before


def plan(owner="owner"):
    return {
        "build_principal": owner,
        "private_input_id": "private fixture name",
        "private_input_receipt": "a" * 64,
        "private_binding_sha256": "b" * 64,
        "source_receipt": "c" * 64,
        "fingerprint": "d" * 64,
    }


def test_artifact_namespace_is_owner_and_composition_bound():
    one = private_artifact_provenance(plan(), principal="owner", composition_identity="e" * 64)
    two = private_artifact_provenance(
        plan("other"), principal="other", composition_identity="e" * 64
    )
    three = private_artifact_provenance(plan(), principal="owner", composition_identity="f" * 64)
    assert len({r["output_namespace"] for r in (one, two, three)}) == 3
    assert "private fixture name" not in repr(one)
    assert one == private_artifact_provenance(
        plan(), principal="owner", composition_identity="e" * 64
    )
    with pytest.raises(ValueError, match="not_owned"):
        private_artifact_provenance(plan(), principal="other", composition_identity="e" * 64)


@pytest.mark.parametrize("key", sorted(PRIVATE_MANIFEST_KEYS))
@pytest.mark.parametrize("value", [None, {}, False, "synthetic"])
def test_public_manifest_rejects_even_malformed_private_markers(key, value):
    manifest = {"schema_version": 1, key: value}
    with pytest.raises(ValueError, match="invalid_manifest_export"):
        public_manifest(manifest)
    export = {
        "schema_version": 1,
        "contract": "broker-build-manifest-export-v1",
        "original_manifest_sha256": "a" * 64,
        "manifest": manifest,
    }
    with pytest.raises(ValueError, match="invalid_manifest_export"):
        validate_manifest_export(export)


def test_ordinary_public_manifest_projection_unchanged():
    manifest = {"schema_version": 1, "fingerprint": "a" * 64, "artifacts": []}
    assert public_manifest(manifest) == manifest
    assert public_manifest(manifest) is not manifest
