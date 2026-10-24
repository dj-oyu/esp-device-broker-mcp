"""Explicit execution keeps raw HOME retention disabled across broker boundaries."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from test_serial_capture import device

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, LogRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action

PRINCIPAL = "ephemeral:local_cli"
SECRET = "SYNTHETIC_PRIVATE_HOME_SENTINEL"
WIRE = b"UNKNOWN_PREFIX HOME_READY secret=" + SECRET.encode() + b"\n"


@asynccontextmanager
async def running(tmp_path, *, home=WIRE):
    backend, port, _ = device(clock_step=0.01, home=home)
    broker = Broker(
        Settings(state_dir=tmp_path / "state", backend="serial", serial=backend.config),
        backend=backend,
        serial_enabled=True,
        lock_path=tmp_path / "device.lockfile",
        principal_valid=lambda principal, origin, scopes: (
            principal == PRINCIPAL and origin == "local_cli" and scopes <= {"read", "control"}
        ),
    )
    await broker.start()
    try:
        yield SimpleNamespace(broker=broker, backend=backend, port=port)
    finally:
        await broker.close()


def prepare(env):
    lease = env.broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id=PRINCIPAL)
    return env.broker.prepare(
        PrepareRequest(session=lease["session"], job_id="private-home", test=Action.CAPTURE)
    )


async def execute(env, prepared):
    result = env.broker.execute(
        ExecuteRequest(session=env.broker.session.capability, plan_hash=prepared["plan_hash"])
    )
    assert result["state"] == "queued"
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)


def assert_no_private(value):
    rendered = json.dumps(value, default=str)
    for forbidden in (SECRET, "UNKNOWN_PREFIX", "escaped_line"):
        assert forbidden not in rendered


async def test_normal_explicit_capture_never_installs_raw_home_retention(tmp_path):
    async with running(tmp_path, home=b"HOME_READY\n") as env:
        prepared = prepare(env)
        await execute(env, prepared)
        assert env.broker.job("private-home")["state"] == "succeeded"
        assert env.port.writes == [b"\x1b", b"s"]
        assert_no_private(env.broker.status())
        assert_no_private(list(env.broker._store().db.iterdump()))


async def test_failed_home_wait_keeps_metadata_but_no_raw_wire_or_storage(tmp_path):
    async with running(tmp_path) as env:
        prepared = prepare(env)
        await execute(env, prepared)
        job = env.broker.job("private-home")
        assert job["state"] == "needs_review"
        assert job["diagnostic"]["receive_summary"]["after_command"]["home_ready_token_lines"] == 1
        assert env.port.writes == [b"\x1b"] and env.port.closes == 1
        assert_no_private(job)
        assert_no_private(env.broker.read_logs(LogRequest()))
        assert_no_private(list(env.broker._store().db.iterdump()))
        for path in env.broker.settings.state_dir.glob("history.sqlite3*"):
            assert SECRET.encode() not in path.read_bytes()
