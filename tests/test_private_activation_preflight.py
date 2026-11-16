"""Generated read-only checks; source resolution is stubbed, native guard is separate."""

import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import test_private_build_binding as binding_fixture

from cardputer_mcp.private_activation_preflight import preflight_private_activation
from cardputer_mcp.private_input_store import PrivateInputReader
from cardputer_mcp.private_state_windows import verified_private_database


@pytest.fixture
def prepared():
    fixture = binding_fixture.PrivateBuildTests()
    fixture.setUp()
    try:
        builder = fixture.builder
        plan = builder.prepare("p", "owner", **fixture.args) | {
            "build_principal": "owner",
            "test": "firmware_build",
        }
        fixture.store_handle = SimpleNamespace(db=fixture.db)
        fixture.plan = plan
        fixture.events = []

        @contextmanager
        def guard(store):
            assert store is fixture.store_handle
            fixture.events.append("enter")
            try:
                yield
            finally:
                fixture.events.append("exit")

        fixture.kwargs = dict(principal="owner", authorized=lambda: True, state_guard=guard)
        # An accidental call to the production factory would mutate schema.
        builder.private_store_factory = lambda: pytest.fail("factory must not be invoked")
        yield fixture
    finally:
        fixture.doCleanups()


def run(f, **overrides):
    return preflight_private_activation(f.builder, f.store_handle, f.plan, **(f.kwargs | overrides))


def test_read_only_real_reprepare_with_same_connection(prepared, monkeypatch):
    f = prepared
    sql = []
    f.db.set_trace_callback(sql.append)
    original = PrivateInputReader.resolve

    def resolve(reader, **kwargs):
        assert reader.connection is f.db
        assert f.events == ["enter"]
        return original(reader, **kwargs)

    monkeypatch.setattr(PrivateInputReader, "resolve", resolve)
    before = f.db.total_changes
    summary = run(f)
    assert summary == {
        "status": "preflight_checked",
        "runtime": "not_installed",
        "activation": "not_granted",
    }
    assert sql and all(statement.lstrip().upper().startswith("SELECT ") for statement in sql)
    assert before == f.db.total_changes
    assert f.events == ["enter", "exit"]
    assert f.builder.private_runtime is None


def test_default_guard_native():
    assert preflight_private_activation.__kwdefaults__["state_guard"] is verified_private_database


@pytest.mark.parametrize("change", ["recipe", "owner", "binding", "fingerprint", "missing_schema"])
def test_changed_or_unowned_inputs_fail(prepared, change):
    f = prepared
    if change == "recipe":
        f.project.trusted_profile.write_text("# changed generated profile")
    elif change == "owner":
        f.plan["build_principal"] = "other"
        f.kwargs["principal"] = "other"
    elif change == "binding":
        f.plan["private_binding_sha256"] = "f" * 64
    elif change == "fingerprint":
        f.plan["fingerprint"] = "f" * 64
    else:
        f.db.execute("DROP TABLE private_input_receipts")
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        run(f)
    assert f.events == ["enter", "exit"]
    assert f.builder.private_runtime is None


@pytest.mark.parametrize("mode", ["active", "uncertain", "output", "installed", "caller", "denied"])
def test_not_idle_or_authorized_refused_before_native_guard(prepared, mode):
    f = prepared
    if mode in ("active", "uncertain"):
        if mode == "active":
            # active is a read-only derived property; use an explicit process fixture.
            f.builder.process = SimpleNamespace(
                active=True, uncertain=False, stop_requested=f.builder.process.stop_requested
            )
        else:
            f.builder.process.uncertain = True
    elif mode == "output":
        f.builder.output_uncertain = True
    elif mode == "installed":
        f.builder.private_runtime = object()
    elif mode == "caller":
        f.kwargs["principal"] = "other"
    else:
        f.kwargs["authorized"] = lambda: False
    with pytest.raises(ValueError):
        run(f)
    assert f.events == []


def test_cancel_during_guard_and_reprepare_closes_scope(prepared, monkeypatch):
    f = prepared
    original = f.builder._reprepare

    def reprepare(*args, **kwargs):
        fresh = original(*args, **kwargs)
        f.builder.request_stop()
        return fresh

    monkeypatch.setattr(f.builder, "_reprepare", reprepare)
    with pytest.raises(Exception, match="build_interrupted_needs_review"):
        run(f)
    assert f.events == ["enter", "exit"]
    assert f.builder.private_runtime is None


def test_guard_cancellation_prevents_any_receipt_read(prepared):
    f = prepared
    sql = []
    f.db.set_trace_callback(sql.append)

    @contextmanager
    def guard(_):
        f.builder.request_stop()
        f.events.append("enter")
        try:
            yield
        finally:
            f.events.append("exit")

    with pytest.raises(Exception, match="build_interrupted_needs_review"):
        run(f, state_guard=guard)
    assert sql == []
    assert f.events == ["enter", "exit"]
