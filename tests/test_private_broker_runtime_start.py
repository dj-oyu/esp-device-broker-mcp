"""Explicit startup through real ProfileJobs using only generated private output.

Receipt/source preparation and native protection are synthetic boundaries. No IDF
process, real asset ingestion or production configuration is exercised.
"""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from test_private_broker_start import setup as setup
from test_profile_jobs import register

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.models import ExecuteRequest, PrepareRequest
from cardputer_mcp.private_artifact_store import PrivateArtifactStore
from cardputer_mcp.private_build_runtime import PrivateBuildCandidate, PrivateBuildRuntime
from cardputer_mcp.private_runtime_session import PrivateRuntimeSession


def install_factory(broker, *, change=None):
    made = []

    def factory(builder, session):
        assert broker.worker is None and broker.profile_jobs.worker is None
        assert broker._store() is session.store
        runtime = PrivateBuildRuntime(
            lambda *_: PrivateBuildCandidate("e" * 64, (("fixture.bin", b"generated-only"),)),
            PrivateArtifactStore(session.store.db),
            operation_guard=lambda _: nullcontext(),
        )
        wrapper = PrivateRuntimeSession(builder, session, runtime)
        made.append(wrapper)
        if change:
            change(wrapper)
        return wrapper

    broker._private_runtime_session_factory = factory
    return made


async def test_start_factory_to_real_profile_worker_atomic_output(setup, tmp_path, monkeypatch):
    broker, pins, sessions = setup
    register(SimpleNamespace(resident=broker), tmp_path)
    broker.principal_valid = lambda *_: True
    made = install_factory(broker)
    await broker.start()
    try:
        wrapper = made[0]
        assert broker.private_runtime_session is wrapper
        assert broker.firmware_build.private_runtime is wrapper.runtime
        prepared = broker.profile_jobs.prepare(
            PrepareRequest(job_id="generated-startup", test="firmware_build", project_id="fixture"),
            "owner",
            "local",
        )
        store = broker._store()
        plan = store.plan(prepared["plan_hash"]) | {
            "private_input_id": "synthetic",
            "private_input_receipt": "a" * 64,
            "private_binding_sha256": "b" * 64,
            "source_receipt": "c" * 64,
        }
        digest = digest_plan(plan)
        store.put_plan(digest, plan)
        monkeypatch.setattr(broker.firmware_build, "_reprepare", lambda *_: dict(plan))
        broker.profile_jobs.execute(ExecuteRequest(plan_hash=digest), "owner", "local")
        await asyncio.wait_for(broker.profile_jobs.queue.join(), 5)
        assert store.job(plan["job_id"])["state"] == "succeeded"
        receipt = wrapper.runtime.artifacts.receipt_for_job(
            job_id=plan["job_id"], principal="owner"
        )
        assert wrapper.runtime.artifacts.resolve(receipt=receipt, principal="owner") == (
            ("fixture.bin", b"generated-only"),
        )
        assert not broker.firmware_build.manifests
    finally:
        await broker.close()
    assert sessions[0]._closed and pins.count("pin") == pins.count("unpin")


async def test_wrong_builder_factory_refused_before_workers(setup):
    broker, pins, sessions = setup
    made = install_factory(broker, change=lambda wrapper: setattr(wrapper, "builder", object()))
    with pytest.raises(ValueError, match="ownership_changed"):
        await broker.start()
    assert not made[0]._installed and broker.worker is None
    assert broker.private_runtime_session is None and broker.store is None
    assert sessions[0]._closed and pins.count("pin") == pins.count("unpin")


async def test_runtime_requires_protected_store_factory(setup):
    broker, _, _ = setup
    install_factory(broker)
    broker._private_store_session_factory = None
    with pytest.raises(ValueError, match="ownership_changed"):
        await broker.start()
    assert broker.store is None and broker.firmware_build.private_runtime is None


async def test_failure_after_runtime_install_retains_and_quiesces_until_close(setup, monkeypatch):
    broker, pins, sessions = setup
    made = install_factory(broker)
    monkeypatch.setattr(
        broker.profile_jobs, "start", lambda: (_ for _ in ()).throw(RuntimeError("fixture"))
    )
    with pytest.raises(RuntimeError, match="fixture"):
        await broker.start()
    wrapper = made[0]
    assert wrapper._installed and not wrapper.runtime._accepting
    assert broker.private_runtime_session is wrapper and not sessions[0]._closed
    assert broker._state_lock.is_locked
    await broker.close()
    assert wrapper._closed and sessions[0]._closed
    assert pins.count("pin") == pins.count("unpin")


async def test_runtime_enter_rejection_cleans_idle_store_without_install(setup):
    broker, pins, sessions = setup
    install_factory(broker, change=lambda wrapper: wrapper.runtime.stop_accepting())
    with pytest.raises(ValueError, match="not_idle"):
        await broker.start()
    assert broker.private_runtime_session is None and broker.store is None
    assert broker.firmware_build.private_runtime is None and sessions[0]._closed
    assert pins.count("pin") == pins.count("unpin")


async def test_idle_failure_after_runtime_install_closes_store_exactly_once(setup, monkeypatch):
    from cardputer_mcp.logs import Event
    from cardputer_mcp.store import Store

    broker, pins, sessions = setup
    made = install_factory(broker)
    append = broker.logs.append
    close = Store.close
    closes = []

    def fail_started(event, *args, **kwargs):
        if event == Event.STARTED:
            raise RuntimeError("fixture started event")
        return append(event, *args, **kwargs)

    def close_once(store):
        closes.append(store)
        close(store)

    monkeypatch.setattr(broker.logs, "append", fail_started)
    monkeypatch.setattr(Store, "close", close_once)
    with pytest.raises(RuntimeError, match="started event"):
        await broker.start()
    assert made[0]._closed and sessions[0]._closed and len(closes) == 1
    assert broker.store is None and broker.private_runtime_session is None
    assert broker.firmware_build.private_runtime is None
    await broker.close()
    assert len(closes) == 1 and pins.count("pin") == pins.count("unpin")
