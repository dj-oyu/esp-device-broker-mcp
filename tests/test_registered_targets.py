"""Authenticated TCP/MCP registration discovery without device or job effects."""

import json
from dataclasses import replace

import httpx2
import pytest
from pydantic import ValidationError
from test_authenticated_mock import LOCAL, REMOTE
from test_authenticated_mock import authenticated_mock as authenticated_mock
from test_http_integration import service as service

from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject, Settings, SourceRepository
from cardputer_mcp.flash_models import FlashDevice
from cardputer_mcp.server import RequestAuth


def registrations(tmp_path):
    project = BuildProject(
        project_dir=tmp_path / "SYNTHETIC_PRIVATE_PROJECT",
        trusted_profile=tmp_path / "SYNTHETIC_PRIVATE_PROFILE.ps1",
        source_repository=SourceRepository(
            github_repository="SYNTHETIC_PRIVATE/repository",
            cache_root=tmp_path / "SYNTHETIC_PRIVATE_CACHE",
        ),
    )
    device = FlashDevice(
        selector={"vid": 1, "pid": 2, "serial_number": "SYNTHETIC_PRIVATE_SERIAL"},
        flash_bytes=8388608,
        project_id="alpha-project",
        python_tool={
            "executable": tmp_path / "python.exe",
            "sha256": "c" * 64,
            "version": "Python 3.13.16",
        },
        esptool_directory=tmp_path / "SYNTHETIC_PRIVATE_TOOLS",
        esptool_sha256="d" * 64,
        backup_directory=tmp_path / "SYNTHETIC_PRIVATE_BACKUP",
        allow_backup=False,
        allow_flash=False,
        allow_restore=False,
    )
    return project, device


def assert_no_work(resident):
    assert resident.backend.calls == 0
    assert not resident.backend.hardware_connected
    assert resident.active_job is None and resident.session is None
    assert not resident.work_sessions and resident.queue.empty()
    assert resident._store().record_count("jobs") == 0
    assert resident._store().record_count("work_sessions") == 0


async def test_registered_targets_exact_ids_only_and_current_settings(
    authenticated_mock, tmp_path, monkeypatch
):
    url, resident = authenticated_mock
    project, device = registrations(tmp_path)
    # Deliberately use valid legacy config with names that cannot be request IDs.
    resident.settings = Settings.model_validate(
        {
            **resident.settings.model_dump(),
            "build_projects": {
                "z-project": project,
                "alpha-project": project,
                str(tmp_path / "SYNTHETIC_PRIVATE_KEY"): project,
                "x" * 65: project,
            },
            "flash_devices": {
                "z-device": device,
                "alpha-device": device,
                "https://SYNTHETIC_PRIVATE.invalid": device,
            },
        }
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("discovery must not acquire, prepare, execute or access hardware")

    for target, names in (
        (resident, ("acquire", "create_work_session", "prepare", "execute")),
        (resident.backend, ("prepare", "execute")),
    ):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)

    async with connect(url, REMOTE) as client:
        result = await call(client, "list_registered_targets")
        assert result == {
            "ok": True,
            "build_projects": [{"project_id": "alpha-project"}, {"project_id": "z-project"}],
            "flash_devices": [{"device_id": "alpha-device"}, {"device_id": "z-device"}],
        }
        rendered = json.dumps(result)
        for private in ("SYNTHETIC_PRIVATE", str(tmp_path), LOCAL, REMOTE, "c" * 64, "d" * 64):
            assert private not in rendered
        # Replacing settings after server creation must change the next response.
        resident.settings = resident.settings.model_copy(
            update={"build_projects": {}, "flash_devices": {"new-device": device}}
        )
        assert await call(client, "list_registered_targets") == {
            "ok": True,
            "build_projects": [],
            "flash_devices": [{"device_id": "new-device"}],
        }
        resident.settings = resident.settings.model_copy(update={"flash_devices": {}})
        assert await call(client, "list_registered_targets") == {
            "ok": True,
            "build_projects": [],
            "flash_devices": [],
        }
    assert_no_work(resident)


async def test_registered_targets_requires_current_read_scope(authenticated_mock, monkeypatch):
    url, resident = authenticated_mock
    authenticate = RequestAuth.authenticate
    scopes = [frozenset({"read"})]

    def scoped(self, header):
        owner = authenticate(self, header)
        return replace(owner, scopes=scopes[0]) if owner is not None else None

    # Synthetic current-scope changes exercise the real HTTP auth/tool path,
    # without a registry fixture's platform-specific filesystem requirements.
    monkeypatch.setattr(RequestAuth, "authenticate", scoped)
    async with connect(url, REMOTE) as client:
        assert await call(client, "list_registered_targets") == {
            "ok": True,
            "build_projects": [],
            "flash_devices": [],
        }
        scopes[0] = frozenset({"control"})
        assert await call(client, "list_registered_targets") == {
            "ok": False,
            "error": "insufficient_scope",
        }
    assert_no_work(resident)


async def test_registered_targets_metadata_empty_input_and_read_only(authenticated_mock):
    url, resident = authenticated_mock
    async with connect(url, LOCAL) as client:
        catalog = (await client.list_tools()).tools
        tool = next(tool for tool in catalog if tool.name == "list_registered_targets")
        assert tool.input_schema["type"] == "object"
        assert not tool.input_schema.get("properties")
        assert not tool.input_schema.get("required")
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.open_world_hint is False
        assert "project_id" in tool.description and "device_id" in tool.description
        rejected = await client.call_tool("list_registered_targets", {"path": "SYNTHETIC_PRIVATE"})
        assert rejected.is_error
        assert "SYNTHETIC_PRIVATE" not in json.dumps(rejected.model_dump(mode="json"))
    async with httpx2.AsyncClient(trust_env=False) as http:
        for headers in ({}, {"Authorization": "Bearer " + "c" * 64}):
            denied = await http.post(
                url,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list_registered_targets", "arguments": {}},
                },
            )
            assert denied.status_code == 401
            assert denied.json() == {"error": "authentication_required"}
    assert_no_work(resident)


async def test_registered_targets_requires_auth_even_on_legacy_mock(service):
    async with connect(service) as client:
        assert await call(client, "list_registered_targets") == {
            "ok": False,
            "error": "authentication_required",
        }


@pytest.mark.parametrize("field,limit", [("build_projects", 16), ("flash_devices", 8)])
def test_registered_target_count_remains_bounded(tmp_path, field, limit):
    project, device = registrations(tmp_path)
    registration = project if field == "build_projects" else device
    maximum = {f"target-{index}": registration for index in range(limit)}
    assert len(getattr(Settings.model_validate({field: maximum}), field)) == limit
    with pytest.raises(ValidationError):
        Settings.model_validate({field: {**maximum, "one-too-many": registration}})
