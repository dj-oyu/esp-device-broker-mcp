"""Real cache contention and refused deferred flash; synthetic files/ROM only."""

import asyncio
import json
import threading
from contextlib import contextmanager, nullcontext

import pytest
from test_flash_policy import env as env

from cardputer_mcp.broker import BrokerError
from cardputer_mcp.flash_binding import prepare_binding
from cardputer_mcp.models import ExecuteRequest, LeaseRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action


@contextmanager
def competing_cache_owner(snapshots):
    entered, release = threading.Event(), threading.Event()

    def hold():
        # The same real CacheLock used by source fetch, on a different thread.
        with snapshots.cache_guard("fixture", write=True):
            entered.set()
            assert release.wait(10)

    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert entered.wait(3)
        yield
    finally:
        release.set()
        worker.join(3)
        assert not worker.is_alive()


def refusal_stages(broker):
    return [
        entry["stage"]
        for entry in broker.logs.entries
        if entry["event"] == "job_transition_refused"
        and (entry["stage"] or "").startswith("flash_binding:")
    ]


@pytest.mark.parametrize("scenario", ["cache_busy", "changed_source", "explicit_cancel"])
async def test_deferred_flash_failure_is_classified_without_enqueue_or_replay(env, scenario):
    broker = env.broker
    snapshots = broker.firmware_source.snapshots
    source, _ = snapshots.resolve("fixture", env.receipt, "alice")
    source_file = source / "CMakeLists.txt"
    original = source_file.read_bytes()
    writes = env.driver.writes
    broker.release(LeaseRequest(session=env.session))
    # The fixture already persisted a succeeded, owned source/build pair.
    ticket = broker.tickets.issue("build-one", "alice")
    prepared = broker.prepare_deferred(
        PrepareRequest(
            job_id="deferred-flash",
            test=Action.FLASH,
            after=ticket,
            device_id="fixture",
            build_job_id="build-one",
        ),
        "alice",
        Origin.LOCAL.value,
    )
    broker.execute_deferred(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), "alice", Origin.LOCAL.value
    )
    if scenario == "changed_source":
        source_file.write_bytes(b"changed synthetic source")
    elif scenario == "explicit_cancel":
        assert broker.cancel_deferred("deferred-flash", "alice")["state"] == "cancelled"
    guard = competing_cache_owner(snapshots) if scenario == "cache_busy" else nullcontext()
    try:
        with guard:
            broker.maintenance_tick()
            cancelled = broker.deferred_job("deferred-flash", "alice")
            assert cancelled["state"] == "cancelled"
            if scenario == "explicit_cancel":
                assert cancelled["result"] == "client_cancelled_waiting"
                assert refusal_stages(broker) == []
            else:
                assert cancelled["result"] == "flash_binding_invalid"
                code = "source_fetch_busy" if scenario == "cache_busy" else "source_snapshot_changed"
                assert refusal_stages(broker) == ["flash_binding:source_authority:" + code]
    finally:
        source_file.write_bytes(original)
    # Releasing contention/restoring bytes never replays a refused/cancelled request.
    for _ in range(3):
        broker.maintenance_tick()
        await asyncio.sleep(0)
    assert broker.deferred_job("deferred-flash", "alice") == cancelled
    assert env.store.job("deferred-flash") is None and broker.queue.empty()
    assert broker.session is None and env.driver.writes == writes


@pytest.mark.parametrize(
    "failure,code",
    [
        (ValueError("build_output_identity_mismatch"), "build_output_identity_mismatch"),
        (ValueError("PRIVATE /synthetic/path token=secret"), "unexpected_error"),
        (RuntimeError("PRIVATE /synthetic/path token=secret"), "unexpected_error"),
    ],
)
async def test_binding_logs_only_fixed_phase_and_cause_without_changing_public_error(
    env, monkeypatch, failure, code
):
    prepared = env.prepare("classified", build_job_id="build-one")

    def refuse(_plan):
        raise failure

    monkeypatch.setattr(env.broker.firmware_flash, "prepare_plan", refuse)
    with pytest.raises(BrokerError, match="^flash_binding_invalid$"):
        prepare_binding(env.broker, prepared["plan"])
    assert refusal_stages(env.broker) == ["flash_binding:flash_plan:" + code]
    assert "PRIVATE" not in json.dumps(list(env.broker.logs.entries))
    assert env.store.job("classified") is None and env.broker.queue.empty()


async def test_valid_shape_with_mismatched_binding_records_comparison_phase(env):
    prepared = env.prepare("mismatched", build_job_id="build-one")
    plan = dict(prepared["plan"], expected_app_bin_sha256="f" * 64)
    with pytest.raises(BrokerError, match="^flash_binding_invalid$"):
        prepare_binding(env.broker, plan)
    assert refusal_stages(env.broker) == ["flash_binding:plan_details:binding_mismatch"]
    assert env.store.job("mismatched") is None and env.broker.queue.empty()
