"""Removed config flags: legacy keys never widen anything; exposure is one `mode`.

Legacy `true` for a removed gate is read and ignored. Legacy `false` used to mean
"closed", so it is a fixed startup error instead of silently becoming "open".
"""

import copy
from pathlib import Path

import pytest
from pydantic import ValidationError

from cardputer_mcp.config import (
    GatewaySettings,
    Settings,
    SourceRepository,
    TunnelMetadata,
    WebUISettings,
    load_settings,
)
from cardputer_mcp.exposure import resolve_mode
from cardputer_mcp.firmware_flash import flash_readiness
from cardputer_mcp.flash_models import FlashDevice, UnattendedFlash

ROOT = Path.cwd().anchor or "/"
PRIVATE = "SYNTHETIC_PRIVATE_SEGMENT"


def absolute(*parts: str) -> str:
    return str(Path(ROOT, PRIVATE, *parts))


def device(**extra):
    return {
        "selector": {"vid": 1, "pid": 2, "serial_number": "synthetic"},
        "flash_bytes": 8388608,
        "project_id": "project",
        "python_tool": {
            "executable": absolute("python.exe"),
            "sha256": "a" * 64,
            "version": "Python 3.13.1",
        },
        "esptool_directory": absolute("tools"),
        "esptool_sha256": "b" * 64,
        "backup_directory": absolute("backups"),
        **extra,
    }


UNATTENDED = {
    "principal_id": "alice",
    "device_id": "device",
    "project_id": "project",
    "operation": "firmware_flash",
    "source_receipt": "c" * 64,
    "source_repository": "owner/repo",
    "source_commit": "d" * 40,
}


def webui(**extra):
    return {
        "public_url": "https://host.example.ts.net:8443",
        "google_client_id": "123-abc.apps.googleusercontent.com",
        "allowed_google_sub": "42",
        "broker_env_file": absolute("broker.env"),
        "google_client_secret_file": absolute("google.env"),
        **extra,
    }


def gateway(**extra):
    values = webui(**extra)
    values["public_url"] = "https://host.example.ts.net"
    return values


# --- removed hardware-write gates -----------------------------------------------------


@pytest.mark.parametrize("key", ["allow_flash", "allow_backup", "allow_restore"])
def test_legacy_true_write_gates_are_accepted_and_ignored(key):
    registration = FlashDevice.model_validate(device(**{key: True}))
    assert not hasattr(registration, key)
    assert key not in registration.model_dump()


def test_registered_device_offers_every_operation_without_any_gate():
    registration = FlashDevice.model_validate(device())
    readiness = flash_readiness(registration, has_lkg=False)
    assert readiness["operations"] == {"backup": True, "flash": True, "restore": True}
    assert readiness["flash_baseline"] == "not_required"


@pytest.mark.parametrize(
    "key", ["allow_flash", "allow_backup", "allow_restore", "private_backup_acl_confirmed"]
)
def test_legacy_false_write_gates_are_a_fixed_startup_error(key):
    with pytest.raises(ValidationError) as caught:
        Settings.model_validate({"flash_devices": {"d": device(**{key: False})}})
    text = str(caught.value)
    assert f"removed_setting_{key}_false" in text
    assert f"delete the `{key}` line" in text
    # Neither the private paths nor any input value is echoed back.
    assert PRIVATE not in text and "synthetic" not in text


@pytest.mark.parametrize("value", ["true", 1, "false", 0, None])
def test_legacy_gates_must_be_real_booleans(value):
    with pytest.raises(ValidationError) as caught:
        FlashDevice.model_validate(device(allow_backup=value))
    assert "removed_setting_allow_backup_must_be_boolean" in str(caught.value)


def test_owner_profile_shape_fails_only_on_the_false_gates():
    legacy = device(
        allow_flash=True, allow_backup=False, allow_restore=False, private_backup_acl_confirmed=True
    )
    with pytest.raises(ValidationError) as caught:
        FlashDevice.model_validate(legacy)
    assert "allow_backup" in str(caught.value)
    del legacy["allow_backup"], legacy["allow_restore"]
    FlashDevice.model_validate(legacy)  # the remaining legacy trues start fine


# --- unattended_flash: the table's presence is what enables it ---------------------------


def test_unattended_table_presence_enables_and_enabled_true_is_ignored():
    plain = FlashDevice.model_validate(device(unattended_flash=UNATTENDED))
    legacy = FlashDevice.model_validate(device(unattended_flash={"enabled": True, **UNATTENDED}))
    assert plain.unattended_flash is not None
    assert plain == legacy
    assert FlashDevice.model_validate(device()).unattended_flash is None
    assert "enabled" not in plain.model_dump()["unattended_flash"]


def test_unattended_enabled_false_is_refused_with_the_delete_table_advice():
    with pytest.raises(ValidationError) as caught:
        UnattendedFlash.model_validate({"enabled": False, **UNATTENDED})
    text = str(caught.value)
    assert "removed_setting_enabled_false" in text
    assert "delete the whole unattended_flash table" in text
    assert "alice" not in text


# --- source repository attestation ---------------------------------------------------------


def source(**extra):
    return {"github_repository": "owner/repo", "cache_root": absolute("cache"), **extra}


def test_private_acl_confirmed_true_is_ignored_and_false_is_refused():
    registration = SourceRepository.model_validate(source(private_acl_confirmed=True))
    assert registration == SourceRepository.model_validate(source())
    with pytest.raises(ValidationError) as caught:
        SourceRepository.model_validate(source(private_acl_confirmed=False))
    assert "removed_setting_private_acl_confirmed_false" in str(caught.value)
    assert PRIVATE not in str(caught.value)


# --- allow_mock_execution ------------------------------------------------------------------


@pytest.mark.parametrize("value", [True, False])
def test_allow_mock_execution_either_value_is_read_and_ignored(value):
    settings = Settings.model_validate({"allow_mock_execution": value})
    assert settings == Settings()
    assert "allow_mock_execution" not in settings.model_dump()


def test_allow_mock_execution_must_still_be_a_boolean():
    with pytest.raises(ValidationError):
        Settings.model_validate({"allow_mock_execution": "yes"})


# --- exposure mode -------------------------------------------------------------------------


def test_every_exposure_is_closed_by_default():
    assert WebUISettings.model_validate(webui()).mode == "off"
    assert GatewaySettings.model_validate(gateway()).mode == "off"
    assert TunnelMetadata().mode == "off"
    ui = WebUISettings.model_validate(webui())
    assert (ui.serve, ui.funnel) == (False, False)
    assert GatewaySettings.model_validate(gateway()).funnel is False
    assert TunnelMetadata().ingress_enabled is False
    assert Settings().webui is None and Settings().gateway is None and Settings().tunnel is None


@pytest.mark.parametrize(
    ("legacy", "mode"),
    [
        ({}, "off"),
        ({"serve": False, "funnel": False}, "off"),
        ({"serve": False}, "off"),
        ({"funnel": False}, "off"),
        ({"serve": True}, "tailnet"),
        ({"serve": True, "funnel": False}, "tailnet"),
        ({"funnel": True}, "public"),
        ({"serve": False, "funnel": True}, "public"),
    ],
)
def test_webui_legacy_booleans_translate_to_mode(legacy, mode):
    settings = WebUISettings.model_validate(webui(**legacy))
    assert settings.mode == mode
    assert settings.serve is (mode == "tailnet") and settings.funnel is (mode == "public")
    assert "serve" not in settings.model_dump() and "funnel" not in settings.model_dump()


@pytest.mark.parametrize("mode", ["off", "tailnet", "public"])
def test_webui_mode_loads_directly(mode):
    assert WebUISettings.model_validate(webui(mode=mode)).mode == mode


@pytest.mark.parametrize(
    "extra",
    [
        {"serve": True, "funnel": True},
        {"mode": "public", "serve": True},
        {"mode": "tailnet", "funnel": True},
        {"mode": "public", "funnel": False},
        {"mode": "tailnet", "serve": False},
        {"mode": "off", "serve": True},
        {"mode": "off", "funnel": True},
        {"mode": "tailnet", "serve": False, "funnel": False},
        {"mode": "funnel"},
        {"mode": True},
        {"serve": "true"},
        {"funnel": 1},
    ],
)
def test_webui_contradictions_and_bad_values_are_errors(extra):
    with pytest.raises(ValidationError) as caught:
        WebUISettings.model_validate(webui(**extra))
    assert PRIVATE not in str(caught.value)


def test_webui_consistent_legacy_and_mode_are_accepted():
    assert WebUISettings.model_validate(webui(mode="tailnet", serve=True)).mode == "tailnet"
    assert WebUISettings.model_validate(webui(mode="public", funnel=True)).mode == "public"
    assert WebUISettings.model_validate(webui(mode="off", serve=False)).mode == "off"


def test_webui_public_needs_a_funnel_port():
    with pytest.raises(ValidationError):
        WebUISettings.model_validate(
            webui(mode="public", public_url="https://host.example.ts.net:9443")
        )
    with pytest.raises(ValidationError):
        WebUISettings.model_validate(
            webui(funnel=True, public_url="https://host.example.ts.net:9443")
        )
    WebUISettings.model_validate(webui(mode="tailnet", public_url="https://h.example.ts.net:9443"))


@pytest.mark.parametrize(
    ("extra", "mode"),
    [
        ({}, "off"),
        ({"funnel": False}, "off"),
        ({"funnel": True}, "public"),
        ({"mode": "public"}, "public"),
    ],
)
def test_gateway_mode(extra, mode):
    settings = GatewaySettings.model_validate(gateway(**extra))
    assert settings.mode == mode and settings.funnel is (mode == "public")


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "tailnet"},
        {"mode": "public", "funnel": False},
        {"mode": "off", "funnel": True},
        {"funnel": "yes"},
    ],
)
def test_gateway_contradictions_and_bad_values_are_errors(extra):
    with pytest.raises(ValidationError):
        GatewaySettings.model_validate(gateway(**extra))


@pytest.mark.parametrize(
    ("extra", "mode"),
    [
        ({}, "off"),
        ({"ingress_enabled": False}, "off"),
        ({"ingress_enabled": True}, "ingress"),
        ({"mode": "ingress"}, "ingress"),
        ({"mode": "off"}, "off"),
        ({"mode": "ingress", "ingress_enabled": True}, "ingress"),
    ],
)
def test_tunnel_ingress_mode(extra, mode):
    settings = TunnelMetadata.model_validate(extra)
    assert settings.mode == mode and settings.ingress_enabled is (mode == "ingress")


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "ingress", "ingress_enabled": False},
        {"mode": "off", "ingress_enabled": True},
        {"mode": "public"},
        {"ingress_enabled": 1},
        {"ingress_enabled": "true"},
    ],
)
def test_tunnel_contradictions_and_bad_values_are_errors(extra):
    with pytest.raises(ValidationError):
        TunnelMetadata.model_validate(extra)


def test_resolve_mode_never_opens_what_the_legacy_keys_left_closed():
    flags = {"serve": "tailnet", "funnel": "public"}
    for serve in (False, True):
        for funnel in (False, True):
            if serve and funnel:
                with pytest.raises(ValueError):
                    resolve_mode({"serve": serve, "funnel": funnel}, "webui", flags)
                continue
            expected = "tailnet" if serve else "public" if funnel else "off"
            assert resolve_mode({"serve": serve, "funnel": funnel}, "webui", flags) == {
                "mode": expected
            }
    untouched = {"mode": "public"}
    assert resolve_mode(untouched, "webui", flags) is untouched
    assert resolve_mode("not-a-mapping", "webui", flags) == "not-a-mapping"


# --- whole files ---------------------------------------------------------------------------


def toml_text(extra_device: str = "", extra_source: str = "", exposure: str = "") -> str:
    def q(value: str) -> str:
        return '"' + value.replace("\\", "/") + '"'

    return (
        'backend = "mock"\n'
        "allow_mock_execution = false\n"
        "[tunnel]\n"
        f"{exposure}"
        "[build_projects.project]\n"
        f"project_dir = {q(absolute('project'))}\n"
        f"trusted_profile = {q(absolute('profile.ps1'))}\n"
        "[build_projects.project.source_repository]\n"
        'github_repository = "owner/repo"\n'
        f"cache_root = {q(absolute('cache'))}\n"
        f"{extra_source}"
        "[flash_devices.d]\n"
        "flash_bytes = 8388608\n"
        'project_id = "project"\n'
        f"backup_directory = {q(absolute('backups'))}\n"
        f"esptool_directory = {q(absolute('tools'))}\n"
        f'esptool_sha256 = "{"b" * 64}"\n'
        f"{extra_device}"
        "[flash_devices.d.selector]\n"
        'vid = 1\npid = 2\nserial_number = "synthetic"\n'
        "[flash_devices.d.python_tool]\n"
        f"executable = {q(absolute('python.exe'))}\n"
        f'sha256 = "{"a" * 64}"\n'
        'version = "Python 3.13.1"\n'
    )


def test_legacy_file_loads_and_owner_false_gates_are_the_only_errors(tmp_path):
    path = tmp_path / "device.toml"
    path.write_text(
        toml_text(
            extra_device=(
                "allow_flash = true\nallow_backup = false\nallow_restore = false\n"
                "private_backup_acl_confirmed = true\n"
            ),
            extra_source="private_acl_confirmed = true\n",
            exposure="ingress_enabled = true\n",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError) as caught:
        load_settings(path)
    text = str(caught.value)
    assert "removed_setting_allow_backup_false" in text
    assert "removed_setting_allow_restore_false" in text
    assert "allow_flash" not in text  # the accepted legacy true is not mentioned
    assert PRIVATE not in text
    # What the instructions say to do: delete the two lines; everything else starts.
    path.write_text(
        toml_text(
            extra_device="allow_flash = true\nprivate_backup_acl_confirmed = true\n",
            extra_source="private_acl_confirmed = true\n",
            exposure="ingress_enabled = true\n",
        ),
        encoding="utf-8",
    )
    settings = load_settings(path)
    assert settings.tunnel is not None and settings.tunnel.mode == "ingress"
    assert set(settings.flash_devices) == {"d"}


def test_new_style_file_loads_with_no_removed_key(tmp_path):
    path = tmp_path / "device.toml"
    path.write_text(toml_text(exposure='mode = "off"\n'), encoding="utf-8")
    settings = load_settings(path)
    assert settings.tunnel is not None and settings.tunnel.mode == "off"
    registration = settings.flash_devices["d"]
    assert flash_readiness(registration, has_lkg=False)["operations"] == {
        "backup": True,
        "flash": True,
        "restore": True,
    }


def test_stale_legacy_settings_object_is_not_mutated_by_translation():
    raw = {"serve": True, "public_url": "x"}
    snapshot = copy.deepcopy(raw)
    resolve_mode(raw, "webui", {"serve": "tailnet"})
    assert raw == snapshot
