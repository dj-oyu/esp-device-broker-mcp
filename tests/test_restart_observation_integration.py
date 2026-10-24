"""Explicit restart request and private report delivery across fake broker/MCP boundaries.

Every port, inventory entry, clock and credential is synthetic. These tests never
enumerate or open actual USB hardware and never send serial command bytes.
"""

import asyncio
import hashlib
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
from test_device_observation_integration import (
    LOCAL,
    LOG_LINE,
    PRINCIPAL,
    REMOTE,
    REMOTE_PRINCIPAL,
    SECRETS,
    WIRE,
    Clock,
    Grant,
    assert_no_durable_logs,
    assert_no_public_or_durable_logs,
    create_work,
    request_device,
    rpc,
    rpc_response,
)
from test_device_observation_integration import unavailable as no_ram_report
from test_serial_restart import device, disconnect_to, identity, observed

from cardputer_mcp import server as server_module
from cardputer_mcp.auth import AuthState
from cardputer_mcp.backend import BackendResult
from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.config import Settings
from cardputer_mcp.device_observation import DeviceLogCollector
from cardputer_mcp.key_registry import KeyRegistry
from cardputer_mcp.models import (
    AcquireRequest,
    CancelRequest,
    CloseWorkSessionRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.operator_timeline import OperatorTimeline
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.server import create_app

JOB = "restart-report"
RESULT = "restart_observation_completed_unverified"


@asynccontextmanager
async def running(tmp_path, *, wire=WIRE, reconnect=False):
    split = 80
    chunks = [wire[:split], OSError("SYNTHETIC_PRIVATE_DISCONNECT")] if reconnect else [wire]
    backend, ports, inventory, created = device(
        chunks=chunks, second_chunks=[wire[split:]] if reconnect else []
    )
    if reconnect:
        disconnect_to(ports, inventory)
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
            ports=ports,
            inventory=inventory,
            created=created,
            clock=clock,
            grant=grant,
            approvals=PlanApprovals(broker),
        )
    finally:
        await broker.close()
        assert_no_durable_logs(broker)


def prepare(env, job_id=JOB):
    if env.broker.session is None:
        env.broker.acquire(
            AcquireRequest(owner=Origin.LOCAL, lease_seconds=300), principal_id=PRINCIPAL
        )
    return env.broker.prepare(
        PrepareRequest(session=env.broker.session.capability, job_id=job_id, test=Action.RESTART)
    )


def validate_prepared(env, plan):
    """Read-only preflight; grants no execution authority."""
    return env.approvals.validate_current(plan["plan_hash"], env.broker.session)


async def execute(env, plan):
    request = ExecuteRequest(session=env.broker.session.capability, plan_hash=plan["plan_hash"])
    env.broker.execute(request)
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    return request


def unavailable(env, job_id=JOB, principal=PRINCIPAL):
    no_ram_report(env, job_id, principal)


def reset_count(env):
    # Both fake handles intentionally share the one ordered trace.
    return sum(event[1:] == ("rts", True) for event in env.ports[0].trace)


@pytest.mark.parametrize("reconnect", [False, True])
async def test_one_explicit_request_resets_once_and_delivers_only_after_close(
    tmp_path, caplog, reconnect
):
    async with running(tmp_path, reconnect=reconnect) as env:
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
        assert not env.created and env.broker.queue.empty() and reset_count(env) == 0
        validate_prepared(env, plan)
        unavailable(env)
        env.broker.execute(request)
        assert env.broker.execute(request)["deduplicated"]
        await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        expected = observed(WIRE)
        if reconnect:
            collector = DeviceLogCollector()
            collector.feed(WIRE[:80])
            collector.mark_gap()
            collector.feed(WIRE[80:])
            expected = replace(collector.finish(), reset_sequence_issued=True, capture_gap=True)
        report = env.broker.read_device_logs(JOB, PRINCIPAL)
        assert report == {
            "job_id": JOB,
            "sha256": expected.sha256,
            "remaining_seconds": 300,
            **expected.payload(),
        }
        assert report["reset_sequence_issued"] is True
        assert report["capture_gap"] is reconnect
        assert report["redacted_lines"] == 2 and LOG_LINE in report["logs"]
        assert report["firmware"]["source_verified"] is False
        assert report["firmware"]["binary_sha256_verified"] is False
        for _ in range(3):
            assert env.broker.read_device_logs(JOB, PRINCIPAL) == report
            assert env.broker.execute(request)["deduplicated"]
        await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        assert env.broker.job(JOB)["result"] == RESULT
        assert env.broker.job(JOB)["state"] == "succeeded"
        assert reset_count(env) == env.backend.calls == env.backend.max_active == 1
        assert len(env.created) == 1 + int(reconnect)
        assert all(port.opens == port.closes == 1 and not port.is_open for port in env.created)
        assert env.ports[0].discards == 1 and env.ports[1].discards == 0
        assert all(port.writes == [] for port in env.ports)
        metadata = env.broker.operator_device_observation_metadata(JOB)
        assert metadata["metadata"] == expected.metadata() and "logs" not in metadata
        assert LOG_LINE not in json.dumps(OperatorTimeline(env.broker, env.approvals).snapshot())
        assert LOG_LINE not in caplog.text and all(secret not in caplog.text for secret in SECRETS)
        assert all(secret not in json.dumps(report) for secret in SECRETS)
        assert_no_public_or_durable_logs(env.broker, request.session, LOCAL, REMOTE)


async def test_anonymous_lease_and_capture_policy_cannot_authorize_restart(tmp_path):
    async with running(tmp_path) as env:
        anonymous = env.broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        with pytest.raises(BrokerError, match="^authentication_required$"):
            env.broker.prepare(
                PrepareRequest(session=anonymous["session"], job_id=JOB, test=Action.RESTART)
            )
        env.broker.release(LeaseRequest(session=anonymous["session"]))
        calls = []
        env.broker.capture_policies = SimpleNamespace(
            authorize_capture=lambda *args, **kwargs: calls.append("authorize") or object(),
            validate_capture=lambda *args, **kwargs: calls.append("validate") or True,
            invalidate_session=lambda: None,
            target=lambda: "synthetic-target",
        )
        plan = prepare(env)
        assert not plan["approval_required"] and "policy_permit_available" not in plan
        assert calls == [] and not env.created
        await execute(env, plan)
        assert calls == [] and reset_count(env) == 1


# rehash=True re-signs the changed plan, so every field must be refused by the
# plan-content validation itself.  rehash=False leaves the stored hash stale and
# is refused by the digest binding before any field is read, so one field per
# location (restart / log_delivery / top level) proves that path.
DIGEST_ONLY_FIELDS = {
    ("restart", "method"),
    ("log_delivery", "principal_id"),
    (None, "device_fingerprint"),
}


def _tamper_cases():
    fields = [
        ("restart", "method", "generic_dtr_reset"),
        ("restart", "attempts", 2),
        ("log_delivery", "principal_id", REMOTE_PRINCIPAL),
        ("log_delivery", "origin", "remote_mcp"),
        (None, "device_fingerprint", "0" * 64),
    ]
    for section, field, value in fields:
        yield pytest.param(True, section, field, value)
        if (section, field) in DIGEST_ONLY_FIELDS:
            yield pytest.param(False, section, field, value)


@pytest.mark.parametrize("rehash,section,field,value", list(_tamper_cases()))
async def test_changed_reset_identity_recipient_or_bounds_cannot_execute(
    tmp_path, rehash, section, field, value
):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        changed = json.loads(json.dumps(plan["plan"]))
        (changed if section is None else changed[section])[field] = value
        digest = digest_plan(changed) if rehash else plan["plan_hash"]
        if rehash:
            env.broker._store().put_plan(digest, changed)
        else:
            env.broker._store().db.execute(
                "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(changed), digest)
            )
        with pytest.raises(BrokerError):
            env.approvals.validate_current(digest, env.broker.session)
        with pytest.raises(BrokerError):
            env.broker.execute(
                ExecuteRequest(session=env.broker.session.capability, plan_hash=digest)
            )
        assert not env.created and env.broker.queue.empty() and reset_count(env) == 0
        unavailable(env)


async def test_changed_port_identity_fails_preflight_without_reset(tmp_path):
    async with running(tmp_path) as env:
        plan = prepare(env)
        env.inventory[:] = [identity(port="SYNTHETIC_COM99")]
        with pytest.raises(BrokerError, match="^plan_backend_mismatch$"):
            await execute(env, plan)
        assert not env.created and reset_count(env) == 0
        unavailable(env)


@pytest.mark.parametrize("invalidate", ["revoke", "lease_expiry"])
async def test_queued_restart_rechecks_authority_before_opening_usb(tmp_path, invalidate):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        env.broker.execute(
            ExecuteRequest(session=env.broker.session.capability, plan_hash=plan["plan_hash"])
        )
        if invalidate == "revoke":
            env.grant.live = False
        else:
            env.clock.now += 301
        await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        assert env.broker.job(JOB)["state"] == "cancelled"
        assert env.backend.calls == 0 and not env.created and reset_count(env) == 0
        unavailable(env)


@pytest.mark.parametrize("phase", ["reset", "intermediate_close", "reopen", "final_close"])
@pytest.mark.parametrize("cancel_worker", [False, True])
async def test_no_second_owner_during_reset_reopen_or_close_even_after_cancellation(
    tmp_path, phase, cancel_worker
):
    async with running(tmp_path, reconnect=True) as env:
        local, remote = create_work(env), create_work(env, origin=Origin.REMOTE)
        lease = request_device(env, local)["lease"]
        plan = prepare(env)
        validate_prepared(env, plan)
        request_device(env, remote, origin=Origin.REMOTE)
        entered, release = threading.Event(), threading.Event()

        def block():
            entered.set()
            assert release.wait(timeout=5)

        if phase == "reset":
            pause = env.backend.pause

            def blocked_pause(seconds):
                block()
                pause(seconds)

            env.backend.pause = blocked_pause
        elif phase == "reopen":
            open_port = env.ports[1].open

            def blocked_open():
                block()
                open_port()

            env.ports[1].open = blocked_open
        else:
            env.ports[int(phase == "final_close")].close_hook = block
        try:
            env.broker.execute(
                ExecuteRequest(session=lease["session"], plan_hash=plan["plan_hash"])
            )
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(2 if cancel_worker else 1):
                if cancel_worker:
                    env.broker.worker.cancel()
                    await asyncio.sleep(0)
                assert env.broker.job(JOB)["state"] == "running"
                assert env.backend.active == 1 and env.broker.active_job == JOB
                assert env.broker.session.principal_id == PRINCIPAL
                assert len(env.broker.waiters) == 1 and env.broker._device_lock.is_locked
                assert not env.broker.worker.done() and reset_count(env) == 1
                with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                    env.broker.release(LeaseRequest(session=lease["session"]))
                with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                    env.broker.close_work_session(
                        CloseWorkSessionRequest(resume_token=local["resume_token"]),
                        principal_id=PRINCIPAL,
                        scopes=frozenset({"read", "control"}),
                    )
                with pytest.raises(BrokerError, match="^only_queued_jobs_can_be_cancelled$"):
                    env.broker.cancel(CancelRequest(session=lease["session"], job_id=JOB))
                with pytest.raises(BrokerError, match="^device_busy$"):
                    env.broker.acquire(
                        AcquireRequest(owner=Origin.REMOTE), principal_id=REMOTE_PRINCIPAL
                    )
                unavailable(env)
                unavailable(env, principal=REMOTE_PRINCIPAL)
                assert env.broker.operator_device_observation_metadata(JOB) is None
        finally:
            release.set()
            await asyncio.wait_for(env.broker.queue.join(), timeout=5)
        assert env.backend.active == 0 and env.backend.max_active == reset_count(env) == 1
        assert all(not port.is_open and port.closes == 1 for port in env.created)
        assert len(env.created) == 2 and all(port.writes == [] for port in env.ports)
        assert len(env.ports[0].owner_threads | env.ports[1].owner_threads) == 1
        if cancel_worker:
            assert env.broker.job(JOB)["state"] == "needs_review"
            assert env.broker.job(JOB)["result"] == "operation_interrupted_review_required"
            unavailable(env)
            assert env.broker.session.principal_id == PRINCIPAL
        else:
            assert env.broker.read_device_logs(JOB, PRINCIPAL)["capture_gap"] is True
            env.broker.release(LeaseRequest(session=lease["session"]))
            assert env.broker.session.principal_id == REMOTE_PRINCIPAL
            unavailable(env, principal=REMOTE_PRINCIPAL)
        assert_no_public_or_durable_logs(env.broker, lease["session"])


@pytest.mark.parametrize("phase", ["intermediate_close", "final_close"])
@pytest.mark.parametrize("failure", ["close_error", "still_open"])
async def test_uncertain_close_withholds_restart_report_and_retains_owner_lock(
    tmp_path, phase, failure
):
    async with running(tmp_path, reconnect=True) as env:
        local, remote = create_work(env), create_work(env, origin=Origin.REMOTE)
        lease = request_device(env, local)["lease"]
        plan = prepare(env)
        validate_prepared(env, plan)
        request_device(env, remote, origin=Origin.REMOTE)
        setattr(env.ports[int(phase == "final_close")], failure, True)
        try:
            await execute(env, plan)
            assert env.broker.job(JOB)["state"] == "needs_review"
            assert env.broker.job(JOB)["result"] == "serial_close_unconfirmed"
            assert len(env.created) == (2 if phase == "final_close" else 1)
            assert reset_count(env) == 1 and env.backend.active == 1
            assert env.broker._device_lock.is_locked and env.backend.ownership_uncertain
            assert env.broker.session.principal_id == PRINCIPAL
            assert len(env.broker.waiters) == 1
            unavailable(env)
            unavailable(env, principal=REMOTE_PRINCIPAL)
            assert env.broker.operator_device_observation_metadata(JOB) is None
            assert not env.broker.device_observations
            with pytest.raises(BrokerError, match="^active_job_cannot_release$"):
                env.broker.release(LeaseRequest(session=lease["session"]))
            with pytest.raises(BrokerError, match="^device_close_unconfirmed$"):
                await env.broker.close()
            assert env.broker._device_lock.is_locked and env.backend.active == 1
            assert all(port.closes == 1 and port.writes == [] for port in env.created)
            assert_no_public_or_durable_logs(env.broker, lease["session"])
        finally:
            # Clear synthetic state for fixture teardown; no hardware was opened.
            env.backend.active = 0
            env.backend.hardware_connected = False
            env.backend.ownership_uncertain = False
            env.broker.active_job = None


@pytest.mark.parametrize(
    "change",
    [
        {"reset_sequence_issued": False},
        {"reset_sequence_issued": 1},
        {"reset_sequence_issued": None},
        {"capture_gap": 1},
        {"capture_gap": "true"},
    ],
)
async def test_malformed_restart_result_cannot_publish_logs(tmp_path, change):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)

        async def result_without_hardware(plan, stage):
            return BackendResult(RESULT, observation=replace(observed(WIRE), **change))

        env.backend.execute = result_without_hardware
        await execute(env, plan)
        assert env.broker.job(JOB)["state"] == "needs_review"
        unavailable(env)
        assert not env.broker.device_observations and not env.created
        assert_no_public_or_durable_logs(env.broker)


async def test_report_reads_never_reset_or_extend_ttl_and_broker_restart_drops_cache(tmp_path):
    async with running(tmp_path) as env:
        plan = prepare(env)
        validate_prepared(env, plan)
        request = await execute(env, plan)
        epoch = env.broker.epoch
        env.broker.release(LeaseRequest(session=request.session))
        for now, remaining in ((100, 300), (250, 150), (399, 1)):
            env.clock.now = now
            assert env.broker.read_device_logs(JOB, PRINCIPAL)["remaining_seconds"] == remaining
            assert reset_count(env) == 1 and env.backend.calls == 1
        env.clock.now = 400
        unavailable(env)
        assert not env.broker.device_observations and env.backend.calls == 1
    async with running(tmp_path) as fresh:
        assert fresh.broker.epoch != epoch
        unavailable(fresh)
        assert fresh.broker.job(JOB)["state"] == "succeeded"
        assert fresh.broker.queue.empty() and not fresh.created and reset_count(fresh) == 0


@pytest.mark.parametrize("failure", ["preflight", "discard", "control", "read"])
async def test_private_exception_messages_are_never_returned_or_persisted(
    tmp_path, caplog, failure
):
    async with running(tmp_path) as env:
        plan = prepare(env)
        private = LOG_LINE + SECRETS[0] + LOCAL + "/private/fake-port"
        if failure == "preflight":

            def fail_preflight(_test):
                raise RuntimeError(private)

            env.backend.prepare = fail_preflight
            with pytest.raises(BrokerError, match="^backend_preflight_failed$"):
                validate_prepared(env, plan)
            assert not env.created
        else:
            validate_prepared(env, plan)
            if failure == "discard":

                def fail_discard():
                    raise OSError(private)

                env.ports[0].reset_input_buffer = fail_discard
            elif failure == "control":
                env.ports[0].control_error = 4
            else:
                env.ports[0].chunks.append(RuntimeError(private))
            await execute(env, plan)
            # Review classification fix: a typed restart failure with a confirmed close
            # is `failed` (was needs_review).
            assert env.broker.job(JOB)["state"] == "failed"
            assert reset_count(env) <= 1
            assert env.ports[0].closes == 1 and env.ports[0].writes == []
        unavailable(env)
        assert all(value not in caplog.text for value in (LOG_LINE, *SECRETS, LOCAL))
        assert_no_public_or_durable_logs(env.broker, LOCAL, "/private/fake-port")


@pytest.fixture
async def http_restart(tmp_path, monkeypatch):
    backend, ports, inventory, created = device(chunks=[WIRE])
    clock, key_clock = Clock(), Clock()
    path = tmp_path / "keys.sqlite3"
    registry = KeyRegistry(path, clock=key_clock)
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
        key_registry_path=path,
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
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url=f"http://127.0.0.1:{settings.port}",
            trust_env=False,
            headers={"Authorization": f"Bearer {LOCAL}"},
        ) as http:
            yield SimpleNamespace(
                http=http,
                broker=broker,
                backend=backend,
                ports=ports,
                inventory=inventory,
                created=created,
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


async def http_observe(env):
    lease = (
        await rpc(env, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 300})
    )["structuredContent"]
    plan = (
        await rpc(
            env,
            "submit",
            {"session": lease["session"], "job_id": JOB, "kind": "restart"},
        )
    )["structuredContent"]
    assert plan["ok"] and not plan["approval_required"]
    request = {"op": "run", "session": lease["session"], "plan_hash": plan["plan_hash"]}
    assert (await rpc(env, "execute", request))["structuredContent"]["ok"]
    await asyncio.wait_for(env.broker.queue.join(), timeout=5)
    return lease["session"], plan


async def test_authenticated_mcp_restart_requires_explicit_request_and_exact_report_recipient(
    http_restart,
):
    env = http_restart
    catalog = await rpc_response(env, "tools/list", {}, token=env.keys["owner"].token)
    assert catalog.status_code == 200
    tools = catalog.json()["result"]["tools"]
    preparation = next(item for item in tools if item["name"] == "submit")
    assert "restart" in json.dumps(preparation["inputSchema"]["$defs"]["Kind"])
    reader = next(item for item in tools if item["name"] == "status")
    assert reader["annotations"]["readOnlyHint"] is True
    assert not any("approv" in item["name"] or "operator" in item["name"] for item in tools)
    session, plan = await http_observe(env)
    report = (await rpc(env, "status", {"view": "logs", "stream": "device", "job_id": JOB}))[
        "structuredContent"
    ]
    assert report["ok"] and LOG_LINE in report["logs"]
    assert report["reset_sequence_issued"] is True and report["capture_gap"] is False
    payload = {
        key: value
        for key, value in report.items()
        if key not in {"ok", "job_id", "sha256", "remaining_seconds"}
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert hashlib.sha256(canonical).hexdigest() == report["sha256"]
    for token in (LOCAL, REMOTE, env.keys["other"].token, env.keys["reader"].token):
        assert (
            await rpc(
                env, "status", {"view": "logs", "stream": "device", "job_id": JOB}, token=token
            )
        )["structuredContent"] == {"ok": False, "error": "device_logs_not_available"}
    for _ in range(3):
        assert (
            await rpc(
                env,
                "execute",
                {"op": "run", "session": session, "plan_hash": plan["plan_hash"]},
            )
        )["structuredContent"]["deduplicated"]
    assert (await rpc(env, "session", {"action": "release", "session": session}))[
        "structuredContent"
    ]["released"]
    for _ in range(3):
        assert (await rpc(env, "status", {"view": "logs", "stream": "device", "job_id": JOB}))[
            "structuredContent"
        ] == report
    assert reset_count(env) == env.backend.calls == 1 and len(env.created) == 1
    assert env.ports[0].opens == env.ports[0].closes == 1 and env.ports[0].writes == []
    assert plan["plan"]["log_delivery"]["principal_id"] == env.keys["owner"].metadata.principal_id
    timeline = await env.http.get("/operator/timeline")
    assert timeline.status_code == 200 and LOG_LINE not in timeline.text
    for tool, request in (
        ("status", {"view": "device"}),
        ("status", {"view": "logs", "stream": "broker"}),
        ("status", {"view": "job", "job_id": JOB}),
    ):
        public = await rpc(env, tool, request, token=env.keys["reader"].token)
        assert LOG_LINE not in json.dumps(public)
    assert_no_public_or_durable_logs(env.broker, session, *(key.token for key in env.keys.values()))


@pytest.mark.parametrize("invalidate", ["revoke", "expire", "rotate"])
async def test_mcp_revalidates_key_after_restart_completion(http_restart, invalidate):
    env = http_restart
    await http_observe(env)
    key = env.keys["owner"]
    assert (await rpc(env, "status", {"view": "logs", "stream": "device", "job_id": JOB}))[
        "structuredContent"
    ]["ok"]
    if invalidate == "revoke":
        assert env.registry.revoke(key.metadata.key_id)
    elif invalidate == "expire":
        env.key_clock.now += 11
    else:
        replacement = env.registry.rotate(key.metadata.key_id)
        assert (
            await rpc(
                env,
                "status",
                {"view": "logs", "stream": "device", "job_id": JOB},
                token=replacement.token,
            )
        )["structuredContent"] == {"ok": False, "error": "device_logs_not_available"}
    response = await rpc_response(
        env,
        "tools/call",
        {
            "name": "status",
            "arguments": {"request": {"view": "logs", "stream": "device", "job_id": JOB}},
        },
        token=key.token,
    )
    assert response.status_code == 401 and response.json() == {"error": "authentication_required"}
    assert env.broker.operator_device_observation_metadata(JOB) is None
    assert not env.broker.device_observations and env.broker.job(JOB)["state"] == "succeeded"
    assert reset_count(env) == env.backend.calls == 1
    assert_no_public_or_durable_logs(env.broker, key.token)


async def test_mcp_invalid_controls_and_read_arguments_are_rejected_without_usb(http_restart):
    env = http_restart
    response = await rpc_response(
        env,
        "tools/call",
        {
            "name": "status",
            "arguments": {"request": {"view": "logs", "stream": "device", "job_id": JOB}},
        },
        token="invalid",
    )
    assert response.status_code == 401 and response.json() == {"error": "authentication_required"}
    lease = (
        await rpc(env, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30})
    )["structuredContent"]
    for extra in (
        {"method": "generic_reset"},
        {"attempts": 2},
        {"port": "/private/fake-port"},
        {"log_delivery": {"principal_id": PRINCIPAL}},
    ):
        result = await rpc(
            env,
            "submit",
            {"session": lease["session"], "job_id": JOB, "kind": "restart", **extra},
        )
        assert result["isError"] is True
        assert "/private/fake-port" not in json.dumps(result)
        assert lease["session"] not in json.dumps(result)
    base = {"view": "logs", "stream": "device", "job_id": JOB}
    for request in (
        {**base, "principal_id": PRINCIPAL},
        {**base, "job_id": LOG_LINE + "\n"},
        {**base, "session": LOCAL},
    ):
        result = await rpc(env, "status", request)
        assert result["isError"] is True
        assert LOG_LINE not in json.dumps(result) and LOCAL not in json.dumps(result)
    assert not env.created and reset_count(env) == 0 and env.backend.calls == 0
