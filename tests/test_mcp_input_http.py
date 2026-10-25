"""Official tools/list and authenticated invalid-input diagnostics; no real device."""

import json

import pytest
from test_profile_jobs import register
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import call, connect
from cardputer_mcp.flash_models import FlashDevice
from cardputer_mcp.source_snapshot import SourceError
from cardputer_mcp.surface import _result


def flash(**changes):
    return {
        "job_id": "synthetic-flash",
        "kind": "flash",
        "session": "a" * 64,
        "device_id": "synthetic-device",
        "build_job_id": "synthetic-build",
        **changes,
    }


async def test_tools_list_discloses_flash_request_contract(registry_service):
    service = registry_service
    async with connect(service.url, service.keys["second"].token) as client:
        tools = (await client.list_tools()).tools
    assert sorted(tool.name for tool in tools) == [
        "cancel",
        "execute",
        "flash",
        "session",
        "status",
        "submit",
    ]
    by_name = {tool.name: tool for tool in tools}
    submit = by_name["submit"]
    schema = json.dumps(submit.input_schema)
    for field in ("session", "device_id", "build_job_id", "project_id", "kind"):
        assert field in schema
    assert "flash|restore" in schema and "fetch|build" in schema
    assert "Live lease for device kinds" in schema
    assert "flash" in json.dumps(submit.input_schema["$defs"]["Kind"])
    assert "Nothing is queued" in (submit.description or "")
    assert "execute_tool" in (submit.description or "")
    assert submit.annotations.read_only_hint is False
    assert submit.annotations.destructive_hint is False
    assert by_name["status"].annotations.read_only_hint is True
    for name in ("execute", "cancel", "session"):
        assert by_name[name].annotations.read_only_hint is False
        assert by_name[name].annotations.destructive_hint is False
    assert by_name["flash"].annotations.read_only_hint is False
    assert by_name["flash"].annotations.destructive_hint is True
    assert [tool.name for tool in tools if tool.annotations.destructive_hint] == ["flash"]


@pytest.mark.parametrize(
    "input_request,reason,fields",
    [
        (flash(job_id="https://SYNTHETIC_SECRET.invalid"), "invalid_field_format", ["job_id"]),
        (flash(kind="SYNTHETIC_SECRET"), "invalid_field_value", ["kind"]),
        (flash(SYNTHETIC_SECRET_KEY="SYNTHETIC_SECRET_VALUE"), "unexpected_field", ["request"]),
    ],
)
async def test_http_tool_validation_is_actionable_and_never_echoes_input(
    registry_service, input_request, reason, fields
):
    service = registry_service
    async with connect(service.url, service.keys["second"].token) as client:
        result = await client.call_tool("submit", {"request": input_request})
    assert result.is_error is True
    assert result.structured_content == dict(
        ok=False,
        error="invalid_tool_request",
        reason=reason,
        fields=fields,
    )
    rendered = json.dumps(result.model_dump(mode="json"))
    assert "SYNTHETIC" not in rendered and "https://" not in rendered and "a" * 64 not in rendered
    assert service.resident.session is None and service.backend.calls == 0
    assert service.resident._store().record_count("jobs") == 0


@pytest.mark.parametrize(
    "input_request,error,field",
    [
        (flash(session=None), "device_session_required", None),
        (flash(project_id="SYNTHETIC_PRIVATE"), "argument_not_allowed", "project_id"),
        (flash(backup_job_id="a-backup", lkg_receipt="b" * 64), "argument_conflict", "lkg_receipt"),
    ],
)
async def test_http_submit_rule_failures_return_fixed_codes_and_never_echo_input(
    registry_service, input_request, error, field
):
    service = registry_service
    async with connect(service.url, service.keys["second"].token) as client:
        result = await client.call_tool("submit", {"request": input_request})
    assert result.is_error is False
    expected = {"ok": False, "error": error}
    if field is not None:
        expected["field"] = field
    assert result.structured_content == expected
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
            flash_bytes=8388608,
            project_id="fixture",
            python_tool={
                "executable": tmp_path / "python.exe",
                "sha256": "b" * 64,
                "version": "Python 3.13.16",
            },
            esptool_directory=tmp_path / "tools",
            esptool_sha256="c" * 64,
            backup_directory=tmp_path / "backups",
            private_backup_acl_confirmed=True,
            original_elf_sha256="d" * 64,
            allow_flash=False,
        )
        broker.settings = broker.settings.model_copy(
            update={"flash_devices": {"disabled": registration}}
        )
        broker.firmware_flash.settings = broker.settings
    async with connect(service.url, service.keys["second"].token) as client:
        lease = await call(
            client, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )
        response = await call(
            client,
            "submit",
            flash(
                session=lease["session"],
                device_id="disabled" if configured else "SYNTHETIC_UNREGISTERED",
            ),
        )
    assert response == dict(
        ok=False,
        error="registered_flash_operation_not_enabled"
        if configured
        else "registered_flash_device_required",
    )
    assert service.backend.calls == 0 and not service.backend.hardware_connected
    assert broker._store().record_count("jobs") == 0


async def test_http_source_receipt_requires_successful_owned_fetch_with_safe_code(
    registry_service, tmp_path, monkeypatch
):
    service = registry_service
    register(service, tmp_path)
    broker = service.resident
    monkeypatch.setattr(
        broker.firmware_source.snapshots,
        "resolve",
        lambda *args: (
            tmp_path,
            {"fetch_job_id": "not-a-successful-fetch", "commit": "a" * 40},
        ),
    )
    async with connect(service.url, service.keys["second"].token) as client:
        result = await call(
            client,
            "submit",
            {
                "job_id": "source-bound-build",
                "kind": "build",
                "project_id": "fixture",
                "source_receipt": "c" * 64,
            },
        )
    assert result == {"ok": False, "error": "successful_owned_source_fetch_required"}
    assert service.backend.calls == 0 and broker._store().record_count("jobs") == 0


def test_only_typed_allowlisted_source_errors_escape_generic_value_error_mask():
    def source():
        raise SourceError("https://SYNTHETIC_SECRET.invalid")

    def other():
        raise ValueError("SYNTHETIC_SECRET")

    assert _result(source) == {"ok": False, "error": "source_fetch_failed"}
    assert _result(other) == {"ok": False, "error": "invalid_request"}
