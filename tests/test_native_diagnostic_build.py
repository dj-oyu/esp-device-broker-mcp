"""Source-bound native diagnostic selection and actual Kconfig evidence; host only."""

from unittest.mock import patch

import pytest
from test_dma_build_modes import build_fixture

from cardputer_mcp.build_locations import build_directory
from cardputer_mcp.diagnostic_registry import same_diagnostic_selection
from cardputer_mcp.models import PrepareRequest
from cardputer_mcp.native_diagnostic_build import ERROR, seed, verify
from cardputer_mcp.surface import SUBMIT_RULES, SubmitRequest, _check


@pytest.mark.parametrize("mode", ["ON", "OFF"])
def test_typed_selector_requires_owned_source_build_shape(mode):
    request = dict(
        job_id="native", test="firmware_build", project_id="fixture", source_receipt="a" * 64
    )
    assert PrepareRequest(**request, native_diagnostic_mode=mode).native_diagnostic_mode == mode
    for changed in (
        {"source_receipt": None},
        {"test": "firmware_fetch_source", "source_commit": "b" * 40},
    ):
        with pytest.raises(ValueError):
            PrepareRequest(**(request | changed), native_diagnostic_mode=mode)
    for invalid in (None, True, "-DOTHER=ON", "on"):
        with pytest.raises(ValueError):
            PrepareRequest(**request, native_diagnostic_mode=invalid)
    submitted = SubmitRequest(
        kind="build",
        job_id="native",
        project_id="fixture",
        source_receipt="a" * 64,
        native_diagnostic_mode=mode,
    )
    assert _check(submitted, "kind", *SUBMIT_RULES[submitted.kind]) is None


def test_selector_isolates_fingerprint_and_output_and_preserves_ordinary_plan(tmp_path):
    builder, project, source, node = build_fixture(tmp_path)
    with (
        patch.object(builder, "_project", return_value=(project, source)),
        patch.object(builder, "_registered_tools", return_value={"node": node}),
    ):
        ordinary = builder.prepare("fixture", "owner", "a" * 64)
        plans = [
            builder.prepare("fixture", "owner", "a" * 64, native_diagnostic_mode=m)
            for m in ("ON", "OFF")
        ]
        assert "native_diagnostic_mode" not in ordinary
        assert len({p["fingerprint"] for p in [ordinary, *plans]}) == 3
        assert (
            len(
                {
                    build_directory(
                        builder.settings.state_dir, project.build_root, p["fingerprint"], "e" * 64
                    )
                    for p in [ordinary, *plans]
                }
            )
            == 3
        )
        assert same_diagnostic_selection(plans[0], plans[0])
        assert not same_diagnostic_selection(plans[0], plans[1])
        assert not same_diagnostic_selection(plans[0], ordinary)
        source["commit"] = "f" * 40
        assert (
            builder.prepare("fixture", "owner", "a" * 64, native_diagnostic_mode="ON")[
                "fingerprint"
            ]
            != plans[0]["fingerprint"]
        )
        with pytest.raises(ValueError):
            builder.prepare("fixture", "owner", native_diagnostic_mode="ON")


@pytest.mark.parametrize("selected", ["ON", "OFF"])
def test_config_seed_is_fixed_and_actual_post_build_config_is_hashed(tmp_path, selected):
    seed(tmp_path, selected)
    before = verify(tmp_path, selected)
    seed(tmp_path, selected)
    assert verify(tmp_path, selected) == before
    with (tmp_path / "sdkconfig").open("a") as output:
        output.write("CONFIG_OTHER_FEATURE=y\n")
    assert verify(tmp_path, selected)["sha256"] != before["sha256"]
    other = "OFF" if selected == "ON" else "ON"
    with pytest.raises(ValueError, match=ERROR):
        verify(tmp_path, other)
    with pytest.raises(ValueError, match=ERROR):
        seed(tmp_path, other)


@pytest.mark.parametrize(
    "raw",
    [
        "CONFIG_OTHER=y\n",
        "CONFIG_KSN_DEVICE_PROBE=y\nCONFIG_KSN_DEVICE_PROBE=y\n",
        "CONFIG_KSN_DEVICE_PROBE=n\n",
    ],
)
def test_config_must_contain_one_explicit_requested_symbol(tmp_path, raw):
    (tmp_path / "sdkconfig").write_text(raw)
    with pytest.raises(ValueError, match=ERROR):
        verify(tmp_path, "ON")


def test_config_link_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("CONFIG_KSN_DEVICE_PROBE=y\n")
    try:
        (tmp_path / "sdkconfig").symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    with pytest.raises(ValueError, match=ERROR):
        seed(tmp_path, "ON")


def test_config_hardlink_is_refused(tmp_path):
    import os

    outside = tmp_path / "outside"
    outside.write_text("CONFIG_KSN_DEVICE_PROBE=y\n")
    os.link(outside, tmp_path / "sdkconfig")
    with pytest.raises(ValueError, match=ERROR):
        verify(tmp_path, "ON")


def test_native_selector_cannot_defer_receipt_admission():
    with pytest.raises(ValueError, match="native_diagnostic_requires_source_build"):
        PrepareRequest(
            job_id="native-after-fetch",
            test="firmware_build",
            project_id="fixture",
            after="01ABCDEF",
            native_diagnostic_mode="ON",
        )


@pytest.mark.parametrize("selected", ["ON", "OFF"])
@pytest.mark.parametrize("deferred", [False, True])
def test_full_host_build_pipeline_binds_generated_config(selected, deferred):
    from test_firmware_build import FirmwareVerticalTests

    FirmwareVerticalTests()._exercise_build(
        source_mode=True,
        node_mode="registered",
        native_mode=selected,
        defer_native=deferred,
        cache_mode="on",
    )


@pytest.mark.parametrize("late", [False, True])
def test_full_host_pipeline_refuses_mutation_before_manifest_or_final_commit(late):
    from test_firmware_build import FirmwareVerticalTests

    FirmwareVerticalTests()._exercise_build(
        source_mode=True,
        node_mode="registered",
        native_mode="ON",
        native_config_failure=not late,
        defer_native=late,
        late_native_mutation=late,
    )


@pytest.mark.parametrize("phase", ["fingerprint", "artifact"])
def test_selected_build_cancellation_never_publishes(phase):
    from test_firmware_build import FirmwareVerticalTests

    FirmwareVerticalTests()._exercise_build(
        source_mode=True, node_mode="registered", native_mode="ON", cancel_at=phase
    )


@pytest.mark.parametrize("authority", [(False,), (True, False), "cancelled"])
def test_selected_final_commit_requires_current_authority_and_no_cancel(authority):
    from test_firmware_build import FirmwareVerticalTests

    FirmwareVerticalTests()._exercise_build(
        source_mode=True,
        node_mode="registered",
        native_mode="ON",
        defer_native=True,
        final_native_authority=authority,
    )


def test_compiler_cache_namespace_keeps_each_explicit_mode_separate(tmp_path):
    from cardputer_mcp.build_ccache import configure_ccache

    build = tmp_path / "output"
    build.mkdir()
    legacy = {"derby_detail_mode": None, "derby_dma_mode": None}
    namespaces = []
    for selected in ({}, {"native_diagnostic_mode": "ON"}, {"native_diagnostic_mode": "OFF"}):
        receipt = configure_ccache(
            {"enabled": True},
            {},
            tmp_path,
            build,
            "fixture",
            "alice",
            "e" * 64,
            legacy | selected,
        )
        namespaces.append(receipt["namespace"])
    assert len(set(namespaces)) == 3


def test_non_deferred_publication_rechecks_config_after_initial_verification():
    from test_firmware_build import FirmwareVerticalTests

    FirmwareVerticalTests()._exercise_build(
        source_mode=True,
        node_mode="registered",
        native_mode="ON",
        native_candidate_mutation=True,
    )


@pytest.mark.parametrize("operation", ["open", "fsync"])
def test_seed_io_failures_are_fixed_without_paths(tmp_path, monkeypatch, operation):
    import cardputer_mcp.native_diagnostic_build as native

    def fail(*args, **kwargs):
        raise OSError("PRIVATE synthetic path")

    monkeypatch.setattr(native.os, operation, fail)
    with pytest.raises(ValueError, match="^" + ERROR + "$"):
        seed(tmp_path, "ON")


@pytest.mark.parametrize(
    "data",
    [b"\xff", b"", b"a" * (1024 * 1024 + 1)],
    ids=["invalid_utf8", "empty", "oversized"],
)
def test_invalid_config_data_is_bounded_and_fixed(tmp_path, data):
    (tmp_path / "sdkconfig").write_bytes(data)
    with pytest.raises(ValueError, match="^" + ERROR + "$"):
        verify(tmp_path, "ON")


def test_config_growth_between_fstat_and_read_is_refused(tmp_path, monkeypatch):
    import cardputer_mcp.native_diagnostic_build as native

    seed(tmp_path, "ON")
    original = native.os.fdopen

    class GrowingReader:
        def __init__(self, fd, mode):
            self.inner = original(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def fileno(self):
            return self.inner.fileno()

        def read(self, limit):
            with (tmp_path / "sdkconfig").open("ab") as writer:
                writer.write(b"extra")
            return self.inner.read(limit)

    monkeypatch.setattr(native.os, "fdopen", GrowingReader)
    with pytest.raises(ValueError, match="^" + ERROR + "$"):
        verify(tmp_path, "ON")


def test_seed_write_failure_is_fixed_and_descriptor_is_closed(tmp_path, monkeypatch):
    import cardputer_mcp.native_diagnostic_build as native

    original = native.os.fdopen
    streams = []

    class BrokenWriter:
        def __init__(self, fd, mode):
            self.inner = original(fd, mode)
            streams.append(self.inner)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()

        def write(self, *args):
            raise OSError("PRIVATE synthetic path")

    monkeypatch.setattr(native.os, "fdopen", BrokenWriter)
    with pytest.raises(ValueError, match="^" + ERROR + "$"):
        seed(tmp_path, "ON")
    assert len(streams) == 1 and streams[0].closed
