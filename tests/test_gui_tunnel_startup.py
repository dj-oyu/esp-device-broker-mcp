"""GUI Tunnel startup: SERIAL follows the TOML, one environment connect, reload without a picker.

No Tk display, Tunnel client, cloud account, or USB device: fake root and manager only.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_gui_tunnel import API_KEY, REMOTE_KEY, URL, FakeManager, write_config
from test_gui_tunnel_resilience import FakeRoot

from cardputer_mcp import config
from cardputer_mcp.gui_tunnel import TunnelController, TunnelOperation, TunnelPanel, perform_tunnel


class RecordingManager(FakeManager):
    def __init__(self) -> None:
        super().__init__()
        self.serial: list[bool] = []

    def configure(self, settings: Any, broker_url: str, *, enable_serial: bool = False) -> Any:
        self.serial.append(enable_serial)
        return super().configure(settings, broker_url, enable_serial=enable_serial)


class Var:
    def __init__(self, value: Any) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


def serial_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    real = config.load_settings

    def load(path: Path | None) -> Any:
        return SimpleNamespace(backend="serial", tunnel=real(path).tunnel)

    monkeypatch.setattr(config, "load_settings", load)


@pytest.mark.parametrize(
    ("backend", "requested", "expected"),
    [("mock", None, False), ("serial", None, True), ("serial", False, False)],
)
def test_load_follows_the_toml_backend_unless_chosen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str, requested: Any, expected: bool
) -> None:
    path = tmp_path / "device.toml"
    write_config(path)
    if backend == "serial":
        serial_backend(monkeypatch)
    manager = RecordingManager()
    operation = TunnelOperation(1, "load", path=path, enable_serial=requested)
    outcome = perform_tunnel(manager, operation, URL, tmp_path)
    assert not outcome.failed
    assert manager.serial == [expected] and outcome.enable_serial is expected
    model = TunnelController()
    model.sequence, model.busy, model.action = 1, True, "load"
    assert model.accept(outcome) and model.enable_serial is expected


def test_reload_uses_the_requested_toml_even_after_a_failed_load(tmp_path: Path) -> None:
    model = TunnelController()
    path = tmp_path / "device.toml"
    first = model.load(path, enable_serial=None)
    assert first is not None
    model.busy = False  # the failed result carried no path
    model.enable_serial = True
    again = model.reload()
    assert again is not None and again.path == path and again.enable_serial is True


def test_reload_without_any_toml_does_nothing() -> None:
    assert TunnelController().reload() is None


def make_panel(path: Path | None, manager: FakeManager) -> TunnelPanel:
    return TunnelPanel(FakeRoot(), URL, broker_config=path, manager=manager)  # type: ignore[arg-type]


def settle(panel: TunnelPanel) -> None:
    """Deliver worker results until the controller is idle."""
    while panel.controller.busy:
        result = panel.worker.results.get(timeout=5)
        panel.worker.results.put(result)
        panel._drain_results()


def test_startup_connects_once_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CARDPUTER_TUNNEL_API_KEY", API_KEY)
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", REMOTE_KEY)
    path = tmp_path / "device.toml"
    write_config(path)
    manager = FakeManager()
    panel = make_panel(path, manager)
    try:
        settle(panel)
        assert manager.calls == ["configure", "connect"]
        assert panel.controller.view.owned and not panel.connect_after_load
    finally:
        panel.worker.stop()


def test_startup_without_both_keys_stays_disconnected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CARDPUTER_TUNNEL_API_KEY", API_KEY)
    monkeypatch.delenv("CARDPUTER_REMOTE_TOKEN", raising=False)
    path = tmp_path / "device.toml"
    write_config(path)
    manager = FakeManager()
    panel = make_panel(path, manager)
    try:
        settle(panel)
        assert manager.calls == ["configure"]
        assert "Not connected at startup" in panel.controller.message
        assert API_KEY not in panel.controller.message
    finally:
        panel.worker.stop()


def test_startup_does_not_connect_after_a_failed_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CARDPUTER_TUNNEL_API_KEY", API_KEY)
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", REMOTE_KEY)
    manager = FakeManager()
    panel = make_panel(tmp_path / "missing.toml", manager)
    try:
        settle(panel)
        assert "connect" not in manager.calls and not panel.connect_after_load
    finally:
        panel.worker.stop()


def test_no_config_means_no_startup_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARDPUTER_TUNNEL_API_KEY", API_KEY)
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", REMOTE_KEY)
    manager = FakeManager()
    panel = make_panel(None, manager)
    try:
        assert not panel.connect_after_load and not panel.controller.busy
    finally:
        panel.worker.stop()


@pytest.mark.parametrize("edited", [False, True])
def test_serial_toggle_reloads_without_a_picker_unless_fields_were_edited(
    tmp_path: Path, edited: bool
) -> None:
    path = tmp_path / "device.toml"
    write_config(path)
    manager = RecordingManager()
    panel = make_panel(path, manager)
    try:
        settle(panel)
        assert manager.serial == [False]
        panel.serial = Var(True)  # type: ignore[assignment]
        panel.fields = {  # type: ignore[assignment]
            key: Var(str(panel.controller.metadata.get(key, 8766 if key == "health_port" else "")))
            for key in ("tunnel_id", "client_exe", "client_archive", "health_port")
        }
        if edited:
            panel.fields["client_exe"].set("other.exe")
        panel._serial_changed()
        settle(panel)
        assert panel.controller.enable_serial is True
        if edited:
            assert manager.serial == [False] and panel.controller.dirty
        else:
            assert manager.serial == [False, True] and not panel.controller.dirty
    finally:
        panel.worker.stop()
