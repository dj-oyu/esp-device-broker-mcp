"""Synthetic adapter/worker shutdown, not broker startup integration."""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_private_store_session import fixture as fixture

from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_runtime import PrivateBuildRuntime
from cardputer_mcp.private_runtime_session import PrivateRuntimeSession
from cardputer_mcp.private_store_session import PrivateStoreSession


@pytest.fixture
def setup(fixture):
    path, events, count, guard = fixture
    session = PrivateStoreSession(path, path_guard=guard)
    session.__enter__()
    builder = FirmwareBuild(SimpleNamespace(build_projects={}))
    runtime = PrivateBuildRuntime(
        lambda *_: pytest.fail("producer ran"),
        PrivateArtifactStore(session.store.db),
        operation_guard=lambda _: nullcontext(),
    )
    wrapper = PrivateRuntimeSession(builder, session, runtime)
    try:
        yield builder, session, runtime, wrapper, count
    finally:
        # Fixture-only recovery: no real child ever exists in these tests.
        builder.process.active = builder.process.uncertain = False
        builder.process.cleanup_pending = builder.output_uncertain = False
        builder.process.owner = None
        runtime.running = False
        runtime._scope_release_failed = False
        if wrapper._installed and not wrapper._closed:
            wrapper.close()
        else:
            session.close()


def test_install_and_shutdown_closes_then_restores(setup):
    builder, session, runtime, wrapper, count = setup
    assert builder.private_runtime is None
    with wrapper:
        assert builder.private_runtime is runtime
        assert builder.private_store_factory is not None
    assert builder.private_runtime is None and builder.private_store_factory is None
    assert count[0] == 0 and builder.process.stop_requested.is_set()
    assert not runtime._accepting


@pytest.mark.parametrize("state", ["running", "active", "uncertain", "output", "cleanup", "owner"])
def test_unjoined_retains_pin_and_stops_new_admission(setup, state):
    builder, session, runtime, wrapper, count = setup
    wrapper.__enter__()
    if state == "running":
        runtime.running = True
    elif state == "output":
        builder.output_uncertain = True
    elif state == "owner":
        builder.process.owner = object()
    else:
        setattr(builder.process, "cleanup_pending" if state == "cleanup" else state, True)
    with pytest.raises(ValueError, match="join_unconfirmed"):
        wrapper.close()
    assert count[0] == 1 and builder.private_runtime is runtime
    with pytest.raises(ValueError, match="not_available"):
        _ = session.receipt_factory
    # Even a later public begin-like reset cannot reopen private admission.
    builder.process.stop_requested.clear()
    with pytest.raises(ValueError, match="runtime_stopping"):
        runtime.begin_operation(builder, None, {})
    with pytest.raises(ValueError, match="runtime_stopping"):
        runtime.execute(builder, {})
    with pytest.raises(ValueError, match="runtime_stopping"):
        runtime.commit(builder, None, {}, lambda: True)


def test_pending_and_operation_discard_before_store_close(setup):
    builder, session, runtime, wrapper, count = setup
    wrapper.__enter__()
    runtime.begin_operation(builder, session.store, {"job_id": "synthetic"})
    runtime.pending["synthetic"] = ("generated", object())
    wrapper.close()
    assert not runtime.pending and runtime._operation is None
    assert count[0] == 0


def test_ownership_change_does_not_restore_or_close(setup):
    builder, session, runtime, wrapper, count = setup
    wrapper.__enter__()
    builder.private_runtime = None
    with pytest.raises(ValueError, match="ownership_changed"):
        wrapper.close()
    assert count[0] == 1
    builder.private_runtime = runtime  # generated fixture restoration only


def test_unknown_operation_cleanup_retains_guard(setup):
    builder, session, runtime, wrapper, count = setup
    wrapper.__enter__()
    runtime._scope_release_failed = True
    with pytest.raises(ValueError, match="state_release_uncertain"):
        wrapper.close()
    assert count[0] == 1 and builder.private_runtime is runtime


def test_outer_session_exit_cannot_unpin_unjoined_supervisor(setup):
    builder, session, runtime, wrapper, count = setup
    wrapper.__enter__()
    builder.process.active = True
    with pytest.raises(ValueError, match="join_unconfirmed"):
        wrapper.close()
    with pytest.raises(ValueError, match="supervisor_active"):
        session.__exit__(None, None, None)
    assert count[0] == 1 and builder.private_runtime is runtime


def test_close_failure_retry_does_not_release_supervisor_twice(setup, monkeypatch):
    builder, session, runtime, wrapper, count = setup
    wrapper.__enter__()
    close = session.close
    monkeypatch.setattr(session, "close", lambda: (_ for _ in ()).throw(RuntimeError("fixture")))
    with pytest.raises(RuntimeError, match="fixture"):
        wrapper.close()
    assert wrapper._shutdown_hold is None
    assert count[0] == 1 and builder.private_runtime is runtime
    monkeypatch.setattr(session, "close", close)
    wrapper.close()
    assert count[0] == 0 and builder.private_runtime is None
