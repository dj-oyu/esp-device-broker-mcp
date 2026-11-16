"""Explicit startup injection with generated DB and synthetic native guard only."""

from contextlib import contextmanager

import pytest

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.private_store_session import PrivateStoreSession
from cardputer_mcp.store import Store


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    path = state / "history.sqlite3"
    path.touch()
    pins = []
    sessions = []

    @contextmanager
    def guard(p):
        assert p == path
        pins.append("pin")
        try:
            yield (1, b"generated")
        finally:
            pins.append("unpin")

    def factory(p, on_commit):
        session = PrivateStoreSession(p, path_guard=guard, on_commit=on_commit)
        sessions.append(session)
        return session

    broker = Broker(
        Settings(state_dir=state),
        lock_path=tmp_path / "device.lock",
        private_store_session_factory=factory,
    )
    return broker, pins, sessions


async def test_pins_before_store_open_preserves_callback_and_final_close(setup, monkeypatch):
    broker, pins, sessions = setup
    observed = []
    initialize = Store.__init__

    def init(store, *args, **kwargs):
        assert pins == ["pin"]
        assert kwargs["on_commit"] == broker.operator_changes.notify
        initialize(store, *args, **kwargs)
        observed.append(store)

    monkeypatch.setattr(Store, "__init__", init)
    await broker.start()
    assert broker.store is observed[0] is sessions[0].store
    assert broker.private_runtime_session is None and broker.firmware_build.private_runtime is None
    assert pins.count("pin") - pins.count("unpin") == 1
    await broker.close()
    assert pins.count("pin") == pins.count("unpin")
    assert sessions[0]._closed and broker.store is None


async def test_idle_start_failure_closes_once_and_rejects_restart(setup, monkeypatch):
    broker, pins, sessions = setup
    closes = []
    close = Store.close

    def shutdown(store):
        closes.append(store)
        close(store)

    monkeypatch.setattr(Store, "close", shutdown)
    monkeypatch.setattr(Store, "recover", lambda _: (_ for _ in ()).throw(RuntimeError("fixture")))
    with pytest.raises(RuntimeError, match="fixture"):
        await broker.start()
    assert len(closes) == 1 and sessions[0]._closed and broker.store is None
    assert pins.count("pin") == pins.count("unpin")
    assert not broker._state_lock.is_locked and not broker._device_lock.is_locked
    with pytest.raises(ValueError, match="broker_already_running"):
        await broker.start()
    await broker.close()
    assert len(closes) == 1


async def test_started_worker_failure_retains_pin_until_explicit_close(setup, monkeypatch):
    broker, pins, sessions = setup
    monkeypatch.setattr(
        broker.profile_jobs, "start", lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
    )
    with pytest.raises(RuntimeError, match="fixture"):
        await broker.start()
    assert broker.worker is not None and broker.store is not None
    assert broker._private_store_session is sessions[0]
    assert pins.count("pin") - pins.count("unpin") == 1
    assert broker._state_lock.is_locked and broker._device_lock.is_locked
    with pytest.raises(ValueError, match="broker_already_running"):
        await broker.start()
    await broker.close()
    assert sessions[0]._closed and broker.store is None


async def test_unknown_child_start_failure_keeps_reachable_pin(setup, monkeypatch):
    broker, pins, sessions = setup

    def fail(_):
        broker.firmware_build.process.uncertain = True
        raise RuntimeError("fixture")

    monkeypatch.setattr(Store, "recover", fail)
    with pytest.raises(RuntimeError, match="fixture"):
        await broker.start()
    assert broker.store is not None and broker._private_store_session is sessions[0]
    with pytest.raises(ValueError, match="device_close_unconfirmed"):
        await broker.close()
    assert not sessions[0]._closed and broker._state_lock.is_locked
    broker.firmware_build.process.uncertain = False  # synthetic only; no real process exists
    await broker.close()
    assert pins.count("pin") == pins.count("unpin")


async def test_factory_cannot_claim_an_already_open_session(setup):
    broker, _, sessions = setup
    factory = broker._private_store_session_factory
    existing = factory(
        broker.settings.state_dir / "history.sqlite3", broker.operator_changes.notify
    )
    existing.__enter__()
    broker._private_store_session_factory = lambda *_: existing
    try:
        with pytest.raises(ValueError, match="ownership_changed"):
            await broker.start()
        assert broker.store is None and broker._private_store_session is None
        assert not existing._closed
        existing.store.db.execute("SELECT 1")
    finally:
        existing.close()


async def test_partial_store_close_failure_has_explicit_reachable_retry(setup, monkeypatch):
    import sqlite3

    broker, pins, sessions = setup
    failing = [True]
    close = Store.close

    def initialize(store, path, **kwargs):
        store.db = sqlite3.connect(path)
        raise RuntimeError("fixture init failure")

    def shutdown(store):
        if failing[0]:
            raise RuntimeError("fixture close failure")
        close(store)

    monkeypatch.setattr(Store, "__init__", initialize)
    monkeypatch.setattr(Store, "close", shutdown)
    with pytest.raises(RuntimeError, match="close failure"):
        await broker.start()
    assert broker.store is None and broker._private_store_session is sessions[0]
    assert broker._state_lock.is_locked and pins.count("pin") > pins.count("unpin")
    with pytest.raises(RuntimeError, match="close failure"):
        await broker.close()
    assert broker._private_store_session is sessions[0]
    failing[0] = False  # Recover the synthetic close fault, not an unknown OS child.
    await broker.close()
    assert broker._private_store_session is None and sessions[0]._closed
    assert pins.count("pin") == pins.count("unpin")
    assert not broker._state_lock.is_locked and not broker._device_lock.is_locked
