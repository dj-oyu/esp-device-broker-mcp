"""Optional mock authentication over localhost HTTP, using synthetic token values only."""

import asyncio
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import Mock

import httpx2
import pytest
import uvicorn

from cardputer_mcp import cli
from cardputer_mcp.auth import AuthError, AuthState
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import Settings
from cardputer_mcp.models import Origin
from cardputer_mcp.server import create_app

LOCAL = "a" * 64
REMOTE = "b" * 64


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
            log_level="critical",
            access_log=False,
            proxy_headers=False,
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("Authenticated mock server stopped before readiness")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/mcp", resident
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
    assert not (settings.state_dir / "runtime-auth.json").exists()


async def test_authenticated_mock_rejects_missing_invalid_and_external_requests(
    authenticated_mock: tuple[str, Broker],
) -> None:
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
        status = await call(local, "device_status")
        assert status["backend"] == "mock"
        assert status["hardware_connected"] is False
        assert status["tunnel"] == "external_lifecycle_unknown"
        spoof = await call(remote, "acquire_session", {"owner": "local_cli"})
        assert spoof == {"ok": False, "error": "authenticated_owner_mismatch"}
        lease = await call(local, "acquire_session", {"owner": "local_cli"})
        session = lease["session"]
        prepared = await call(
            local, "prepare_test", {"session": session, "job_id": "authenticated-mock"}
        )
        assert prepared["ok"]
        actions = {
            "renew_session": {},
            "release_session": {},
            "prepare_test": {"job_id": "wrong-origin"},
            "execute_test": {"plan_hash": prepared["plan_hash"]},
            "cancel_job": {"job_id": "authenticated-mock"},
        }
        for tool, fields in actions.items():
            stolen = await call(remote, tool, {"session": session, **fields})
            assert stolen == {"ok": False, "error": "authenticated_owner_mismatch"}, tool
        assert (await call(local, "renew_session", {"session": session}))["renewed"]
        executed = await call(
            local, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
        )
        assert executed["ok"]
        await resident.queue.join()
        done = await call(remote, "job_status", {"job_id": "authenticated-mock"})
        assert done["state"] == "succeeded"
        logs = await call(remote, "read_logs", {})
        for credential in (LOCAL, REMOTE, session):
            assert credential not in str(status) + str(logs) + str(done)
        assert (await call(local, "release_session", {"session": session}))["released"]
        acquired = await call(remote, "acquire_session", {"owner": "remote_mcp"})
        assert acquired["ok"]
        rejected = await call(local, "release_session", {"session": acquired["session"]})
        assert rejected["error"] == "authenticated_owner_mismatch"
        assert (await call(remote, "release_session", {"session": acquired["session"]}))["released"]


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


@pytest.mark.parametrize("options", [[], ["--auth-source", "none"]])
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


@pytest.mark.parametrize("source", ["environment", "file"])
def test_cli_require_auth_passes_explicit_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    settings = Settings(state_dir=tmp_path / "state")
    create = Mock(return_value=object())
    run = Mock()
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "create_app", create)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(
        "sys.argv", ["cardputer", "--auth-source", source, "serve", "--require-auth"]
    )
    cli.main()
    create.assert_called_once_with(
        settings,
        serial_enabled=False,
        require_auth=True,
        key_registry_path=None,
        auth_source=source,
    )
    run.assert_called_once()
    assert run.call_args.kwargs["proxy_headers"] is False


def test_cli_legacy_mock_remains_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(state_dir=tmp_path / "state")
    create = Mock(return_value=object())
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "create_app", create)
    monkeypatch.setattr(cli.uvicorn, "run", Mock())
    monkeypatch.setattr("sys.argv", ["cardputer", "serve"])
    cli.main()
    create.assert_called_once_with(
        settings,
        serial_enabled=False,
        require_auth=False,
        key_registry_path=None,
        auth_source="environment",
    )


async def test_status_reports_default_ingress_disabled_but_external_lifecycle_unknown(
    authenticated_mock,
):
    url, _ = authenticated_mock
    async with connect(url, REMOTE) as client:
        status = await call(client, "device_status")
    assert status["tunnel_ingress_enabled"] is False
    assert status["tunnel"] == "external_lifecycle_unknown"
