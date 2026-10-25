"""The shared uvicorn readiness wait explains a missed deadline instead of a bare timeout."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
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


async def test_timeout_reports_pending_stage_and_stacks(capsys) -> None:
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
    assert "=== thread stacks ===" in report and "MainThread" in report
    assert "test_timeout_reports_pending_stage_and_stacks" in report
    assert "=== asyncio tasks (server task first) ===" in report
    # The lifespan task's await chain reaches the app's blocked startup.
    assert "blocked_lifespan_startup" in report
    assert report in capsys.readouterr().err


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
