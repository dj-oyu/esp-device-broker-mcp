"""Headless GUI lifecycle tests; no native client, account, hardware, or Tk display."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from cardputer_mcp.gui_tunnel import (
    CONFIG_FAILED,
    REQUEST_FAILED,
    REVIEW_WARNING,
    TunnelController,
    TunnelOperation,
    TunnelOutcome,
    TunnelPanel,
    TunnelView,
    perform_tunnel,
    runtime_remote_key,
)
from cardputer_mcp.tunnel import TunnelSettings

URL = "http://127.0.0.1:8765/mcp"
API_KEY = "synthetic-api-key"
REMOTE_KEY = "a" * 64


def snapshot(**changes: Any) -> dict[str, Any]:
    return {
        "state": "disconnected",
        "status_code": "tunnel_configured",
        "configured": True,
        "owned": False,
        "process_alive": False,
        "local_readiness": False,
        "cloud_poll_observed": False,
        "app_verified": False,
        "ownership_uncertain": False,
        "exit_code": None,
        **changes,
    }


class FakeManager:
    def __init__(self) -> None:
        self.data = snapshot()
        self.calls: list[str] = []
        self.threads: list[int] = []
        self.failure: Exception | None = None
        self.close_ok = True

    def configure(
        self, settings: TunnelSettings, broker_url: str, *, enable_serial: bool = False
    ) -> dict[str, Any]:
        self.calls.append("configure")
        assert broker_url == URL
        self.data = snapshot()
        return self.snapshot()

    def connect(self, api_key: str, bearer: str) -> dict[str, Any]:
        self.calls.append("connect")
        self.threads.append(threading.get_ident())
        if self.failure:
            raise self.failure
        assert api_key == API_KEY and bearer == "Bearer " + REMOTE_KEY
        self.data = snapshot(
            state="running", status_code="tunnel_started", owned=True, process_alive=True
        )
        return self.snapshot()

    def poll(self) -> dict[str, Any]:
        self.calls.append("poll")
        return self.snapshot()

    def disconnect(self) -> dict[str, Any]:
        self.calls.append("disconnect")
        if self.close_ok:
            self.data = snapshot(status_code="tunnel_disconnected")
        else:
            self.data = snapshot(
                state="stop_uncertain",
                status_code="tunnel_stop_unconfirmed",
                owned=True,
                ownership_uncertain=True,
            )
        return self.snapshot()

    def close(self) -> bool:
        self.calls.append("close")
        if self.close_ok:
            self.data = snapshot(state="closed", status_code="tunnel_closed")
        else:
            self.data = snapshot(
                state="stop_uncertain",
                status_code="tunnel_stop_unconfirmed",
                owned=True,
                ownership_uncertain=True,
            )
        return self.close_ok

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)


def configured(*, running: bool = False) -> TunnelController:
    model = TunnelController()
    model.path = Path("device.mock.local.toml")
    model.view = TunnelView.parse(
        snapshot(
            state="running" if running else "disconnected",
            owned=running,
            process_alive=running,
            status_code="tunnel_started" if running else "tunnel_configured",
        )
    )
    return model


def test_credentials_hidden_consumed_and_not_retained_by_controller() -> None:
    model = configured()
    operation = model.connect(API_KEY, REMOTE_KEY)
    assert operation is not None
    assert API_KEY not in repr(operation) and REMOTE_KEY not in repr(operation)
    outcome = perform_tunnel(FakeManager(), operation, URL, Path.cwd())
    assert operation.api_key == operation.bearer == ""
    assert API_KEY not in repr(outcome) and REMOTE_KEY not in repr(outcome)
    assert API_KEY not in repr(vars(model)) and REMOTE_KEY not in repr(vars(model))


def test_manual_restart_stops_then_requests_new_credentials_without_replay() -> None:
    model = configured(running=True)
    manager = FakeManager()
    operation = model.restart()
    assert operation is not None and operation.action == "restart_stop"
    assert operation.api_key == operation.bearer == ""
    assert model.restart() is None
    result = perform_tunnel(manager, operation, URL, Path.cwd())
    assert manager.calls == ["disconnect"]
    assert model.accept(result) and model.prompt_after_stop
    assert model.can_connect() and not model.busy
    assert model.poll() is None
    assert model.connect("", REMOTE_KEY) is None


@pytest.mark.parametrize("action", ["disconnect", "restart", "request_close"])
def test_unconfirmed_stop_retains_window_and_disables_connect(action: str) -> None:
    model = configured(running=True)
    manager = FakeManager()
    manager.close_ok = False
    operation = getattr(model, action)()
    assert operation is not None
    assert model.accept(perform_tunnel(manager, operation, URL, Path.cwd()))
    assert not model.closed and model.stop_unconfirmed
    assert not model.can_connect() and not model.can_edit()
    assert not model.prompt_after_stop
    assert model.message == REVIEW_WARNING
    assert model.needs_close_confirmation() and model.can_disconnect()


def test_stop_can_be_retried_without_starting_another_client() -> None:
    model = configured(running=True)
    manager = FakeManager()
    manager.close_ok = False
    first = model.disconnect()
    assert first is not None
    model.accept(perform_tunnel(manager, first, URL, Path.cwd()))
    manager.close_ok = True
    retry = model.disconnect()
    assert retry is not None
    model.accept(perform_tunnel(manager, retry, URL, Path.cwd()))
    assert not model.stop_unconfirmed and model.can_connect()
    assert manager.calls == ["disconnect", "disconnect"]


def test_close_during_connect_waits_for_result_then_closes_owned_client() -> None:
    model = configured()
    manager = FakeManager()
    operation = model.connect(API_KEY, REMOTE_KEY)
    assert operation is not None and model.needs_close_confirmation()
    assert model.request_close() is None
    assert model.closing and not model.closed
    assert model.accept(perform_tunnel(manager, operation, URL, Path.cwd()))
    close = model.pending_close()
    assert close is not None and close.action == "close"
    assert model.pending_close() is None
    assert model.accept(perform_tunnel(manager, close, URL, Path.cwd()))
    assert model.closed
    assert manager.calls == ["connect", "close"]
    assert model.connect(API_KEY, REMOTE_KEY) is None


def test_unexpected_exit_is_visible_and_never_autorestarts() -> None:
    model = configured(running=True)
    manager = FakeManager()
    manager.data = snapshot(status_code="tunnel_client_exited", exit_code=17)
    operation = model.poll()
    assert operation is not None
    assert model.accept(perform_tunnel(manager, operation, URL, Path.cwd()))
    assert model.view.exit_code == 17 and model.failed
    assert not model.view.process_alive and not model.view.owned
    assert model.poll() is None and model.can_connect()
    assert "exited" in model.message
    assert manager.calls == ["poll"]


def test_cloud_auth_failure_reports_fixed_error_without_automatic_retry() -> None:
    model = configured(running=True)
    manager = FakeManager()
    manager.data = snapshot(status_code="tunnel_cloud_auth_failed")
    operation = model.poll()
    assert operation is not None
    assert model.accept(perform_tunnel(manager, operation, URL, Path.cwd()))
    assert "authentication failed" in model.message
    assert model.failed and model.can_connect()
    assert model.poll() is None
    assert manager.calls == ["poll"]


def test_unknown_errors_and_snapshot_text_are_never_exposed() -> None:
    manager = FakeManager()
    manager.failure = RuntimeError(API_KEY + REMOTE_KEY)
    operation = TunnelOperation(1, "connect", api_key=API_KEY, bearer="Bearer " + REMOTE_KEY)
    outcome = perform_tunnel(manager, operation, URL, Path.cwd())
    assert outcome.failed and outcome.message == REQUEST_FAILED
    assert API_KEY not in repr(outcome) and REMOTE_KEY not in repr(outcome)
    assert operation.api_key == operation.bearer == ""
    manager.failure = None
    manager.data = snapshot(status_code=API_KEY, error=REMOTE_KEY, exit_code=REMOTE_KEY)
    outcome = perform_tunnel(manager, TunnelOperation(2, "poll"), URL, Path.cwd())
    assert outcome.message == REQUEST_FAILED and outcome.failed
    assert API_KEY not in repr(outcome) and REMOTE_KEY not in repr(outcome)


def test_failed_connect_without_snapshot_requires_shutdown_review() -> None:
    model = configured()
    operation = model.connect(API_KEY, REMOTE_KEY)
    assert operation is not None
    result = TunnelOutcome(operation.sequence, "connect", None, REQUEST_FAILED, True)
    assert model.accept(result)
    assert model.stop_unconfirmed and not model.can_connect()
    assert model.needs_close_confirmation()


def test_independent_evidence_does_not_claim_application_verification() -> None:
    view = TunnelView.parse(
        snapshot(
            state="running",
            owned=True,
            process_alive=True,
            local_readiness=True,
            cloud_poll_observed=True,
        )
    )
    assert view.local_ready and view.cloud_poll_success
    assert view.application_verified is None
    assert TunnelPanel._evidence(view.application_verified) == "unknown"
    view = TunnelView.parse(snapshot(local_readiness=True))
    assert view.local_ready and view.cloud_poll_success is None


def write_config(path: Path, *, complete: bool = True) -> None:
    text = 'backend = "mock"\nport = 8765\n[tunnel]\nhealth_port = 8766\n'
    if complete:
        text += (
            'tunnel_id = "tunnel_' + "1" * 32 + '"\n'
            'client_exe = "tunnel-client.exe"\nclient_archive = "official.zip"\n'
        )
    path.write_text(text)


def test_saving_secret_fields_is_rejected_without_modifying_file(tmp_path: Path) -> None:
    path = tmp_path / "device.toml"
    write_config(path)
    before = path.read_bytes()
    result = perform_tunnel(
        FakeManager(),
        TunnelOperation(1, "save", path=path, metadata={"api_key": API_KEY}),
        URL,
        tmp_path,
    )
    assert result.failed and result.message == CONFIG_FAILED
    assert path.read_bytes() == before and API_KEY not in repr(result)


def test_runtime_auth_never_falls_back_to_local_operator_token() -> None:
    environment = {"CARDPUTER_LOCAL_TOKEN": "synthetic-local-token"}
    assert runtime_remote_key("", environment) == ""
    environment["CARDPUTER_REMOTE_TOKEN"] = REMOTE_KEY
    assert runtime_remote_key("", environment) == REMOTE_KEY
    assert runtime_remote_key("explicit-issued-key", environment) == "explicit-issued-key"


def test_unknown_config_error_values_are_not_echoed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cardputer_mcp import tunnel

    def fail(*args: Any, **kwargs: Any) -> None:
        raise tunnel.TunnelError("tunnel_config_conflict: " + API_KEY)

    monkeypatch.setattr(tunnel, "load_gui_config", fail)
    outcome = perform_tunnel(
        FakeManager(), TunnelOperation(1, "load", path=tmp_path / "broker.toml"), URL, tmp_path
    )
    assert outcome.failed and outcome.message == CONFIG_FAILED
    assert API_KEY not in repr(outcome)
