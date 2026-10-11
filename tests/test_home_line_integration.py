"""Local HOME previews cross real broker/HTTP boundaries with invented serial bytes only."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx2
import pytest
from test_serial_capture import device

from cardputer_mcp.auth import AuthState
from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.key_registry import KeyRegistry
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, LogRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_timeline import OperatorTimeline
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64
PRINCIPAL = "ephemeral:local_cli"
ATTESTATION = "DEVICE HOME AND FIRMWARE VERIFIED"
SECRET = "SYNTHETIC_PRIVATE_HOME_SENTINEL"
WIRE = b"UNKNOWN_PREFIX HOME_READY\tsecret=" + SECRET.encode() + b"\x01\r\n"
METADATA_KEYS = {
    "job_id",
    "plan_hash",
    "broker_epoch",
    "principal_id",
    "diagnostic_id",
    "state",
    "remaining_seconds",
    "wire_bytes",
    "limit_bytes",
    "shape",
}


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Grant:
    def __init__(self):
        self.live = True
        self.scopes = {"read", "control"}
        self.calls = []

    def __call__(self, principal, origin, scopes):
        self.calls.append((principal, origin, scopes))
        return (
            self.live and principal == PRINCIPAL and origin == "local_cli" and scopes <= self.scopes
        )


@asynccontextmanager
async def running(tmp_path, *, home=WIRE, backend_kind="serial", **port_options):
    backend, port, _ = device(clock_step=0.01, home=home, **port_options)
    clock, grant = Clock(), Grant()
    settings = Settings(
        state_dir=tmp_path / "state",
        **({"backend": "serial", "serial": backend.config} if backend_kind == "serial" else {}),
    )
    broker = Broker(
        settings,
        backend=backend if backend_kind == "serial" else None,
        serial_enabled=backend_kind == "serial",
        lock_path=tmp_path / "device.lockfile",
        clock=clock,
        principal_valid=grant,
    )
    await broker.start()
    env = SimpleNamespace(
        broker=broker,
        backend=broker.backend,
        port=port,
        clock=clock,
        grant=grant,
        approvals=PlanApprovals(broker),
    )
    try:
        yield env
    finally:
        if port.close_release is not None:
            port.close_release.set()
        await broker.close()


def prepare(env, *, job_id="private-home", test=Action.CAPTURE, principal=PRINCIPAL):
    broker = env.broker
    if broker.session is None:
        broker.acquire(
            AcquireRequest(owner=Origin.LOCAL, lease_seconds=300), principal_id=principal
        )
    plan = broker.prepare(
        PrepareRequest(session=broker.session.capability, job_id=job_id, test=test)
    )
    detail = env.approvals.detail(plan["plan_hash"])
    return plan["plan_hash"], detail["review_revision"]


def approve(env, plan_hash, revision, *, retain=True, attestation=ATTESTATION):
    return env.approvals.approve(plan_hash, revision, attestation, retain_local_home_line=retain)


async def execute(env, plan_hash):
    request = ExecuteRequest(session=env.broker.session.capability, plan_hash=plan_hash)
    env.broker.execute(request)
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    return request


def metadata(env, plan_hash, job_id="private-home"):
    return env.broker.operator_home_line_metadata(job_id, plan_hash)


def preview(env, row):
    return env.broker.operator_home_line(row["job_id"], row["plan_hash"], row["diagnostic_id"])


def assert_preview_unavailable(env, row):
    try:
        result = preview(env, row)
    except BrokerError as error:
        assert str(error) == "local_home_diagnostic_not_available"
    else:
        assert "escaped_line" not in result


def assert_no_private(value):
    rendered = json.dumps(value, default=str)
    assert SECRET not in rendered
    assert "UNKNOWN_PREFIX" not in rendered
    assert "escaped_line" not in rendered


def assert_no_durable_private(broker):
    assert_no_private("\n".join(broker._store().db.iterdump()))
    for path in broker.settings.state_dir.glob("history.sqlite3*"):
        assert SECRET.encode() not in path.read_bytes()


@pytest.mark.parametrize("retain", [False, True])
async def test_opt_in_keeps_mismatched_home_private_without_changing_capture(tmp_path, retain):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision, retain=retain)
        assert env.port.opens == 0
        await execute(env, plan_hash)
        job = env.broker.job("private-home")
        assert job["state"] == "needs_review"
        assert job["result"] == "serial_capture_timeout"
        assert env.port.writes == [b"\x1b"]
        assert env.port.opens == env.port.closes == 1
        assert not env.port.is_open and not env.backend.hardware_connected
        row = metadata(env, plan_hash)
        if retain:
            assert set(row) == METADATA_KEYS
            assert row["state"] == "available"
            assert row["job_id"] == "private-home" and row["plan_hash"] == plan_hash
            assert row["broker_epoch"] == env.broker.epoch and row["principal_id"] == PRINCIPAL
            assert row["wire_bytes"] == len(WIRE) - 1
            assert row["limit_bytes"] == 1024 and 0 < row["remaining_seconds"] <= 120
            private = preview(env, row)
            assert set(private) == METADATA_KEYS | {"escaped_line"}
            assert SECRET in private["escaped_line"]
            assert r"\x20" in private["escaped_line"]
            assert r"\t" in private["escaped_line"]
            assert r"\x01" in private["escaped_line"] and r"\r" in private["escaped_line"]
            assert all(33 <= ord(char) <= 126 for char in private["escaped_line"])
            assert_no_private(row)
        else:
            assert row is None
            assert env.broker.local_home_diagnostic is None
        timeline = OperatorTimeline(env.broker, env.approvals).snapshot()
        card = next(card for card in timeline["cards"] if card["job_id"] == "private-home")
        assert card.get("local_home_line") == row
        for public in (job, env.broker.status(), env.broker.read_logs(LogRequest()), timeline):
            assert_no_private(public)
        with pytest.raises(BrokerError):
            env.broker.read_capture("private-home", PRINCIPAL)
        assert_no_durable_private(env.broker)


async def test_omitted_opt_in_is_off_and_cannot_be_added_after_approval(tmp_path):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        env.approvals.approve(plan_hash, revision, ATTESTATION)
        assert metadata(env, plan_hash) is None
        # A replayed approval cannot silently turn a previously unselected option on.
        try:
            approve(env, plan_hash, revision)
        except BrokerError:
            pass
        assert metadata(env, plan_hash) is None
        await execute(env, plan_hash)
        assert metadata(env, plan_hash) is None
        assert_no_durable_private(env.broker)


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
async def test_direct_approval_requires_a_real_boolean(tmp_path, value):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        with pytest.raises(BrokerError):
            approve(env, plan_hash, revision, retain=value)
        assert not env.broker._store().db.execute("SELECT 1 FROM approvals").fetchall()
        assert env.broker.local_home_diagnostic is None and env.port.opens == 0


@pytest.mark.parametrize("kind", ["mock", "hello", "unauthenticated", "no-read"])
async def test_opt_in_rejects_non_manual_serial_capture_authority(tmp_path, kind):
    async with running(tmp_path, backend_kind="mock" if kind == "mock" else "serial") as env:
        test = Action.HELLO if kind in ("hello", "unauthenticated") else Action.CAPTURE
        principal = None if kind == "unauthenticated" else PRINCIPAL
        plan_hash, revision = prepare(env, test=test, principal=principal)
        if kind == "no-read":
            env.grant.scopes = {"control"}
        phrase = (
            ""
            if kind == "mock"
            else ("DEVICE FREE AND FIRMWARE VERIFIED" if test == Action.HELLO else ATTESTATION)
        )
        with pytest.raises(BrokerError):
            approve(env, plan_hash, revision, attestation=phrase)
        assert env.broker.local_home_diagnostic is None and env.port.opens == 0
        assert not env.broker._store().db.execute("SELECT 1 FROM approvals").fetchall()


@pytest.mark.parametrize("change", ["revoke", "scope", "lease-expire"])
async def test_queued_invalidation_never_opens_serial_or_retains_raw(tmp_path, change):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        env.broker.execute(
            ExecuteRequest(session=env.broker.session.capability, plan_hash=plan_hash)
        )
        # Synchronous enqueue followed by invalidation occurs before the worker can start.
        if change == "revoke":
            env.grant.live = False
        elif change == "scope":
            env.grant.scopes = {"read"}
        else:
            env.clock.now = env.broker.session.deadline
        await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        env.broker.maintenance_tick()
        assert env.broker.job("private-home")["state"] == "cancelled"
        assert env.port.opens == 0 and env.port.writes == []
        row = metadata(env, plan_hash)
        if row is not None:
            assert row["state"] in ("expired", "cleared")
            assert_preview_unavailable(env, row)
        assert_no_durable_private(env.broker)


async def test_repeat_approval_does_not_rearm_extend_deadline_or_replay_usb(tmp_path):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        first = metadata(env, plan_hash)
        env.clock.now += 10
        assert approve(env, plan_hash, revision)["already_approved"]
        second = metadata(env, plan_hash)
        assert first["diagnostic_id"] == second["diagnostic_id"]
        assert second["remaining_seconds"] <= first["remaining_seconds"] - 10
        request = await execute(env, plan_hash)
        complete = metadata(env, plan_hash)
        assert complete["diagnostic_id"] == first["diagnostic_id"]
        assert env.broker.execute(request)["deduplicated"]
        await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        assert env.port.opens == env.port.closes == 1
        assert env.port.writes == [b"\x1b"]
        with pytest.raises(BrokerError):
            approve(env, plan_hash, revision)
        assert metadata(env, plan_hash)["diagnostic_id"] == first["diagnostic_id"]


@pytest.mark.parametrize("mismatch", ["job", "hash", "diagnostic", "epoch", "principal"])
async def test_preview_rechecks_all_binding_fields(tmp_path, mismatch):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        await execute(env, plan_hash)
        row = metadata(env, plan_hash)
        assert row["state"] == "available"
        assert SECRET in preview(env, row)["escaped_line"]
        args = [row["job_id"], row["plan_hash"], row["diagnostic_id"]]
        if mismatch == "job":
            args[0] = "other-job"
        elif mismatch == "hash":
            args[1] = "f" * 64
        elif mismatch == "diagnostic":
            args[2] = "f" * 32
        elif mismatch == "epoch":
            env.broker.epoch = "f" * 32
        else:
            env.grant.live = False
        try:
            result = env.broker.operator_home_line(*args)
        except BrokerError as error:
            assert SECRET not in str(error)
        else:
            assert "escaped_line" not in result
        assert_no_durable_private(env.broker)


async def test_expiry_clears_existing_probe_and_restart_never_recovers_it(tmp_path):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        await execute(env, plan_hash)
        row = metadata(env, plan_hash)
        retained = env.broker.local_home_diagnostic
        assert SECRET in preview(env, row)["escaped_line"]
        env.clock.now += 120
        env.broker.maintenance_tick()
        try:
            expired = preview(env, row)
        except BrokerError:
            expired = {}
        assert "escaped_line" not in expired
        if retained.probe is not None:
            assert "escaped_line" not in retained.probe.preview()
        assert_no_durable_private(env.broker)
    async with running(tmp_path) as fresh:
        assert fresh.broker.epoch != row["broker_epoch"]
        assert metadata(fresh, plan_hash) is None
        assert fresh.broker.local_home_diagnostic is None
        assert fresh.port.opens == 0
        assert_no_durable_private(fresh.broker)


async def test_broker_close_clears_probe_even_when_an_old_reference_survives(tmp_path):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        await execute(env, plan_hash)
        retained = env.broker.local_home_diagnostic
        probe = retained.probe
        assert SECRET in probe.preview()["escaped_line"]
        await env.broker.close()
        assert env.broker.local_home_diagnostic is None
        assert "escaped_line" not in probe.preview()


async def test_completed_preview_survives_lease_release_without_extending_expiry(tmp_path):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        await execute(env, plan_hash)
        row = metadata(env, plan_hash)
        env.clock.now += 1.25
        env.broker.operator_release_idle(env.broker.session.id)
        assert env.broker.session is None
        released = metadata(env, plan_hash)
        assert released["diagnostic_id"] == row["diagnostic_id"]
        assert released["remaining_seconds"] <= row["remaining_seconds"] - 1
        assert SECRET in preview(env, released)["escaped_line"]
        assert env.port.opens == env.port.closes == 1


async def test_next_opt_in_replaces_single_entry_and_erases_old_probe(tmp_path):
    async with running(tmp_path, home=b"HOME_READY\n") as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        await execute(env, plan_hash)
        assert env.broker.job("private-home")["state"] == "succeeded"
        assert env.port.writes == [b"\x1b", b"s"]
        old = metadata(env, plan_hash)
        old_probe = env.broker.local_home_diagnostic.probe
        assert preview(env, old)["escaped_line"] == "HOME_READY"
        next_hash, next_revision = prepare(env, job_id="next-home")
        approve(env, next_hash, next_revision)
        current = metadata(env, next_hash, "next-home")
        assert current["state"] == "armed" and current["diagnostic_id"] != old["diagnostic_id"]
        assert metadata(env, plan_hash) is None
        assert "escaped_line" not in old_probe.preview()
        assert_preview_unavailable(env, old)
        assert env.port.opens == 1


async def test_second_pending_opt_in_cannot_replace_unconsumed_consent(tmp_path):
    async with running(tmp_path) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        old = metadata(env, plan_hash)
        next_hash, next_revision = prepare(env, job_id="next-home")
        with pytest.raises(BrokerError, match="local_home_diagnostic_already_armed"):
            approve(env, next_hash, next_revision)
        assert metadata(env, plan_hash) == old
        assert metadata(env, next_hash, "next-home") is None
        assert env.port.opens == 0


async def test_preexisting_secret_home_line_is_never_retained(tmp_path):
    async with running(tmp_path, preloaded=[WIRE], home=b"NO_ACK\n") as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        await execute(env, plan_hash)
        row = metadata(env, plan_hash)
        assert row["state"] == "no_matching_line" and row["wire_bytes"] == 0
        assert "escaped_line" not in preview(env, row)
        assert env.broker.job("private-home")["result"] == "serial_capture_timeout"
        assert env.port.writes == [b"\x1b"]
        assert_no_durable_private(env.broker)


async def test_oversized_candidate_has_no_excerpt_or_fallback_second_line(tmp_path):
    oversized = b"HOME_READY " + SECRET.encode() + b"x" * 1024 + b"\n"
    async with running(tmp_path, home=oversized + WIRE) as env:
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        await execute(env, plan_hash)
        row = metadata(env, plan_hash)
        assert row["state"] == "line_too_long"
        assert row["wire_bytes"] > 1024
        assert "escaped_line" not in preview(env, row)
        assert_no_private(row)
        assert env.port.writes == [b"\x1b"]
        assert_no_durable_private(env.broker)


async def test_active_revocation_erases_preview_but_holds_usb_until_confirmed_close(tmp_path):
    async with running(tmp_path) as env:
        env.port.close_release = threading.Event()
        plan_hash, revision = prepare(env)
        approve(env, plan_hash, revision)
        env.broker.execute(
            ExecuteRequest(session=env.broker.session.capability, plan_hash=plan_hash)
        )
        async with asyncio.timeout(5):
            while not env.port.close_started.is_set():
                await asyncio.sleep(0.001)
        lease_id = env.broker.session.id
        env.grant.live = False
        env.broker.maintenance_tick()
        assert env.port.is_open and env.backend.active == 1
        assert env.broker.active_job == "private-home"
        assert env.broker.session.id == lease_id
        assert env.broker._device_lock.is_locked
        with pytest.raises(BrokerError, match="active_job_cannot_release"):
            env.broker.operator_release_idle(lease_id)
        row = metadata(env, plan_hash)
        if row is not None:
            assert_preview_unavailable(env, row)
        env.port.close_release.set()
        await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        env.broker.maintenance_tick()
        assert env.port.closes == 1 and not env.port.is_open
        assert env.backend.active == 0 and env.broker.active_job is None
        assert env.broker.session is None
        assert env.port.writes == [b"\x1b"]
        assert_no_durable_private(env.broker)


@pytest.fixture
async def http_home(tmp_path):
    backend, port, _ = device(clock_step=0.01, home=WIRE)
    clock = Clock()
    settings = Settings(backend="serial", serial=backend.config, state_dir=tmp_path / "state")
    broker = Broker(
        settings,
        backend=backend,
        serial_enabled=True,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
    )
    registry = KeyRegistry(tmp_path / "keys.sqlite3")
    reader = registry.issue("Synthetic local reader", Origin.LOCAL, scopes={"read"})
    app = create_app(
        settings,
        broker,
        serial_enabled=True,
        auth=AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE}),
        key_registry_path=tmp_path / "keys.sqlite3",
    )
    inner = app
    while not hasattr(inner, "router"):
        inner = inner.app
    ready, shutdown = asyncio.Event(), asyncio.Event()

    async def lifespan():
        # AnyIO requires lifespan entry and exit to occur in the same asyncio task.
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
                http=http, broker=broker, port=port, reader=reader.token, clock=clock
            )
    finally:
        shutdown.set()
        await asyncio.wait_for(task, timeout=5)
        registry.close()


async def rpc(env, tool, request=None, *, token=LOCAL):
    response = await env.http.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {} if request is None else {"request": request}},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


async def http_prepare(env):
    acquired = (await rpc(env, "acquire_session", {"owner": "local_cli", "lease_seconds": 300}))[
        "structuredContent"
    ]
    prepared = (
        await rpc(
            env,
            "prepare_test",
            {"session": acquired["session"], "job_id": "private-home", "test": "capture_home"},
        )
    )["structuredContent"]
    assert prepared["ok"]
    detail = (
        await env.http.post("/operator/plan", json={"plan_hash": prepared["plan_hash"]})
    ).json()
    assert detail["ok"]
    return acquired["session"], {
        "plan_hash": prepared["plan_hash"],
        "review_revision": detail["review_revision"],
        "attestation": ATTESTATION,
    }


async def http_capture(env):
    session, approval = await http_prepare(env)
    approved = await env.http.post(
        "/operator/approve-plan", json={**approval, "retain_local_home_line": True}
    )
    assert approved.status_code == 200 and approved.json()["ok"], approved.text
    executed = (
        await rpc(env, "execute_test", {"session": session, "plan_hash": approval["plan_hash"]})
    )["structuredContent"]
    assert executed["ok"]
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    timeline = (await env.http.get("/operator/timeline")).json()
    assert_no_private(timeline)
    row = next(card for card in timeline["cards"] if card["job_id"] == "private-home")[
        "local_home_line"
    ]
    return {key: row[key] for key in ("job_id", "plan_hash", "diagnostic_id")}


async def test_http_local_preview_is_the_only_surface_with_private_bytes(http_home):
    env = http_home
    request = await http_capture(env)
    response = await env.http.post("/operator/local-home-line", json=request)
    assert response.status_code == 200 and response.json()["ok"]
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == METADATA_KEYS | {"ok", "escaped_line"}
    assert SECRET in response.json()["escaped_line"]
    for token in (LOCAL, REMOTE, env.reader):
        for tool, body in (
            ("device_status", None),
            ("job_status", {"job_id": "private-home"}),
            ("read_logs", {}),
            ("read_capture", {"job_id": "private-home"}),
        ):
            assert_no_private(await rpc(env, tool, body, token=token))
        for tool in ("local_home_line", "operator_home_line"):
            assert_no_private(await rpc(env, tool, request, token=token))
    assert env.port.writes == [b"\x1b"] and env.port.opens == env.port.closes == 1
    assert_no_durable_private(env.broker)


async def test_http_preview_denies_remote_readonly_and_loopback_bypasses(http_home):
    env = http_home
    request = await http_capture(env)
    for token, status in (("", 401), ("c" * 64, 401), (REMOTE, 403), (env.reader, 403)):
        response = await env.http.post(
            "/operator/local-home-line", json=request, headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == status
        assert_no_private(response.json())
    for headers in (
        {"Host": "hostile.invalid"},
        {"Origin": "https://hostile.invalid"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Forwarded": "for=127.0.0.1"},
    ):
        response = await env.http.post("/operator/local-home-line", json=request, headers=headers)
        assert response.status_code == 403
        assert SECRET not in response.text
    for path in (
        "/operator/local-home-line/",
        "/operator//local-home-line",
        "//operator/local-home-line",
        "/mcp/local-home-line",
    ):
        response = await env.http.post(path, json=request)
        assert response.status_code >= 400
        assert SECRET not in response.text
    for method in ("GET", "PUT", "DELETE"):
        response = await env.http.request(method, "/operator/local-home-line")
        assert response.status_code == 405
    response = await env.http.post("/operator/local-home-line?raw=true", json=request)
    assert response.status_code == 400
    assert env.port.opens == 1


async def test_http_opt_in_and_preview_bodies_are_strict_and_never_echo_input(http_home):
    env = http_home
    _, approval = await http_prepare(env)
    for value in (None, 0, 1, "true", [], {}):
        response = await env.http.post(
            "/operator/approve-plan", json={**approval, "retain_local_home_line": value}
        )
        assert response.status_code == 400
    assert env.port.opens == 0 and env.broker.local_home_diagnostic is None
    for body in (
        "{}",
        "null",
        "[]",
        SECRET,
        json.dumps({"job_id": "private-home", "plan_hash": approval["plan_hash"]}),
        json.dumps({"job_id": SECRET, "plan_hash": True, "diagnostic_id": "a" * 32}),
        '{"job_id":"private-home","job_id":"' + SECRET + '"}',
    ):
        response = await env.http.post(
            "/operator/local-home-line", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400
        assert SECRET not in response.text
    assert env.port.opens == 0
