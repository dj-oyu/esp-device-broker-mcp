"""Shared, non-secret TOML configuration; synthetic files only, never a client."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from cardputer_mcp import tunnel
from cardputer_mcp.config import load_settings

ID = "tunnel_" + "1" * 32
OTHER_ID = "tunnel_" + "2" * 32
SECRET = "SYNTHETIC_NOT_A_REAL_CREDENTIAL"


def metadata(**updates):
    return {
        "tunnel_id": ID,
        "client_exe": "tools/tunnel-client.exe",
        "client_archive": "downloads/client.zip",
        "health_port": 8766,
        **updates,
    }


def broker_file(project, values=None, name="broker.toml", **broker):
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"backend": "mock", "port": 18765, "state_dir": "state", **broker}
    if values is not None:
        data["tunnel"] = values
    path.write_text(tunnel._toml_document(data), encoding="utf-8")
    return path


def legacy_file(project, values=None, name="tunnel.local.toml", **updates):
    path = project / name
    path.write_text(
        tunnel._toml_document(
            {
                **(metadata() if values is None else values),
                "broker_config": "broker.toml",
                **updates,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_shared_canonical_file_loads_broker_and_tunnel_without_starting_anything(
    tmp_path, monkeypatch
):
    path = broker_file(tmp_path, metadata())
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda *a, **kw: pytest.fail("started client"))
    broker = load_settings(path)
    config = tunnel.load_gui_config(path, tmp_path)
    assert broker.port == 18765
    assert config == tunnel.load_config(path, tmp_path)
    assert config.broker_config == path
    assert config.client_exe == tmp_path / "tools/tunnel-client.exe"
    assert config.client_archive == tmp_path / "downloads/client.zip"
    assert config.tunnel_id == ID


@pytest.mark.parametrize("values", [None, {"tunnel_id": ""}])
def test_absent_or_incomplete_metadata_does_not_block_mock_startup(tmp_path, values):
    path = broker_file(tmp_path, values)
    assert load_settings(path).backend == "mock"
    assert tunnel.load_gui_config(path, tmp_path) is None
    with pytest.raises(tunnel.TunnelError, match="^tunnel_metadata_incomplete$"):
        tunnel.load_config(path, tmp_path)


@pytest.mark.parametrize(
    "field",
    ["api_key", "bearer", "auto_connect", "broker_config"],
)
def test_canonical_metadata_rejects_extra_fields_without_echo(tmp_path, field):
    path = broker_file(tmp_path, metadata(**{field: SECRET}))
    with pytest.raises(ValidationError):
        load_settings(path)
    with pytest.raises(tunnel.TunnelError) as error:
        tunnel.load_config(path, tmp_path)
    assert str(error.value) == "invalid_private_tunnel_config"
    assert SECRET not in str(error.value)
    unknown = broker_file(tmp_path, metadata(), name="unknown.toml", api_key=SECRET)
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.load_gui_config(unknown, tmp_path)


@pytest.mark.parametrize("port", [1023, 65536, True])
def test_metadata_health_port_is_strict_bounded_integer(tmp_path, port):
    path = broker_file(tmp_path, metadata(health_port=port))
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.load_gui_config(path, tmp_path)


@pytest.mark.parametrize("bad", [{"tunnel_id": SECRET}, {"client_exe": ""}])
def test_bad_ids_are_sanitized(tmp_path, bad):
    path = broker_file(tmp_path, metadata(**bad))
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.load_gui_config(path, tmp_path)


@pytest.mark.parametrize("changed", [{"tunnel_id": OTHER_ID}, {"ingress_enabled": True}])
def test_conflicting_legacy_metadata_fails_closed_in_both_entry_points(tmp_path, changed):
    broker = broker_file(tmp_path, metadata())
    legacy = legacy_file(tmp_path, metadata(**changed))
    for path in (broker, legacy):
        with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
            tunnel.load_config(path, tmp_path)
    with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
        tunnel.load_gui_config(broker, tmp_path)


@pytest.mark.parametrize("values", [metadata(), {"tunnel_id": ID, "client_exe": None}])
def test_save_changes_only_tunnel_semantics_and_can_round_trip_serial_tables(tmp_path, values):
    path = broker_file(
        tmp_path,
        backend="serial",
        allow_mock_execution=False,
        max_waiters=19,
        serial={
            "selector": {"vid": 123, "pid": 456, "serial_number": "SYNTHETIC\\identity"},
            "observe_seconds": 1.5,
        },
    )
    before = tomllib.loads(path.read_text())
    tunnel.save_gui_config(path, tmp_path, values)
    after = tomllib.loads(path.read_text())
    assert after.pop("tunnel") == {k: v for k, v in values.items() if v is not None}
    assert after == before
    assert load_settings(path).backend == "serial"
    assert list(tmp_path.glob(".tunnel-config-*")) == []


@pytest.mark.parametrize("field", ["api_key", "broker_config"])
def test_save_rejects_secret_or_unrecognized_metadata_atomically(tmp_path, field):
    path = broker_file(tmp_path)
    before = path.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata(**{field: SECRET}))
    assert path.read_bytes() == before
    unknown = broker_file(tmp_path, name="unknown.toml", unknown=SECRET)
    before = unknown.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(unknown, tmp_path, metadata())
    assert unknown.read_bytes() == before


@pytest.mark.parametrize("directory", [False])
def test_save_refuses_symlink_file_or_parent(tmp_path, directory):
    path = broker_file(tmp_path, name="real/broker.toml")
    if directory:
        alias = tmp_path / "alias"
        alias.symlink_to(path.parent, target_is_directory=True)
        target = alias / path.name
    else:
        target = tmp_path / "link.toml"
        target.symlink_to(path)
    before = path.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^unsafe_tunnel_config_path$"):
        tunnel.save_gui_config(target, tmp_path, metadata())
    assert path.read_bytes() == before


def test_save_replace_failure_is_sanitized_and_original_survives(tmp_path, monkeypatch):
    path = broker_file(tmp_path)
    before = path.read_bytes()

    def fail(*args):
        raise OSError(SECRET)

    monkeypatch.setattr(tunnel.os, "replace", fail)
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata())
    assert path.read_bytes() == before
    assert list(tmp_path.glob(".tunnel-config-*")) == []


def test_save_preserves_unicode_non_bmp_and_control_characters(tmp_path):
    state = "state/日本語/😀/\x7f"
    identity = "SYNTHETIC-🌍-\b-\f-\t-\r-\n-\x00"
    values = metadata(
        client_exe="工具/😀/tunnel-client.exe",
        client_archive="下载/🌍/client.zip",
    )
    path = broker_file(
        tmp_path,
        backend="serial",
        state_dir=state,
        serial={"selector": {"vid": 123, "pid": 456, "serial_number": identity}},
    )
    before = tomllib.loads(path.read_text())
    tunnel.save_gui_config(path, tmp_path, values)
    after = tomllib.loads(path.read_text())
    assert after.pop("tunnel") == values
    assert after == before
    assert load_settings(path).state_dir == Path(state)
    assert load_settings(path).serial.selector.serial_number == identity
    assert "\\ud83d" not in path.read_text()
    original = path.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata(client_archive="bad\ud83d.zip"))
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".tunnel-config-*")) == []
