"""Firmware file verification never blocks the broker loop; never USB or SDK.

A flash request is admitted in three phases: cheap synchronous checks (with
deduplication), pure file verification in worker threads, then every check
again and the enqueue with no await in between. The worker verifies the same
way before its final synchronous checks and the running transition.
"""

import asyncio
import threading
import time

import pytest
import test_flash_policy

from cardputer_mcp import firmware_flash
from cardputer_mcp.broker import BrokerError
from cardputer_mcp.models import ExecuteRequest, LeaseRequest
from cardputer_mcp.offload_io import IOMemo, IOPending, complete, offload, pure_io

env = test_flash_policy.env


# ---------------------------------------------------------------------------
# offload_io
# ---------------------------------------------------------------------------


def test_pure_io_outside_offload_calls_directly():
    assert pure_io("double", lambda value: value * 2, 4) == 8


async def test_offload_runs_pure_work_in_a_thread_and_the_last_pass_on_the_loop():
    loop_thread = threading.get_ident()
    threads: list[int] = []
    passes: list[int] = []

    def read(name: str) -> str:
        threads.append(threading.get_ident())
        return name.upper()

    def compute() -> str:
        passes.append(threading.get_ident())
        return pure_io("read", read, "a") + pure_io("read", read, "b") + pure_io("read", read, "a")

    memo = IOMemo()
    assert await offload(compute, memo) == "ABA"
    assert len(threads) == 2 and loop_thread not in threads  # "a" is read once
    assert passes == [loop_thread] * 3
    assert complete(compute, memo) == (True, "ABA")
    assert complete(lambda: pure_io("read", read, "c"), memo) == (False, None)
    assert len(threads) == 2


async def test_offload_memoizes_and_reraises_the_pure_failure():
    calls = []

    def fail() -> None:
        calls.append(1)
        raise ValueError("pure_failure")

    def compute() -> None:
        try:
            pure_io("fail", fail)
        except ValueError:
            # A memo miss is not an Exception: validation code cannot swallow it.
            raise
        except Exception as exc:  # pragma: no cover - would hide IOPending
            raise AssertionError from exc

    memo = IOMemo()
    with pytest.raises(ValueError, match="pure_failure"):
        await offload(compute, memo)
    with pytest.raises(ValueError, match="pure_failure"):
        complete(compute, memo)
    assert calls == [1]
    assert not issubclass(IOPending, Exception)


# ---------------------------------------------------------------------------
# Broker admission and dispatch
# ---------------------------------------------------------------------------


class _Gate:
    """Slow, thread-recording stand-in for bundle verification."""

    def __init__(self, monkeypatch, *, hold_from: int = 1, delay: float = 0.0):
        self.original = firmware_flash.verify_bundle
        self.threads: list[int] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.hold_from, self.delay = hold_from, delay
        monkeypatch.setattr(firmware_flash, "verify_bundle", self)

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        if len(self.threads) >= self.hold_from:
            self.started.set()
            assert self.release.wait(10)
        time.sleep(self.delay)
        return self.original(*args, **kwargs)

    async def wait_started(self) -> None:
        assert await asyncio.to_thread(self.started.wait, 10)


class _Ticker:
    """Largest gap between 20 ms loop ticks; a blocked loop shows as a long gap."""

    def __init__(self) -> None:
        self.gaps: list[float] = []
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        last = time.monotonic()
        while not self.stop.is_set():
            await asyncio.sleep(0.02)
            now = time.monotonic()
            self.gaps.append(now - last)
            last = now

    async def finish(self) -> float:
        self.stop.set()
        await self.task
        return max(self.gaps)


def _request(env, prepared) -> ExecuteRequest:
    return ExecuteRequest(session=env.session, plan_hash=prepared["plan_hash"])


def _nothing_enqueued(env, job_id: str) -> None:
    assert env.store.job(job_id) is None
    assert env.broker.queue.empty()
    assert job_id not in env.broker._execution_intents
    assert job_id not in env.broker._flash_bindings


async def test_slow_verification_never_delays_the_enqueue_response_or_other_requests(
    env, monkeypatch
):
    prepared = env.prepare("slow-flash", build_job_id="build-one")
    writes = env.driver.writes
    gate = _Gate(monkeypatch, delay=0.6)
    gate.release.set()
    loop_thread = threading.get_ident()
    ticker = _Ticker()
    pending = asyncio.create_task(env.broker.execute_async(_request(env, prepared)))
    await gate.wait_started()
    # Another request is answered while verification runs in its thread.
    asked = time.monotonic()
    assert env.broker.job("flash-one")["state"] == "succeeded"
    await asyncio.sleep(0)
    assert time.monotonic() - asked < 0.2
    assert not pending.done()
    result = await pending
    assert result["deduplicated"] is False and result["state"] == "queued"
    await env.broker.queue.join()
    gap = await ticker.finish()
    assert env.broker.job("slow-flash")["state"] == "succeeded", env.broker.job("slow-flash")
    assert env.driver.writes == writes + 1
    # Admission, worker validation and the flash packet each verified off the loop.
    assert len(gate.threads) >= 3 and loop_thread not in gate.threads
    assert gap < 0.45, gap


async def test_same_plan_requested_twice_during_verification_is_deduplicated(env, monkeypatch):
    prepared = env.prepare("twice", build_job_id="build-one")
    gate = _Gate(monkeypatch)
    first = asyncio.create_task(env.broker.execute_async(_request(env, prepared)))
    second = asyncio.create_task(env.broker.execute_async(_request(env, prepared)))
    await gate.wait_started()
    gate.release.set()
    results = await asyncio.gather(first, second)
    assert sorted(item["deduplicated"] for item in results) == [False, True]
    assert {item["id"] for item in results} == {"twice"}
    await env.broker.queue.join()
    assert env.broker.job("twice")["state"] == "succeeded"
    # A later repeat is deduplicated in the cheap phase, without file work.
    calls = len(gate.threads)
    again = await env.broker.execute_async(_request(env, prepared))
    assert again["deduplicated"] is True and len(gate.threads) == calls


def _release_lease(env) -> None:
    env.broker.release(LeaseRequest(session=env.session))


def _revoke_principal(env) -> None:
    env.broker.principal_valid = lambda *args: False


def _fail_build(env) -> None:
    env.store.db.execute("UPDATE jobs SET state='failed' WHERE id='build-one'")
    env.store.db.commit()


def _close(env) -> None:
    env.broker.closing = True


@pytest.mark.parametrize(
    "change,codes",
    [
        (_release_lease, None),
        (_revoke_principal, {"principal_not_authorized", "invalid_session"}),
        (
            _fail_build,
            {
                "backend_preflight_failed",
                "flash_binding_invalid",
                "execution_intent_not_available",
            },
        ),
        (_close, {"execution_intent_not_available", "broker_closing"}),
    ],
    ids=["lease_released", "principal_revoked", "build_no_longer_succeeded", "closing"],
)
async def test_changes_during_verification_refuse_without_enqueueing(
    env, monkeypatch, change, codes
):
    prepared = env.prepare("changed", build_job_id="build-one")
    gate = _Gate(monkeypatch)
    pending = asyncio.create_task(env.broker.execute_async(_request(env, prepared)))
    await gate.wait_started()
    change(env)
    gate.release.set()
    with pytest.raises(BrokerError) as refused:
        await pending
    if codes is not None:
        assert str(refused.value) in codes, str(refused.value)
    _nothing_enqueued(env, "changed")
    env.broker.closing = False


async def test_reauthorization_runs_after_verification_before_the_enqueue(env, monkeypatch):
    prepared = env.prepare("reauth", build_job_id="build-one")
    _Gate(monkeypatch).release.set()

    def reauthorize() -> None:
        raise BrokerError("principal_not_authorized")

    with pytest.raises(BrokerError, match="principal_not_authorized"):
        await env.broker.execute_async(_request(env, prepared), reauthorize=reauthorize)
    _nothing_enqueued(env, "reauth")


async def test_worker_verifies_off_the_loop_and_rechecks_before_running(env, monkeypatch):
    prepared = env.prepare("worker-changed", build_job_id="build-one")
    writes = env.driver.writes
    # The synchronous admission verifies inline (preflight and binding); the
    # worker's pass, in its thread, is held.
    gate = _Gate(monkeypatch, hold_from=3)
    loop_thread = threading.get_ident()
    env.execute(prepared)
    await gate.wait_started()
    assert gate.threads[:2] == [loop_thread] * 2 and gate.threads[2] != loop_thread
    assert env.broker.job("worker-changed")["state"] == "queued"
    _fail_build(env)
    gate.release.set()
    await env.broker.queue.join()
    job = env.broker.job("worker-changed")
    assert job["state"] == "cancelled" and job["result"] == "flash_binding_invalid", job
    assert env.driver.writes == writes
