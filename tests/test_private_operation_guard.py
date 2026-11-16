"""Generated state-scope lifetime tests; native ACL proof is separate."""

from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest
from test_private_build_finalize import fixture as fixture

from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_build_runtime import PrivateBuildCandidate, PrivateBuildRuntime
from cardputer_mcp.private_state_windows import verified_private_database


def setup(fixture, guard):
    _, store, artifacts, args, _ = fixture
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))
    builder._reprepare = lambda *_: dict(args["plan"])
    runtime = PrivateBuildRuntime(
        lambda *_: PrivateBuildCandidate("e" * 64, args["files"]),
        artifacts,
        operation_guard=guard,
    )
    builder.private_runtime = runtime
    return builder, store, runtime, args


def test_default_guard_is_native_and_unopened_execute_is_denied(fixture):
    _, _, artifacts, args, _ = fixture
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))
    runtime = PrivateBuildRuntime(lambda *_: pytest.fail("producer started"), artifacts)
    assert runtime.operation_guard is verified_private_database
    with pytest.raises(ValueError, match="operation_unavailable"):
        runtime.execute(builder, args["plan"])


def test_scope_held_through_execution_and_atomic_commit(fixture):
    events = []

    @contextmanager
    def guard(store):
        events.append("enter")
        try:
            yield
        finally:
            events.append(store.job("fixture")["state"])

    builder, store, runtime, args = setup(fixture, guard)
    runtime.begin_operation(builder, store, args["plan"])
    assert events == ["enter"]
    runtime.execute(builder, args["plan"])
    assert events == ["enter"]
    runtime.commit(builder, store, args["plan"], args["live"])
    assert events == ["enter", "succeeded"]
    assert runtime._operation is None


def test_uncertain_child_retains_guard_until_explicit_joined_recovery(fixture):
    events = []

    @contextmanager
    def guard(_):
        events.append("enter")
        try:
            yield
        finally:
            events.append("close")

    builder, store, runtime, args = setup(fixture, guard)
    runtime.begin_operation(builder, store, args["plan"])
    builder.process.uncertain = True
    runtime.discard("fixture")
    assert events == ["enter"] and builder.output_uncertain
    with pytest.raises(ValueError, match="child_not_joined"):
        runtime.release_after_join()
    builder.process.uncertain = False
    assert events == ["enter"]  # no automatic reopening or release
    runtime.release_after_join()
    assert events == ["enter", "close"] and builder.output_uncertain


def test_wrong_store_or_changed_plan_never_runs(fixture):
    builder, store, runtime, args = setup(fixture, lambda _: nullcontext())
    with pytest.raises(ValueError, match="finalizer_invalid"):
        runtime.begin_operation(builder, SimpleNamespace(db=object()), args["plan"])
    runtime.begin_operation(builder, store, args["plan"])
    with pytest.raises(ValueError, match="operation_unavailable"):
        runtime.execute(builder, args["plan"] | {"fingerprint": "f" * 64})
    runtime.discard("fixture")


def test_release_failure_does_not_undo_commit_or_silently_clear_latch(fixture):
    @contextmanager
    def guard(_):
        yield
        raise RuntimeError("synthetic close failure")

    builder, store, runtime, args = setup(fixture, guard)
    runtime.begin_operation(builder, store, args["plan"])
    runtime.execute(builder, args["plan"])
    receipt = runtime.commit(builder, store, args["plan"], args["live"])
    assert store.job("fixture")["state"] == "succeeded"
    assert runtime.artifacts.resolve(receipt=receipt, principal="owner") == args["files"]
    assert builder.output_uncertain and runtime._operation is not None
    with pytest.raises(ValueError, match="release_uncertain"):
        runtime.release_after_join()


def test_guard_entry_failure_leaves_no_operation(fixture):
    @contextmanager
    def guard(_):
        raise ValueError("synthetic invalid database ACL")
        yield  # pragma: no cover

    builder, store, runtime, args = setup(fixture, guard)
    with pytest.raises(ValueError, match="invalid database ACL"):
        runtime.begin_operation(builder, store, args["plan"])
    assert runtime._operation is None and not runtime.pending and not runtime.running


def test_discard_joined_candidate_closes_scope_without_publication(fixture):
    events = []

    @contextmanager
    def guard(_):
        try:
            yield
        finally:
            events.append("close")

    builder, store, runtime, args = setup(fixture, guard)
    runtime.begin_operation(builder, store, args["plan"])
    runtime.execute(builder, args["plan"])
    builder.request_stop("fixture")
    runtime.discard("fixture")
    assert events == ["close"] and runtime._operation is None and not runtime.pending
    assert store.job("fixture")["state"] == "running"
    assert store.db.execute("SELECT COUNT(*) FROM private_artifact_receipts").fetchone()[0] == 0


def test_guard_callback_starting_child_prevents_operation_admission(fixture):
    holder = {}

    @contextmanager
    def guard(_):
        holder["builder"].process.uncertain = True
        yield

    builder, store, runtime, args = setup(fixture, guard)
    holder["builder"] = builder
    with pytest.raises(ValueError, match="child_not_joined"):
        runtime.begin_operation(builder, store, args["plan"])
    assert runtime._operation is not None and builder.output_uncertain
    builder.process.uncertain = False
    runtime.release_after_join()
    assert runtime._operation is None
