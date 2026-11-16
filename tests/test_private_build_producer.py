"""Generated orchestration tests; native reader/scratch validated separately."""

import hashlib
import shutil
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from cardputer_mcp import private_build_producer as module
from cardputer_mcp.private_build_output import PrivateBuildLogs
from cardputer_mcp.private_input_contract import InputFile


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    paths = []
    registration = SimpleNamespace(source_commit="a" * 40, recipe_sha256="b" * 64)
    builder = SimpleNamespace(
        settings=SimpleNamespace(
            build_projects={"fixture": SimpleNamespace(private_inputs={"fixture": registration})}
        ),
        process=SimpleNamespace(active=False, uncertain=False, stop_requested=threading.Event()),
        output_uncertain=False,
    )
    plan = dict(project_id="fixture", private_input_id="fixture", build_principal="owner")

    def check(*_):
        if builder.process.stop_requested.is_set():
            raise ValueError("synthetic cancellation")

    monkeypatch.setattr(module, "_recheck", check)

    @contextmanager
    def scratch(*_, **kwargs):
        path = tmp_path / str(len(paths))
        path.mkdir()
        paths.append(path)
        try:
            yield path
        finally:
            shutil.rmtree(path)

    @contextmanager
    def stage(*_, **kwargs):
        with scratch() as path:
            (path / "source.txt").write_bytes(b"generated source")
            yield SimpleNamespace(directory=path, composition_identity="e" * 64)

    def read(reg, manifest, **kwargs):
        for item in manifest:
            data = (reg.local_root / item.path).read_bytes()
            if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
                raise ValueError("synthetic changed output")
            yield item.path, data

    monkeypatch.setattr(module, "stage_private_build", stage)

    @contextmanager
    def output_scope(*args, **kwargs):
        with scratch(*args, **kwargs) as path:

            def read_outputs(manifest, **read_kwargs):
                return read(SimpleNamespace(local_root=path), manifest, **read_kwargs)

            yield SimpleNamespace(path=path, read_outputs=read_outputs)

    monkeypatch.setattr(module, "materialize_windows_workspace_scope", output_scope)
    return builder, plan, paths, tmp_path


def compile_fixture(source, output, logs):
    assert source != output
    assert (source / "source.txt").read_bytes() == b"generated source"
    assert not (output / "source.txt").exists()
    data = b"generated firmware"
    (output / "fixture.bin").write_bytes(data)
    return (InputFile("fixture.bin", len(data), hashlib.sha256(data).hexdigest()),)


def test_separate_roots_capture_and_cleanup(fixture):
    builder, plan, paths, container = fixture
    producer = module.NativePrivateProducer(builder, container, compile_fixture)
    result = producer(plan, PrivateBuildLogs("owner"))
    assert result.files == (("fixture.bin", b"generated firmware"),)
    assert len(paths) == 2 and all(not p.exists() for p in paths)
    assert producer.retained is None


def test_active_child_retains_roots_until_explicit_join(fixture):
    builder, plan, paths, container = fixture

    def compile_active(*args):
        result = compile_fixture(*args)
        builder.process.active = True
        return result

    producer = module.NativePrivateProducer(builder, container, compile_active)
    with pytest.raises(ValueError, match="child_not_joined"):
        producer(plan, PrivateBuildLogs("owner"))
    assert all(p.exists() for p in paths)
    assert builder.output_uncertain
    with pytest.raises(ValueError, match="child_not_joined"):
        producer.release_after_join()
    builder.process.active = False
    producer.release_after_join()
    assert all(not p.exists() for p in paths)
    assert builder.output_uncertain  # recovery must establish other latches


@pytest.mark.parametrize("failure", ["cancel", "changed"])
def test_failure_never_returns_candidate_and_cleans_joined_roots(fixture, failure):
    builder, plan, paths, container = fixture

    def compile_fail(source, output, logs):
        result = compile_fixture(source, output, logs)
        if failure == "cancel":
            builder.process.stop_requested.set()
        else:
            (output / "fixture.bin").write_bytes(b"changed")
        return result

    producer = module.NativePrivateProducer(builder, container, compile_fail)
    with pytest.raises(ValueError):
        producer(plan, PrivateBuildLogs("owner"))
    assert all(not p.exists() for p in paths)


def test_existing_child_blocks_new_workspace_and_compiler(fixture):
    builder, plan, paths, container = fixture
    builder.process.active = True
    producer = module.NativePrivateProducer(
        builder, container, lambda *_: pytest.fail("compiler started")
    )
    with pytest.raises(ValueError, match="child_not_joined"):
        producer(plan, PrivateBuildLogs("owner"))
    assert paths == []


def test_stop_accepted_during_recheck_never_starts_compiler(fixture, monkeypatch):
    builder, plan, paths, container = fixture
    count = 0

    def recheck(*_):
        nonlocal count
        count += 1
        if count == 2:
            builder.process.stop_requested.set()

    monkeypatch.setattr(module, "_recheck", recheck)
    producer = module.NativePrivateProducer(
        builder, container, lambda *_: pytest.fail("compiler started")
    )
    with pytest.raises(ValueError, match="cancelled"):
        producer(plan, PrivateBuildLogs("owner"))
    assert all(not p.exists() for p in paths)


def test_default_compiler_callback_uses_same_plan_and_private_roots(fixture):
    builder, plan, paths, container = fixture
    seen = []

    def compile(plan_snapshot, source, output, logs):
        seen.append(plan_snapshot)
        return compile_fixture(source, output, logs)

    builder.compile_private_staged = compile
    producer = module.NativePrivateProducer(builder, container)
    assert producer(plan, PrivateBuildLogs("owner")).files == (
        ("fixture.bin", b"generated firmware"),
    )
    assert seen == [plan]
    assert all(not path.exists() for path in paths)
