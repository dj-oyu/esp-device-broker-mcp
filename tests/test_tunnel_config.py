"""Shared, non-secret TOML configuration; synthetic files only, never a client."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from cardputer_mcp import tunnel
from cardputer_mcp.config import Settings, load_settings

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


@pytest.mark.parametrize("values", [None, {}, {"tunnel_id": ""}, {"health_port": 8766}])
def test_absent_or_incomplete_metadata_does_not_block_mock_startup(tmp_path, values):
    path = broker_file(tmp_path, values)
    assert load_settings(path).backend == "mock"
    assert tunnel.load_gui_config(path, tmp_path) is None
    with pytest.raises(tunnel.TunnelError, match="^tunnel_metadata_incomplete$"):
        tunnel.load_config(path, tmp_path)


def test_placeholder_is_loadable_metadata_and_not_a_connection_request(tmp_path):
    path = broker_file(tmp_path, metadata(tunnel_id="tunnel_" + "0" * 32))
    assert load_settings(path).backend == "mock"
    assert tunnel.load_config(path, tmp_path).tunnel_id == "tunnel_" + "0" * 32


@pytest.mark.parametrize(
    "field",
    [
        "api_key",
        "bearer",
        "mcp_key",
        "client_key",
        "token",
        "autoconnect",
        "auto_connect",
        "enabled",
        "command",
        "server_url",
        "broker_config",
    ],
)
def test_canonical_metadata_rejects_extra_fields_without_echo(tmp_path, field):
    path = broker_file(tmp_path, metadata(**{field: SECRET}))
    with pytest.raises(ValidationError):
        load_settings(path)
    with pytest.raises(tunnel.TunnelError) as error:
        tunnel.load_config(path, tmp_path)
    assert str(error.value) == "invalid_private_tunnel_config"
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("port", [0, 1023, 65536, True, "8766", 8766.5])
def test_metadata_health_port_is_strict_bounded_integer(tmp_path, port):
    path = broker_file(tmp_path, metadata(health_port=port))
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.load_gui_config(path, tmp_path)


@pytest.mark.parametrize("port", [float("inf"), float("-inf"), float("nan")])
def test_metadata_health_port_rejects_nonfinite_values(port):
    with pytest.raises(ValidationError):
        Settings(tunnel={"health_port": port})


@pytest.mark.parametrize("identifier", ["tunnel_123", "tunnel_" + "A" * 32, SECRET])
def test_bad_ids_are_sanitized(tmp_path, identifier):
    path = broker_file(tmp_path, metadata(tunnel_id=identifier))
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.load_gui_config(path, tmp_path)


@pytest.mark.parametrize("field", ["client_exe", "client_archive"])
def test_canonical_metadata_rejects_blank_paths(tmp_path, field):
    path = broker_file(tmp_path, metadata(**{field: ""}))
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.load_gui_config(path, tmp_path)


def test_legacy_fills_omitted_canonical_metadata_for_same_broker(tmp_path):
    broker = broker_file(tmp_path, {"tunnel_id": ID})
    legacy = legacy_file(tmp_path)
    config = tunnel.load_gui_config(broker, tmp_path)
    assert config == tunnel.load_config(legacy, tmp_path)
    assert config.broker_config == broker
    assert config.health_port == 8766
    assert load_settings(broker).tunnel.client_exe is None  # No automatic migration.


def test_relative_paths_resolve_against_project_not_config_parent_or_shell(tmp_path, monkeypatch):
    broker = broker_file(tmp_path, metadata(), name="nested/broker.toml")
    legacy_file(tmp_path, broker_config="nested/../nested/broker.toml")
    monkeypatch.chdir(tmp_path.parent)
    config = tunnel.load_gui_config(Path("nested/broker.toml"), tmp_path)
    assert config.broker_config == broker
    assert config.client_exe == tmp_path / "tools/tunnel-client.exe"


@pytest.mark.parametrize(
    "changed",
    [
        {"tunnel_id": OTHER_ID},
        {"client_exe": "other/tunnel-client.exe"},
        {"client_archive": "other.zip"},
        {"health_port": 8767},
    ],
)
def test_conflicting_legacy_metadata_fails_closed_in_both_entry_points(tmp_path, changed):
    broker = broker_file(tmp_path, metadata())
    legacy = legacy_file(tmp_path, metadata(**changed))
    for path in (broker, legacy):
        with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
            tunnel.load_config(path, tmp_path)
    with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
        tunnel.load_gui_config(broker, tmp_path)


def test_zero_placeholder_does_not_silently_override_legacy_id(tmp_path):
    path = broker_file(tmp_path, metadata(tunnel_id="tunnel_" + "0" * 32))
    legacy_file(tmp_path)
    with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
        tunnel.load_gui_config(path, tmp_path)


def test_unrelated_legacy_profile_is_never_used_or_rewritten(tmp_path):
    path = broker_file(tmp_path, metadata())
    legacy = legacy_file(tmp_path, metadata(tunnel_id=OTHER_ID), broker_config="other.toml")
    before = legacy.read_bytes()
    assert tunnel.load_gui_config(path, tmp_path).tunnel_id == ID
    assert legacy.read_bytes() == before


def test_explicit_legacy_path_uses_same_broker_identity_check(tmp_path):
    path = broker_file(tmp_path)
    legacy = legacy_file(tmp_path, name="old-settings.toml")
    assert tunnel.load_gui_config(path, tmp_path) is None
    assert tunnel.load_gui_config(path, tmp_path, legacy).tunnel_id == ID


def test_canonical_config_never_accepts_flat_metadata_or_unknown_broker_fields(tmp_path):
    path = broker_file(tmp_path, metadata(), api_key=SECRET)
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.load_gui_config(path, tmp_path)


def test_save_changes_only_tunnel_semantics_and_can_round_trip_serial_tables(tmp_path):
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
    tunnel.save_gui_config(path, tmp_path, metadata())
    after = tomllib.loads(path.read_text())
    assert after.pop("tunnel") == metadata()
    assert after == before
    assert load_settings(path).backend == "serial"
    assert tunnel.load_gui_config(path, tmp_path).tunnel_id == ID
    assert list(tmp_path.glob(".tunnel-config-*")) == []


def test_save_runtime_settings_checks_broker_identity(tmp_path):
    path = broker_file(tmp_path, metadata())
    settings = tunnel.load_config(path, tmp_path)
    tunnel.save_gui_config(path, tmp_path, settings)
    before = path.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
        tunnel.save_gui_config(
            path, tmp_path, settings.model_copy(update={"broker_config": tmp_path / "other.toml"})
        )
    assert path.read_bytes() == before


def test_save_compatible_legacy_metadata_only_writes_canonical_file(tmp_path):
    path = broker_file(tmp_path)
    legacy = legacy_file(tmp_path)
    before = legacy.read_bytes()
    tunnel.save_gui_config(path, tmp_path, metadata())
    assert legacy.read_bytes() == before
    assert load_settings(path).tunnel.tunnel_id == ID
    assert tunnel.load_gui_config(path, tmp_path).tunnel_id == ID


def test_conflicting_save_leaves_all_files_untouched(tmp_path):
    path = broker_file(tmp_path, metadata())
    legacy = legacy_file(tmp_path)
    before = (path.read_bytes(), legacy.read_bytes())
    with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
        tunnel.save_gui_config(path, tmp_path, metadata(tunnel_id=OTHER_ID))
    assert (path.read_bytes(), legacy.read_bytes()) == before
    assert list(tmp_path.glob(".tunnel-config-*")) == []


@pytest.mark.parametrize("field", ["api_key", "mcp_key", "bearer", "auto_connect", "broker_config"])
def test_save_rejects_secret_or_unrecognized_metadata_atomically(tmp_path, field):
    path = broker_file(tmp_path)
    before = path.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata(**{field: SECRET}))
    assert path.read_bytes() == before


def test_save_never_discards_unknown_existing_fields(tmp_path):
    path = broker_file(tmp_path, unknown=SECRET)
    before = path.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata())
    assert path.read_bytes() == before


@pytest.mark.parametrize("directory", [False, True])
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


def test_save_refuses_concurrent_change_and_preserves_new_content(tmp_path, monkeypatch):
    path = broker_file(tmp_path)
    replacement = b'backend = "mock"\nport = 18767\n'
    original_guard = tunnel._reject_symlink_path
    count = 0

    def change(target):
        nonlocal count
        count += 1
        if count == 2:
            path.write_bytes(replacement)
        original_guard(target)

    monkeypatch.setattr(tunnel, "_reject_symlink_path", change)
    with pytest.raises(tunnel.TunnelError, match="^tunnel_config_changed$"):
        tunnel.save_gui_config(path, tmp_path, metadata())
    assert path.read_bytes() == replacement
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


def test_save_rejects_lone_surrogate_without_modifying_existing_file(tmp_path):
    path = broker_file(tmp_path)
    original = path.read_bytes()
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata(client_archive="bad\ud83d.zip"))
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".tunnel-config-*")) == []


def test_save_revalidates_serialized_toml_before_any_replace(tmp_path, monkeypatch):
    path = broker_file(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(tunnel, "_toml_document", lambda values: 'state_dir = "\\ud83d"\n')
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata())
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".tunnel-config-*")) == []


def test_save_revalidates_serialized_semantics_before_any_replace(tmp_path, monkeypatch):
    path = broker_file(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(tunnel, "_toml_document", lambda values: 'backend = "mock"\nport = 9999\n')
    with pytest.raises(tunnel.TunnelError, match="^invalid_private_tunnel_config$"):
        tunnel.save_gui_config(path, tmp_path, metadata())
    assert path.read_bytes() == original
    assert list(tmp_path.glob(".tunnel-config-*")) == []
