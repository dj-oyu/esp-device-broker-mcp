"""Synthetic child calls only; does not validate Windows or actual ESP-IDF."""

import json
import os
from types import SimpleNamespace

import pytest

from cardputer_mcp import firmware_build as module
from cardputer_mcp.private_build_output import PrivateBuildLogs


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    system, idf, python_env = (tmp_path / p for p in ("windows", "idf", "python"))
    compiler = tmp_path / "xtensa-esp32s3-elf-gcc.exe"
    for path in (
        system / "System32/WindowsPowerShell/v1.0/powershell.exe",
        idf / "tools/idf.py",
        python_env / "Scripts/python.exe",
        compiler,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic executable not run")
    project = SimpleNamespace(target="esp32s3", trusted_profile=tmp_path / "profile.ps1")
    builder = module.FirmwareBuild(SimpleNamespace(build_projects={}))
    plan = dict(
        job_id="fixture",
        project_id="fixture",
        build_principal="owner",
        private_input_id="fixture",
        private_input_receipt="a" * 64,
        private_binding_sha256="b" * 64,
        fingerprint="c" * 64,
    )
    monkeypatch.setattr(builder, "_reprepare", lambda *_: dict(plan))
    monkeypatch.setattr(builder, "_project", lambda *_: (project, {}))
    monkeypatch.setattr(
        module, "os", SimpleNamespace(name="nt", environ={"SYSTEMROOT": str(system)}, walk=os.walk)
    )
    monkeypatch.setattr(
        module.shutil,
        "which",
        lambda name, **_: str(compiler) if name.startswith("xtensa") else None,
    )
    calls, monitors = [], []
    builder._authority_monitor = lambda: monitors.append(True)
    for name in ("claim_build_directory", "configure_ccache", "probe_ccache", "ccache_statistics"):
        monkeypatch.setattr(module, name, lambda *_, **__: pytest.fail("public output/cache used"))

    def run(args, env, cwd, emit, **kwargs):
        calls.append(args)
        assert cwd == source
        assert env["CCACHE_DISABLE"] == "1" and env["IDF_CCACHE_ENABLE"] == "0"
        assert env["TEMP"] == env["TMP"] == str(output / "broker-private-temp")
        assert kwargs["monitor"] is builder._authority_monitor
        if "-e" in args:
            emit(f"IDF_PATH={idf}", "stdout")
            emit(f"IDF_PYTHON_ENV_PATH={python_env}", "stdout")
            emit(f"PATH={tmp_path}", "stdout")
            emit("IDF_CCACHE_ENABLE=1", "stdout")
        elif args[-1] == "build":
            assert "--no-ccache" in args and args[args.index("-B") + 1] == str(output)
            assert "SDKCONFIG=" + str(output / "sdkconfig") in args
            (output / "project_description.json").write_text(json.dumps({"target": "esp32s3"}))
            (output / "fixture.bin").write_bytes(b"generated firmware")
            emit("synthetic private diagnostics", "stderr")
        else:
            emit("ESP-IDF v6.0.1" if args[-2].endswith("idf.py") else "fixture version", "stdout")
        return 0

    monkeypatch.setattr(builder.process, "run", run)
    return builder, plan, source, output, calls, monitors


def test_staged_compile_uses_private_roots_without_publication(fixture):
    builder, plan, source, output, calls, monitors = fixture
    logs = PrivateBuildLogs("owner")
    result = builder.compile_private_staged(plan, source, output, logs)
    assert [item.path for item in result] == ["fixture.bin"]
    assert len(calls) == 5 and len(monitors) >= len(calls) * 2
    assert not builder.manifests and not builder._pending and builder._build_lock is None
    assert not list(source.iterdir())
    assert not (output / "broker-manifest.json").exists()
    assert builder.private_runtime is None


@pytest.mark.parametrize("reason", ["cancel", "authority", "fingerprint", "monitor"])
def test_preflight_failure_never_starts_child(fixture, monkeypatch, reason):
    builder, plan, source, output, calls, _ = fixture
    if reason == "cancel":
        builder.request_stop("fixture")
    elif reason == "authority":

        def deny():
            raise ValueError("synthetic authority denied")

        builder._authority_monitor = deny
    elif reason == "monitor":
        builder._authority_monitor = None
    else:
        monkeypatch.setattr(builder, "_reprepare", lambda *_: plan | {"fingerprint": "d" * 64})
    with pytest.raises(ValueError):
        builder.compile_private_staged(plan, source, output, PrivateBuildLogs("owner"))
    assert calls == []


def test_active_child_latches_without_touching_roots(fixture, monkeypatch):
    builder, plan, source, output, calls, _ = fixture
    monkeypatch.setattr(builder.process, "active", True)
    with pytest.raises(ValueError, match="child_not_joined"):
        builder.compile_private_staged(plan, source, output, PrivateBuildLogs("owner"))
    assert calls == [] and builder.output_uncertain


def test_monitor_changed_child_state_blocks_launch(fixture):
    builder, plan, source, output, calls, _ = fixture
    builder._authority_monitor = lambda: setattr(builder.process, "uncertain", True)
    with pytest.raises(ValueError, match="child_not_joined"):
        builder.compile_private_staged(plan, source, output, PrivateBuildLogs("owner"))
    assert not calls and builder.output_uncertain


def test_cancellation_after_profile_blocks_next_child(fixture, monkeypatch):
    builder, plan, source, output, calls, _ = fixture
    original = builder.process.run

    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        builder.request_stop("fixture")
        return result

    monkeypatch.setattr(builder.process, "run", run)
    with pytest.raises(ValueError):
        builder.compile_private_staged(plan, source, output, PrivateBuildLogs("owner"))
    assert len(calls) == 1
