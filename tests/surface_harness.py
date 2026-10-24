"""In-process localhost HTTP broker on the mock backend for six-tool surface characterization.

Synthetic tokens only; never opens a serial port or reads installed broker state.
"""

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn
from mcp import Client

from cardputer_mcp.auth import AuthState
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call
from cardputer_mcp.config import Settings
from cardputer_mcp.models import Origin
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64


@dataclass
class Surface:
    url: str
    resident: Broker


@asynccontextmanager
async def serve(tmp_path: Path, *, require_auth: bool) -> AsyncIterator[Surface]:
    """Bearer tokens always authenticate; require_auth only toggles auth_required."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    settings = Settings(
        state_dir=tmp_path / "state",
        port=port,
        allow_mock_execution=True,
        mock_stage_seconds=0.001,
    )
    resident = Broker(settings, lock_path=tmp_path / "device.lockfile")
    auth = AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE})
    app = create_app(settings, resident, require_auth=require_auth, auth=auth)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            proxy_headers=False,
            timeout_graceful_shutdown=2,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("mock server stopped before readiness")
                await asyncio.sleep(0.01)
        yield Surface(f"http://127.0.0.1:{port}/mcp", resident)
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


async def raw(client: Client, tool: str, request: Any) -> tuple[bool, Any, str]:
    """(isError, structuredContent, first text content) without client.call's error raise."""
    result = await client.call_tool(tool, {"request": request})
    text = result.content[0].text if result.content else ""
    return bool(result.is_error), result.structured_content, text


async def lease(client: Client) -> str:
    opened = await call(client, "session", {"action": "open", "owner": "remote_mcp"})
    granted = await call(
        client, "session", {"action": "request", "resume_token": opened["resume_token"]}
    )
    return str(granted["lease"]["session"])


async def ticket(client: Client, session: str, job_id: str) -> str:
    plan = await call(
        client, "submit", {"kind": "diagnostics", "session": session, "job_id": job_id}
    )
    queued = await call(
        client,
        "execute",
        {"op": "run", "plan_hash": plan["plan_hash"], "session": session, "continues": True},
    )
    assert queued["ok"], queued
    return str(queued["continuation_ticket"])


def dumps(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True)
