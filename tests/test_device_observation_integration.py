"""Explicit firmware reports cross broker/MCP boundaries using fake USB only."""

import asyncio
import hashlib
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from weakref import WeakKeyDictionary

import httpx2
import pytest
from test_serial_observation import STARTUP, collected, device

from cardputer_mcp import server as server_module
from cardputer_mcp.auth import AuthState
from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.config import Settings
from cardputer_mcp.key_registry import KeyRegistry
from cardputer_mcp.models import (
    AcquireRequest,
    CreateWorkSessionRequest,
    ExecuteRequest,
    LeaseRequest,
    LogRequest,
    Origin,
    PrepareRequest,
    RequestDeviceRequest,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_timeline import OperatorTimeline
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64
PRINCIPAL = "ephemeral:local_cli"
REMOTE_PRINCIPAL = "ephemeral:remote_mcp"
SCOPES = frozenset({"read", "control"})
LOG_LINE = "SYNTHETIC_PRIVATE_CONSOLE_SENTINEL"
SECRETS = ("SYNTHETIC_PASSWORD_VALUE", "SYNTHETIC_BEARER_VALUE")
WIRE = (
    STARTUP
    + LOG_LINE.encode()
    + b"\npassword=SYNTHETIC_PASSWORD_VALUE\n"
    + b"Authorization: Bearer SYNTHETIC_BEARER_VALUE\n"
)

# Live public/SQLite/filesystem checks happen immediately. Scan again after
# shutdown as well, including files created by teardown or checkpointing.
_durable_checks = WeakKeyDictionary()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Grant:
    def __init__(self):
        self.live = True
        self.scopes = set(SCOPES)
        self.calls = []

    def __call__(self, principal, origin, scopes):
        self.calls.append((principal, origin, scopes))
        return (
            self.live
            and principal == f"ephemeral:{origin}"
            and origin in {"local_cli", "remote_mcp"}
            and scopes <= self.scopes
        )


@asynccontextmanager
async def running(tmp_path, *, wire=WIRE, **port_options):
    backend, port, _ = device(clock_step=1, incoming=[wire] if wire else [], **port_options)
    clock, grant = Clock(), Grant()
    settings = Settings(backend="serial", serial=backend.config, state_dir=tmp_path / "state")
    broker = Broker(
        settings,
        backend=backend,
        serial_enabled=True,
        lock_path=tmp_path / "device.lockfile",
        clock=clock,
        principal_valid=grant,
    )
    await broker.start()
    try:
        yield SimpleNamespace(
            broker=broker,
            backend=backend,
            port=port,
            clock=clock,
            grant=grant,
            approvals=PlanApprovals(broker),
        )
    finally:
        if port.close_release is not None:
            port.close_release.set()
        await broker.close()
        assert_no_durable_logs(broker)


def prepare(env, job_id="firmware-report", *, origin=Origin.LOCAL):
    if env.broker.session is None:
        env.broker.acquire(
            AcquireRequest(owner=origin, lease_seconds=300),
            principal_id=f"ephemeral:{origin.value}",
        )
    return env.broker.prepare(
        PrepareRequest(session=env.broker.session.capability, job_id=job_id, test=Action.FIRMWARE)
    )


def validate_prepared(env, plan):
    """Read-only preflight; grants no execution authority."""
    return env.approvals.validate_current(plan["plan_hash"], env.broker.session)


async def execute(env, plan):
    request = ExecuteRequest(session=env.broker.session.capability, plan_hash=plan["plan_hash"])
    env.broker.execute(request)
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    return request


def unavailable(env, job_id="firmware-report", principal=PRINCIPAL):
    """No RAM report is delivered. The owner may still read the durable job log."""
    owner = env.broker.job_principal(job_id) if env.broker.store is not None else None
    try:
        result = env.broker.read_device_logs(job_id, principal)
    except BrokerError as error:
        assert str(error) == "device_logs_not_available"
        return
    assert owner == principal
    assert result["source"] == "durable_job_log" and "sha256" not in result
    for secret in SECRETS:
        assert secret not in json.dumps(result)


def assert_no_public_or_durable_logs(broker, *credentials):
    public = json.dumps([broker.status(), broker.read_logs(LogRequest())])
    for row in broker._store().db.execute("SELECT id FROM jobs"):
        public += json.dumps(broker.job(row[0]))
    history = "\n".join(broker._store().db.iterdump())
    for forbidden in (LOG_LINE, *SECRETS, *credentials):
        assert forbidden not in public and forbidden not in history
    _durable_checks.setdefault(broker, set()).update(credentials)
    for path in broker.settings.state_dir.rglob("*"):
        if path.is_file():
            data = read_live_fixture_file(broker, path)
            for forbidden in (*_console_text(path), *SECRETS, *credentials):
                assert forbidden.encode() not in data


def _console_text(path):
    """Redacted device text is durable only in the job-log database."""
    durable = path.parent.name == "job-logs" and path.name.startswith("job-logs.sqlite3")
    return () if durable else (LOG_LINE,)


def read_live_fixture_file(broker, path):
    try:
        return path.read_bytes()
    except PermissionError:
        # A second handle cannot read Windows' locked byte range. Read through
        # this fixture's already-owned lock descriptor instead, never unlock,
        # change sharing/permissions or omit the file. Preserve its position.
        lock = broker._state_lock
        assert os.name == "nt" and path == Path(lock.lock_file) and lock.is_locked
        descriptor = lock._context.lock_file_fd
        assert descriptor is not None
        info, current = os.fstat(descriptor), path.stat()
        assert (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino)
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            data = bytearray()
            while chunk := os.read(descriptor, 65536):
                data.extend(chunk)
            return bytes(data)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)


def assert_no_durable_logs(broker):
    credentials = _durable_checks.pop(broker, set())
    for path in broker.settings.state_dir.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for forbidden in (*_console_text(path), *SECRETS, *credentials):
                assert forbidden.encode() not in data


async def test_explicit_request_delivers_redacted_report_without_approval(tmp_path, caplog):
    async with running(tmp_path) as env:
        plan = prepare(env)
        assert plan["approval_required"] is False
        assert plan["plan"]["log_delivery"] == {
            "principal_id": PRINCIPAL,
            "origin": "local_cli",
            "retention_seconds": 300,
            "max_received_bytes": 65536,
            "max_lines": 256,
            "max_line_bytes": 1024,
            "max_rendered_bytes": 32768,
        }
        request = ExecuteRequest(session=env.broker.session.capability, plan_hash=plan["plan_hash"])
        unavailable(env)
        assert env.port.opens == 0 and env.broker.queue.empty()
        validate_prepared(env, plan)
        unavailable(env)
        request = await execute(env, plan)
        result = env.broker.read_device_logs("firmware-report", PRINCIPAL)
        expected = collected(WIRE)
        assert result == {
            "job_id": "firmware-report",
            "sha256": expected.sha256,
            "remaining_seconds": 300,
            **expected.payload(),
        }
        assert LOG_LINE in result["logs"]
        assert result["redacted_lines"] == 2
        assert result["firmware"]["project_name"] == "synthetic-pocketjs"
        assert result["firmware"]["source_verified"] is False
        assert result["firmware"]["binary_sha256_verified"] is False
        for secret in SECRETS:
            assert secret not in json.dumps(result) and secret not in caplog.text
        for _ in range(3):
            assert env.broker.read_device_logs("firmware-report", PRINCIPAL) == result
            assert env.broker.execute(request)["deduplicated"]
        await env.broker.queue.join()
        assert env.port.opens == env.port.closes == 1 and env.port.writes == []
        job = env.broker.job("firmware-report")
        assert job["state"] == "succeeded"
        assert job["result"] == "firmware_observation_completed_no_commands"
        metadata = env.broker.operator_device_observation_metadata("firmware-report")
        assert metadata["metadata"] == expected.metadata()
        assert "logs" not in metadata and "logs" not in metadata["metadata"]
        assert LOG_LINE not in json.dumps(metadata)
        assert LOG_LINE not in json.dumps(OperatorTimeline(env.broker, env.approvals).snapshot())
        assert LOG_LINE not in caplog.text
        assert_no_public_or_durable_logs(env.broker, request.session, LOCAL, REMOTE)


async def test_unauthenticated_lease_cannot_prepare_device_log_sharing(tmp_path):
    async with running(tmp_path) as env:
        lease = env.broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        with pytest.raises(BrokerError, match="^authentication_required$"):
            env.broker.prepare(
                PrepareRequest(session=lease["session"], job_id="anonymous", test=Action.FIRMWARE)
            )
        assert env.port.opens == 0


@pytest.mark.parametrize("change", [{"principal_id": REMOTE_PRINCIPAL}, {"origin": "remote_mcp"}])
@pytest.mark.parametrize("rehash", [False, True])
async def test_tampered_log_recipient_cannot_execute(tmp_path, change, rehash):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        changed = json.loads(json.dumps(plan["plan"]))
        changed["log_delivery"].update(change)
        plan_hash = digest_plan(changed) if rehash else plan["plan_hash"]
        if rehash:
            env.broker._store().put_plan(plan_hash, changed)
        else:
            env.broker._store().db.execute(
                "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(changed), plan_hash)
            )
        with pytest.raises(BrokerError):
            env.approvals.validate_current(plan_hash, env.broker.session)
        with pytest.raises(BrokerError):
            env.broker.execute(
                ExecuteRequest(session=env.broker.session.capability, plan_hash=plan_hash)
            )
        assert env.port.opens == 0 and env.broker.queue.empty()
        unavailable(env)


async def test_wrong_principal_and_missing_read_scope_never_expose_report(tmp_path):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        await execute(env, plan)
        for principal in (REMOTE_PRINCIPAL, "same-origin-different-key", ""):
            unavailable(env, principal=principal)
        env.broker.release(LeaseRequest(session=env.broker.session.capability))
        env.grant.scopes = {"read"}
        assert env.broker.read_device_logs("firmware-report", PRINCIPAL)["logs"]
        assert env.grant.calls[-1] == (PRINCIPAL, "local_cli", frozenset({"read"}))
        env.grant.scopes = {"control"}
        unavailable(env)
        assert env.broker.operator_device_observation_metadata("firmware-report") is None
        assert not env.broker.device_observations
        env.grant.scopes = set(SCOPES)
        unavailable(env)
        assert env.port.opens == 1


@pytest.mark.parametrize("outcome", [False, None, 1, "true", "exception"])
async def test_current_recipient_validation_fails_closed_and_erases_cache(tmp_path, outcome):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        await execute(env, plan)

        def invalid(*args):
            if outcome == "exception":
                raise RuntimeError(LOG_LINE)
            return outcome

        env.broker.principal_valid = invalid
        unavailable(env)
        assert not env.broker.device_observations
        assert env.port.opens == 1
        assert_no_public_or_durable_logs(env.broker)


async def test_read_after_release_retains_fixed_deadline_and_restart_never_replays(tmp_path):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        request = await execute(env, plan)
        old_epoch = env.broker.epoch
        env.broker.release(LeaseRequest(session=request.session))
        env.clock.now = 399
        report = env.broker.read_device_logs("firmware-report", PRINCIPAL)
        assert report["remaining_seconds"] == 1
        assert env.port.opens == 1
        env.clock.now = 400
        unavailable(env)
        assert not env.broker.device_observations
        assert env.broker.job("firmware-report")["state"] == "succeeded"
    async with running(tmp_path) as fresh:
        assert fresh.broker.epoch != old_epoch
        unavailable(fresh)
        assert fresh.broker.job("firmware-report")["state"] == "succeeded"
        assert fresh.port.opens == 0 and fresh.broker.queue.empty()


def create_work(env, *, origin=Origin.LOCAL):
    return env.broker.create_work_session(
        CreateWorkSessionRequest(owner=origin),
        principal_id=f"ephemeral:{origin.value}",
        scopes=SCOPES,
    )


def request_device(env, work, *, origin=Origin.LOCAL):
    return env.broker.request_device(
        RequestDeviceRequest(resume_token=work["resume_token"], lease_seconds=300),
        principal_id=f"ephemeral:{origin.value}",
        scopes=SCOPES,
    )


async def test_two_origins_wait_for_confirmed_close_before_handover(tmp_path):
    async with running(tmp_path) as env:
        local, remote = create_work(env), create_work(env, origin=Origin.REMOTE)
        lease = request_device(env, local)["lease"]
        plan = prepare(env)
        validate_prepared(env, plan)
        request_device(env, remote, origin=Origin.REMOTE)
        env.port.close_release = threading.Event()
        env.broker.execute(ExecuteRequest(session=lease["session"], plan_hash=plan["plan_hash"]))
        assert await asyncio.to_thread(env.port.close_started.wait, 5)
        assert env.broker.job("firmware-report")["state"] == "running"
        assert env.backend.active == 1 and env.port.is_open
        unavailable(env)
        unavailable(env, principal=REMOTE_PRINCIPAL)
        assert env.broker.operator_device_observation_metadata("firmware-report") is None
        with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
            env.broker.release(LeaseRequest(session=lease["session"]))
        assert env.broker.session.principal_id == PRINCIPAL and len(env.broker.waiters) == 1
        assert env.port.opens == 1 and env.broker._device_lock.is_locked
        env.port.close_release.set()
        await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        assert env.broker.read_device_logs("firmware-report", PRINCIPAL)["logs"]
        env.broker.release(LeaseRequest(session=lease["session"]))
        assert env.broker.session.principal_id == REMOTE_PRINCIPAL
        unavailable(env, principal=REMOTE_PRINCIPAL)
        env.port.chunks.append(b"remote session console\n")
        next_plan = prepare(env, "remote-report", origin=Origin.REMOTE)
        validate_prepared(env, next_plan)
        await execute(env, next_plan)
        assert env.broker.read_device_logs("remote-report", REMOTE_PRINCIPAL)["logs"] == [
            "remote session console"
        ]
        assert env.backend.max_active == 1 and env.port.opens == env.port.closes == 2
        assert env.port.writes == []


@pytest.mark.parametrize("port_options", [{"close_error": True}, {"still_open": True}])
async def test_uncertain_close_withholds_data_and_keeps_device_lock(tmp_path, port_options):
    async with running(tmp_path, **port_options) as env:
        try:
            plan = prepare(env)
            validate_prepared(env, plan)
            request = await execute(env, plan)
            assert env.broker.job("firmware-report")["state"] == "needs_review"
            assert env.broker.job("firmware-report")["result"] == "serial_close_unconfirmed"
            unavailable(env)
            assert not env.broker.device_observations
            assert env.broker.operator_device_observation_metadata("firmware-report") is None
            with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                env.broker.release(LeaseRequest(session=request.session))
            with pytest.raises(BrokerError, match="^device_close_unconfirmed$"):
                await env.broker.close()
            assert env.broker._device_lock.is_locked and env.backend.active == 1
            assert env.port.opens == env.port.closes == 1 and env.port.writes == []
            assert_no_public_or_durable_logs(env.broker)
        finally:
            # Synthetic teardown only; the fake port never opened hardware.
            env.backend.active = 0
            env.backend.hardware_connected = False
            env.backend.ownership_uncertain = False
            env.broker.active_job = None


async def test_read_failure_withholds_partial_logs_and_sanitizes_exception(tmp_path, caplog):
    async with running(tmp_path) as env:
        env.port.chunks.append(OSError(LOG_LINE + SECRETS[0]))
        plan = prepare(env)
        validate_prepared(env, plan)
        await execute(env, plan)
        job = env.broker.job("firmware-report")
        # Review classification fix: a read error with a confirmed close is `failed`
        # (was needs_review); the result code is unchanged.
        assert job["state"] == "failed" and job["result"] == "serial_read_failed"
        unavailable(env)
        assert not env.broker.device_observations
        assert env.broker.operator_device_observation_metadata("firmware-report") is None
        assert env.port.opens == env.port.closes == 1 and env.port.writes == []
        assert LOG_LINE not in caplog.text and SECRETS[0] not in caplog.text
        assert_no_public_or_durable_logs(env.broker)


@pytest.mark.parametrize("mutation", ["missing", "failed"])
async def test_read_rechecks_job_after_recipient_callback_and_erases_cache(tmp_path, mutation):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        await execute(env, plan)

        def change_job(principal, origin, scopes):
            assert (principal, origin, scopes) == (PRINCIPAL, "local_cli", frozenset({"read"}))
            if mutation == "missing":
                env.broker._store().db.execute("DELETE FROM jobs WHERE id=?", ("firmware-report",))
            else:
                env.broker._store().set_job("firmware-report", "failed", "synthetic_failure")
            return True

        env.broker.principal_valid = change_job
        unavailable(env)
        assert not env.broker.device_observations
        assert env.broker.operator_device_observation_metadata("firmware-report") is None
        assert env.port.opens == env.port.closes == 1
        assert_no_public_or_durable_logs(env.broker)


@pytest.fixture
async def http_observation(tmp_path, monkeypatch):
    backend, port, _ = device(clock_step=1, incoming=[WIRE])
    clock, key_clock = Clock(), Clock()
    registry_path = tmp_path / "keys.sqlite3"
    registry = KeyRegistry(registry_path, clock=key_clock)
    keys = {
        "owner": registry.issue(
            "Synthetic owner", Origin.REMOTE, scopes={"control"}, ttl_seconds=10
        ),
        "other": registry.issue("Synthetic other", Origin.REMOTE, scopes={"control"}),
        "reader": registry.issue("Synthetic reader", Origin.REMOTE, scopes={"read"}),
    }
    monkeypatch.setattr(server_module, "time", key_clock)
    monkeypatch.setattr(
        server_module,
        "KeyRegistry",
        lambda path, *, create: KeyRegistry(path, clock=key_clock, create=create),
    )
    settings = Settings(backend="serial", serial=backend.config, state_dir=tmp_path / "state")
    broker = Broker(
        settings,
        backend=backend,
        serial_enabled=True,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
    )
    app = create_app(
        settings,
        broker,
        serial_enabled=True,
        auth=AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE}),
        key_registry_path=registry_path,
    )
    inner = app
    while not hasattr(inner, "router"):
        inner = inner.app
    ready, shutdown = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with inner.router.lifespan_context(inner):
            ready.set()
            await shutdown.wait()

    task = asyncio.create_task(lifespan())
    try:
        async with asyncio.timeout(5):
            while not ready.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.001)
        transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx2.AsyncClient(
            transport=transport,
            base_url=f"http://127.0.0.1:{settings.port}",
            trust_env=False,
            headers={"Authorization": f"Bearer {LOCAL}"},
        ) as http:
            yield SimpleNamespace(
                http=http,
                broker=broker,
                port=port,
                registry=registry,
                keys=keys,
                clock=clock,
                key_clock=key_clock,
            )
    finally:
        shutdown.set()
        await asyncio.wait_for(task, timeout=5)
        registry.close()
        assert_no_durable_logs(broker)


async def rpc_response(env, method, params, *, token):
    return await env.http.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )


async def rpc(env, tool, request=None, *, token=None):
    response = await rpc_response(
        env,
        "tools/call",
        {"name": tool, "arguments": {} if request is None else {"request": request}},
        token=env.keys["owner"].token if token is None else token,
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


DEVICE_LOGS = {"view": "logs", "stream": "device", "job_id": "firmware-report"}


async def http_observe(env):
    acquired = (
        await rpc(env, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 300})
    )["structuredContent"]
    prepared = (
        await rpc(
            env,
            "submit",
            {
                "session": acquired["session"],
                "job_id": "firmware-report",
                "kind": "observe_firmware",
            },
        )
    )["structuredContent"]
    assert prepared["ok"] and not prepared["approval_required"]
    request = {"op": "run", "session": acquired["session"], "plan_hash": prepared["plan_hash"]}
    assert (await rpc(env, "execute", request))["structuredContent"]["ok"]
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    return acquired["session"], prepared


async def test_mcp_catalog_and_structured_report_obey_principal_boundary(http_observation):
    env = http_observation
    catalog = await rpc_response(env, "tools/list", {}, token=env.keys["owner"].token)
    assert catalog.status_code == 200
    tools = catalog.json()["result"]["tools"]
    descriptor = next(item for item in tools if item["name"] == "status")
    assert descriptor["annotations"]["readOnlyHint"] is True
    assert "job_id" in json.dumps(descriptor["inputSchema"])
    assert "device" in json.dumps(descriptor["inputSchema"])
    assert not any("approv" in item["name"] or "operator" in item["name"] for item in tools)
    session, plan = await http_observe(env)
    result = await rpc(env, "status", DEVICE_LOGS)
    report = result["structuredContent"]
    assert report["ok"] and LOG_LINE in report["logs"]
    payload = {
        k: v for k, v in report.items() if k not in {"ok", "job_id", "sha256", "remaining_seconds"}
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert hashlib.sha256(canonical).hexdigest() == report["sha256"]
    # Reads are not bound to the job's principal: every read-scoped caller gets the
    # owner's exact redacted report (cross-principal reads, 2026-10-25).
    for token in (LOCAL, REMOTE, env.keys["other"].token, env.keys["reader"].token):
        other = (await rpc(env, "status", DEVICE_LOGS, token=token))["structuredContent"]
        assert other == report
    assert (await rpc(env, "session", {"action": "release", "session": session}))[
        "structuredContent"
    ]["released"]
    for _ in range(3):
        assert (await rpc(env, "status", DEVICE_LOGS))["structuredContent"] == report
    timeline = await env.http.get("/operator/timeline")
    assert timeline.status_code == 200 and LOG_LINE not in timeline.text
    for request in (
        ({"view": "device"}),
        ({"view": "logs", "stream": "broker"}),
        ({"view": "job", "job_id": "firmware-report"}),
    ):
        public = await rpc(env, "status", request, token=env.keys["reader"].token)
        assert LOG_LINE not in json.dumps(public)
    assert_no_public_or_durable_logs(env.broker, session, *(key.token for key in env.keys.values()))
    assert plan["plan"]["log_delivery"]["principal_id"] == env.keys["owner"].metadata.principal_id
    assert env.port.opens == env.port.closes == 1 and env.port.writes == []


@pytest.mark.parametrize("invalidate", ["revoke", "expire", "rotate"])
async def test_mcp_rechecks_key_authority_after_device_completion(http_observation, invalidate):
    env = http_observation
    await http_observe(env)
    key = env.keys["owner"]
    assert (await rpc(env, "status", DEVICE_LOGS))["structuredContent"]["ok"]
    if invalidate == "revoke":
        assert env.registry.revoke(key.metadata.key_id)
    elif invalidate == "expire":
        env.key_clock.now += 11
    else:
        replacement = env.registry.rotate(key.metadata.key_id)
        # The RAM report stays bound to the original key's validity; any read-scoped
        # caller (here the replacement key) still reads the durable redacted log.
        durable = (await rpc(env, "status", DEVICE_LOGS, token=replacement.token))[
            "structuredContent"
        ]
        assert durable["ok"] and durable["source"] == "durable_job_log"
        assert "sha256" not in durable
    response = await rpc_response(
        env,
        "tools/call",
        {"name": "status", "arguments": {"request": DEVICE_LOGS}},
        token=key.token,
    )
    assert response.status_code == 401
    assert response.json() == {"error": "authentication_required"}
    assert env.broker.operator_device_observation_metadata("firmware-report") is None
    assert env.broker.job("firmware-report")["state"] == "succeeded"
    assert env.port.opens == 1
