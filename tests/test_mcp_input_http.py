"""Official tools/list and authenticated invalid-input diagnostics; no real device."""

import json

import pytest
from test_profile_jobs import register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.flash_models import FlashDevice
from cardputer_mcp.server import _result
from cardputer_mcp.source_snapshot import SourceError


def flash(**changes):
    return {
        "job_id": "synthetic-flash", "test": "firmware_flash", "session": "a" * 64,
        "device_id": "synthetic-device", "build_job_id": "synthetic-build", **changes,
    }


async def test_tools_list_discloses_flash_request_contract(registry_service):
    service = registry_service
    async with connect(service.url, service.keys["second"].token) as client:
        tools = (await client.list_tools()).tools
    prepare = next(tool for tool in tools if tool.name == "prepare_test")
    text = prepare.description or ""
    for field in ("request.session", "request.device_id", "request.build_job_id", "project_id"):
        assert field in text
    assert "Do not pass project_id" in text
    schema = json.dumps(prepare.input_schema)
    assert "allOf" in schema and "firmware_flash" in schema
    assert "Required successful owned firmware_build" in schema
    assert "Registered source/build project" in schema
    assert prepare.annotations.read_only_hint is False
    assert prepare.annotations.destructive_hint is False
    execute = next(tool for tool in tools if tool.name == "execute_test")
    assert execute.annotations.destructive_hint is True


@pytest.mark.parametrize("input_request,reason,fields", [
    (flash(session=None), "device_session_required", ["session"]),
    (flash(device_id=None), "registered_flash_device_required", ["device_id"]),
    (flash(project_id="SYNTHETIC_PRIVATE"), "project_id_not_allowed_for_flash_operations", ["project_id"]),
    (flash(build_job_id=None), "successful_build_job_required", ["build_job_id"]),
    (flash(job_id="https://SYNTHETIC_SECRET.invalid"), "invalid_field_format", ["job_id"]),
    (flash(test="SYNTHETIC_SECRET"), "invalid_field_value", ["test"]),
    (flash(SYNTHETIC_SECRET_KEY="SYNTHETIC_SECRET_VALUE"), "unexpected_field", ["request"]),
])
async def test_http_tool_validation_is_actionable_and_never_echoes_input(
    registry_service, input_request, reason, fields
):
    service = registry_service
    async with connect(service.url, service.keys["second"].token) as client:
        result = await client.call_tool("prepare_test", {"request": input_request})
    assert result.is_error is True
    assert result.structured_content == dict(
        ok=False, error="invalid_tool_request", reason=reason, fields=fields,
    )
    rendered = json.dumps(result.model_dump(mode="json"))
    assert "SYNTHETIC" not in rendered and "https://" not in rendered and "a" * 64 not in rendered
    assert service.resident.session is None and service.backend.calls == 0
    assert service.resident._store().record_count("jobs") == 0


@pytest.mark.parametrize("configured", [False, True])
async def test_http_flash_registration_and_operation_disabled_have_distinct_codes(
    registry_service, tmp_path, configured
):
    service = registry_service
    broker = service.resident
    if configured:
        registration = FlashDevice(
            selector={"vid": 1, "pid": 2, "serial_number": "SYNTHETIC"},
            flash_bytes=8388608, project_id="fixture",
            python_tool={"executable": tmp_path / "python.exe", "sha256": "b" * 64, "version": "Python 3.13.16"},
            esptool_directory=tmp_path / "tools", esptool_sha256="c" * 64,
            backup_directory=tmp_path / "backups", private_backup_acl_confirmed=True,
            original_elf_sha256="d" * 64, allow_flash=False,
        )
        broker.settings = broker.settings.model_copy(update={"flash_devices": {"disabled": registration}})
        broker.firmware_flash.settings = broker.settings
    async with connect(service.url, service.keys["second"].token) as client:
        lease = await call(client, "acquire_session", {"owner": "remote_mcp"})
        response = await call(client, "prepare_test", flash(
            session=lease["session"], device_id="disabled" if configured else "SYNTHETIC_UNREGISTERED",
        ))
    assert response == dict(
        ok=False, error="registered_flash_operation_not_enabled" if configured else "registered_flash_device_required",
    )
    assert service.backend.calls == 0 and not service.backend.hardware_connected
    assert broker._store().record_count("jobs") == 0


async def test_http_source_receipt_requires_successful_owned_fetch_with_safe_code(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    monkeypatch.setattr(broker.firmware_source.snapshots, "resolve", lambda *args: (
        tmp_path, {"fetch_job_id": "not-a-successful-fetch", "commit": "a" * 40},
    ))
    async with connect(service.url, service.keys["second"].token) as client:
        result = await call(client, "prepare_test", {
            "job_id": "source-bound-build", "test": "firmware_build",
            "project_id": "fixture", "source_receipt": "c" * 64,
        })
    assert result == {"ok": False, "error": "successful_owned_source_fetch_required"}
    assert service.backend.calls == 0 and broker._store().record_count("jobs") == 0


def test_only_typed_allowlisted_source_errors_escape_generic_value_error_mask():
    def source():
        raise SourceError("https://SYNTHETIC_SECRET.invalid")

    def other():
        raise ValueError("SYNTHETIC_SECRET")

    assert _result(source) == {"ok": False, "error": "source_fetch_failed"}
    assert _result(other) == {"ok": False, "error": "invalid_request"}
