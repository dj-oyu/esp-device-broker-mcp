"""Optional mock authentication over localhost HTTP, using synthetic token values only."""

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest
import uvicorn
from server_ready import LOG_LEVEL, wait_until_started

from cardputer_mcp import cli
from cardputer_mcp.auth import AuthError, AuthState
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import Settings
from cardputer_mcp.models import Origin
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64


@pytest.fixture(scope="module")
def event_loop_policy():
    # CPython's Windows Proactor transport can raise from socket.shutdown
    # before detaching a reset connection. A test server then cannot finish
    # wait_closed(), even after Uvicorn's graceful-drain deadline.
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(params=["environment", "injected"])
async def authenticated_mock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[tuple[str, Broker]]:
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
    auth = None
    if request.param == "environment":
        monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", LOCAL)
        monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", REMOTE)
    else:
        monkeypatch.delenv("CARDPUTER_LOCAL_TOKEN", raising=False)
        monkeypatch.delenv("CARDPUTER_REMOTE_TOKEN", raising=False)
        auth = AuthState({Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE})
    app = create_app(settings, resident, require_auth=True, auth=auth)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level=LOG_LEVEL,
            access_log=False,
            proxy_headers=False,
            # A rejected HTTP stream can keep a connection task open after the
            # client closes. Bound its drain so lifespan still shuts down.
            timeout_graceful_shutdown=2,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        await wait_until_started(server, task, "Authenticated mock server")
        yield f"http://127.0.0.1:{port}/mcp", resident
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        assert task.done() and not server.server_state.connections
    assert not (settings.state_dir / "runtime-auth.json").exists()


async def test_authenticated_mock_rejects_missing_invalid_and_external_requests(
    authenticated_mock: tuple[str, Broker],
) -> None:
    if os.name == "nt":
        assert isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)
    url, _ = authenticated_mock
    async with httpx2.AsyncClient(trust_env=False) as http:
        for headers in ({}, {"Authorization": "Bearer " + "c" * 64}):
            rejected = await http.post(url, json={}, headers=headers)
            assert rejected.status_code == 401
            assert rejected.json() == {"error": "authentication_required"}
        for headers in (
            {"Origin": "https://hostile.invalid"},
            {"X-Forwarded-For": "127.0.0.1"},
            {"Host": "hostile.invalid"},
        ):
            rejected = await http.post(
                url, json={}, headers={"Authorization": f"Bearer {LOCAL}", **headers}
            )
            assert rejected.status_code == 403
            assert rejected.json() == {"error": "local_only"}


async def test_authenticated_mock_binds_all_session_actions_to_origin(
    authenticated_mock: tuple[str, Broker],
) -> None:
    url, resident = authenticated_mock
    async with connect(url, LOCAL) as local, connect(url, REMOTE) as remote:
        status = await call(local, "status", {"view": "device"})
        assert status["backend"] == "mock"
        assert status["hardware_connected"] is False
        assert status["tunnel"] == "external_lifecycle_unknown"
        spoof = await call(
            remote, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
        )
        assert spoof == {"ok": False, "error": "authenticated_owner_mismatch"}
        lease = await call(
            local, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 30}
        )
        session = lease["session"]
        prepared = await call(
            local,
            "submit",
            {"kind": "diagnostics", "session": session, "job_id": "authenticated-mock"},
        )
        assert prepared["ok"]
        actions = {
            "session:renew": ("session", {"action": "renew"}),
            "session:release": ("session", {"action": "release"}),
            "submit": ("submit", {"kind": "diagnostics", "job_id": "wrong-origin"}),
            "execute": ("execute", {"op": "run", "plan_hash": prepared["plan_hash"]}),
            "cancel": ("cancel", {"target": "job", "job_id": "authenticated-mock"}),
        }
        for label, (tool, fields) in actions.items():
            stolen = await call(remote, tool, {"session": session, **fields})
            assert stolen == {"ok": False, "error": "authenticated_owner_mismatch"}, label
        assert (await call(local, "session", {"action": "renew", "session": session}))["renewed"]
        executed = await call(
            local, "execute", {"op": "run", "session": session, "plan_hash": prepared["plan_hash"]}
        )
        assert executed["ok"]
        await resident.queue.join()
        done = await call(remote, "status", {"view": "job", "job_id": "authenticated-mock"})
        assert done["state"] == "succeeded"
        logs = await call(remote, "status", {"view": "logs", "stream": "broker"})
        for credential in (LOCAL, REMOTE, session):
            assert credential not in str(status) + str(logs) + str(done)
        assert (await call(local, "session", {"action": "release", "session": session}))["released"]
        acquired = await call(
            remote, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        assert acquired["ok"]
        rejected = await call(
            local, "session", {"action": "release", "session": acquired["session"]}
        )
        assert rejected["error"] == "authenticated_owner_mismatch"
        assert (
            await call(remote, "session", {"action": "release", "session": acquired["session"]})
        )["released"]


async def test_authenticated_mock_missing_environment_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CARDPUTER_LOCAL_TOKEN", raising=False)
    monkeypatch.delenv("CARDPUTER_REMOTE_TOKEN", raising=False)
    settings = Settings(state_dir=tmp_path / "state")
    resident = Broker(settings, lock_path=tmp_path / "device.lockfile")
    app = create_app(settings, resident, require_auth=True)
    inner = app
    while not hasattr(inner, "router"):
        inner = inner.app
    with pytest.raises(AuthError, match="^runtime_environment_credentials_unavailable$"):
        async with inner.router.lifespan_context(inner):
            pytest.fail("Authenticated mock startup accepted missing credentials")
    assert resident.store is None
    assert not (settings.state_dir / "runtime-auth.json").exists()


@pytest.mark.parametrize("options", [["--auth-source", "none"]])
def test_cli_require_auth_needs_explicit_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], options: list[str]
) -> None:
    create = Mock()
    run = Mock()
    monkeypatch.setattr(cli, "create_app", create)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr("sys.argv", ["cardputer", *options, "serve", "--require-auth"])
    with pytest.raises(SystemExit, match="^1$"):
        cli.main()
    assert capsys.readouterr().err == "Request failed: auth_source_required\n"
    create.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "require_auth", "source"),
    [
        (["--auth-source", "environment", "serve", "--require-auth"], True, "environment"),
        (["--auth-source", "file", "serve", "--require-auth"], True, "file"),
        (["serve"], False, "environment"),
    ],
)
def test_cli_require_auth_passes_explicit_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    require_auth: bool,
    source: str,
) -> None:
    settings = Settings(state_dir=tmp_path / "state")
    create = Mock(return_value=object())
    run = Mock()
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "create_app", create)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr("sys.argv", ["cardputer", *argv])
    cli.main()
    create.assert_called_once_with(
        settings,
        serial_enabled=False,
        require_auth=require_auth,
        key_registry_path=None,
        auth_source=source,
    )
    run.assert_called_once()
    assert run.call_args.kwargs["proxy_headers"] is False


async def test_status_reports_default_ingress_disabled_but_external_lifecycle_unknown(
    authenticated_mock,
):
    url, _ = authenticated_mock
    async with connect(url, REMOTE) as client:
        status = await call(client, "status", {"view": "device"})
    assert status["tunnel_ingress_enabled"] is False
    assert status["tunnel"] == "external_lifecycle_unknown"
