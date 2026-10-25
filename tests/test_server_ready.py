"""The shared uvicorn readiness wait explains a missed deadline instead of a bare timeout."""

import asyncio
import re
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
import server_ready
import uvicorn
from server_ready import LOG_LEVEL, ServerStartupTimeout, startup_stage, wait_until_started


async def blocked_lifespan_startup(gate: asyncio.Event) -> None:
    await gate.wait()


def gated_app(gate: asyncio.Event) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "lifespan"
        assert (await receive())["type"] == "lifespan.startup"
        await blocked_lifespan_startup(gate)
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    return app


@pytest.mark.parametrize("call_graph", ["native", "await_chain"])
async def test_timeout_reports_pending_stage_and_stacks(capsys, monkeypatch, call_graph) -> None:
    if call_graph == "await_chain":
        # Python < 3.14 has no asyncio.format_call_graph; exercise the fallback everywhere.
        monkeypatch.delattr(asyncio, "format_call_graph", raising=False)
    elif not hasattr(asyncio, "format_call_graph"):
        pytest.skip("asyncio.format_call_graph requires Python 3.14")
    gate = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config(
            gated_app(gate), host="127.0.0.1", port=0, log_level=LOG_LEVEL, access_log=False
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        with pytest.raises(ServerStartupTimeout) as caught:
            await wait_until_started(server, task, "Gated server", deadline=0.3)
    finally:
        gate.set()
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
    report = str(caught.value)
    assert isinstance(caught.value, TimeoutError)
    assert "Gated server: uvicorn not ready within 0.3s" in report
    assert "pending stage: app_lifespan_startup" in report
    assert "app_lifespan_startup@" in report and "started@" not in report
    assert "longest poll gap" in report and "gc during wait" in report
    assert "=== thread stacks at deadline ===" in report and "MainThread" in report
    # An awaiting (not blocking) lifespan never stalls the loop for a full second.
    assert "<loop never stayed blocked for 1s>" in report
    assert "test_timeout_reports_pending_stage_and_stacks" in report
    assert "=== asyncio tasks (server task first) ===" in report
    # The lifespan task's await chain reaches the app's blocked startup.
    assert "blocked_lifespan_startup" in report
    assert report in capsys.readouterr().err


def synchronously_blocking_startup(seconds: float) -> None:
    time.sleep(seconds)  # stands in for synchronous broker/SQLite work on the loop


def loop_blocking_app(gate: asyncio.Event, seconds: float) -> Any:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "lifespan"
        assert (await receive())["type"] == "lifespan.startup"
        synchronously_blocking_startup(seconds)
        await gate.wait()
        await send({"type": "lifespan.startup.complete"})
        assert (await receive())["type"] == "lifespan.shutdown"
        await send({"type": "lifespan.shutdown.complete"})

    return app


def sampler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "readiness-stall-sampler"]


async def test_timeout_reports_stack_sampled_while_loop_was_blocked(monkeypatch) -> None:
    monkeypatch.setattr(server_ready, "BLOCK_SAMPLE_AT", (0.3, 0.6, 5.0))
    gate = asyncio.Event()
    server = uvicorn.Server(
        uvicorn.Config(
            loop_blocking_app(gate, 1.2),
            host="127.0.0.1",
            port=0,
            log_level=LOG_LEVEL,
            access_log=False,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        with pytest.raises(ServerStartupTimeout) as caught:
            await wait_until_started(server, task, "Blocking server", deadline=0.2)
    finally:
        gate.set()
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
    assert not sampler_threads()
    report = str(caught.value)
    blocked, _, at_deadline = report.partition("=== thread stacks at deadline ===")
    blocked = blocked.partition("=== loop thread stacks while loop was blocked ===")[2]
    # The blocking frame was captured during the block, not after it ended.
    assert "synchronously_blocking_startup" in blocked
    assert "time.sleep(seconds)" in blocked
    assert "synchronously_blocking_startup" not in at_deadline
    # The 0.3 s and 0.6 s samples see the same stack and are folded into one block.
    assert blocked.count("--- blocked ") == 1
    # A starved watchdog may cross both thresholds in one wake-up and take one sample.
    assert re.search(
        r"--- blocked \d\.\ds at \+\d\.\ds(; blocked \d\.\ds at \+\d\.\ds)? ---", blocked
    )
    assert "blocked 5." not in blocked


async def test_ready_server_returns_and_early_exit_fails() -> None:
    gate = asyncio.Event()
    gate.set()
    server = uvicorn.Server(
        uvicorn.Config(
            gated_app(gate), host="127.0.0.1", port=0, log_level=LOG_LEVEL, access_log=False
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        await wait_until_started(server, task, "Open server", deadline=5)
        assert startup_stage(server) == "started"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)

    async def finished() -> None:
        return None

    done = asyncio.create_task(finished())
    await done
    with pytest.raises(AssertionError, match="Stopped server stopped before readiness"):
        await wait_until_started(SimpleNamespace(started=False), done, "Stopped server")


def test_stage_names_follow_uvicorn_startup_order() -> None:
    pending = asyncio.Event()
    finished = asyncio.Event()
    finished.set()
    queued: asyncio.Queue[object] = asyncio.Queue()
    queued.put_nowait({"type": "lifespan.startup"})
    empty: asyncio.Queue[object] = asyncio.Queue()

    def stage(**state: Any) -> str:
        return startup_stage(SimpleNamespace(started=False, **state))

    assert stage() == "config_load"
    assert (
        stage(lifespan=SimpleNamespace(startup_event=pending, receive_queue=queued))
        == "lifespan_startup_queued"
    )
    assert (
        stage(lifespan=SimpleNamespace(startup_event=pending, receive_queue=empty))
        == "app_lifespan_startup"
    )
    assert stage(lifespan=SimpleNamespace(startup_event=finished)) == "socket_bind_listen"
    assert stage(lifespan=SimpleNamespace(), servers=[]) == "post_bind"
    assert startup_stage(SimpleNamespace(started=True)) == "started"
