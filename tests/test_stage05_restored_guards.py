"""Guards an earlier suite trim deleted, re-pinned at their lowest owning layer.

Synthetic files and keys only; no hardware, no real broker, no real key registry.
"""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
import pytest
from test_registry_http import registry_service as registry_service
from test_stdio_adapter import KEY, SECRET, _exchange, _start_stdio
from test_stdio_adapter import synthetic_service as synthetic_service
from test_tunnel_config import OTHER_ID, broker_file, legacy_file, metadata

from cardputer_mcp import tunnel
from cardputer_mcp.build_locations import (
    BuildLocationError,
    check_storage_before_write,
    check_windows_path,
    claim_build_directory,
    output_base,
)
from cardputer_mcp.client import SURFACE_TOOLS, call, connect
from cardputer_mcp.firmware_build import BuildFlashLayoutFailure, build_flash_layout_receipt
from cardputer_mcp.stdio_adapter import HTTPBrokerAdapter

REPARSE = 0x400
META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _reparse_lstat(target):
    """Make one path report a Windows reparse attribute; everything else is real."""
    original = Path.lstat

    def lstat(path):
        value = original(path)
        if target(path):
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=REPARSE)
        return value

    return patch.object(Path, "lstat", lstat)


# --- tunnel config: a conflicting save is all-or-nothing ---------------------------


def test_conflicting_save_leaves_all_files_untouched(tmp_path):
    path = broker_file(tmp_path, metadata())
    legacy = legacy_file(tmp_path)
    before = (path.read_bytes(), legacy.read_bytes())
    with pytest.raises(tunnel.TunnelError, match="^tunnel_config_conflict$"):
        tunnel.save_gui_config(path, tmp_path, metadata(tunnel_id=OTHER_ID))
    assert (path.read_bytes(), legacy.read_bytes()) == before
    assert list(tmp_path.glob(".tunnel-config-*")) == []


# --- identity: a read key is reported without its token; revocation is immediate ----


async def test_identity_read_key_and_revocation(registry_service):
    service = registry_service
    key = service.keys["reader"]
    async with connect(service.url, key.token) as client:
        result = await call(client, "status", {"view": "identity"})
    assert result["origin"] == "remote_mcp"
    assert result["principal_id"] == key.metadata.principal_id
    assert result["scopes"] == ["read"]
    assert key.token not in json.dumps(result)
    service.registry.revoke(key.metadata.key_id)
    async with httpx2.AsyncClient(trust_env=False) as http:
        denied = await http.post(
            service.url, json={}, headers={"Authorization": "Bearer " + key.token}
        )
    assert denied.status_code == 401
    assert denied.json() == {"error": "authentication_required"}
    assert key.token not in denied.text


# --- build flash layout receipt: no link or reparse point is followed ---------------


@pytest.fixture
def layout(tmp_path):
    binary = tmp_path / "firmware.bin"
    binary.write_bytes(b"synthetic firmware")
    artifacts = [
        {
            "name": "firmware.bin",
            "bytes": binary.stat().st_size,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
    ]
    raw = json.dumps(
        {
            "flash_files": {"0x10000": "firmware.bin"},
            "extra_esptool_args": {"chip": "esp32s3"},
            "flash_settings": {"flash_mode": "dio", "flash_size": "8MB", "flash_freq": "80m"},
            "write_flash_args": ["--flash-mode", "dio", "--flash-size", "8MB"],
        }
    ).encode()
    (tmp_path / "flasher_args.json").write_bytes(raw)
    build_flash_layout_receipt(tmp_path, "esp32s3", artifacts)  # the plain file is accepted
    return tmp_path, raw, artifacts


def test_linked_layout_is_rejected(layout):
    root, raw, artifacts = layout
    outside = root / "outside.json"
    outside.write_bytes(raw)
    path = root / "flasher_args.json"
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are not available to this user")
    with pytest.raises(BuildFlashLayoutFailure):
        build_flash_layout_receipt(root, "esp32s3", artifacts)


def test_reparse_layout_is_rejected(layout):
    root, _, artifacts = layout
    with _reparse_lstat(lambda path: path.name == "flasher_args.json"):
        with pytest.raises(BuildFlashLayoutFailure):
            build_flash_layout_receipt(root, "esp32s3", artifacts)


# --- build output locations: root, long path, reparse and free space ---------------


@pytest.fixture
def build_root(tmp_path):
    root = tmp_path.resolve()
    return root / "state", root / "short"


def test_filesystem_root_is_rejected(build_root):
    state, output = build_root
    with pytest.raises(BuildLocationError, match="^build_output_root_invalid$"):
        claim_build_directory(state, Path(output.anchor), "a" * 64, "b" * 64)


def test_long_windows_path_has_fixed_diagnostic():
    check_windows_path(PureWindowsPath("C:/b") / ("a" * 64), windows=True)
    with pytest.raises(BuildLocationError, match="^build_output_path_too_long$"):
        check_windows_path(PureWindowsPath("C:/" + "x" * 170) / ("a" * 64), windows=True)


def test_reparse_root_marker_is_rejected(build_root):
    state, output = build_root
    output.mkdir()
    with _reparse_lstat(lambda path: path == output):
        with pytest.raises(BuildLocationError, match="^build_output_linked$"):
            output_base(state, output)


def test_insufficient_space_is_refused_before_any_write(build_root):
    state, output = build_root
    with patch(
        "cardputer_mcp.build_locations.shutil.disk_usage",
        return_value=SimpleNamespace(free=1, total=100),
    ):
        with pytest.raises(BuildLocationError, match="^build_output_insufficient_space$"):
            check_storage_before_write(state, output, 2)
    assert not output.exists()


# --- stdio adapter through a real subprocess ---------------------------------------


async def test_stdio_lost_response_is_a_fixed_code_and_stderr_is_sanitized(synthetic_service):
    service = synthetic_service
    service.lose_response = True
    process = await _start_stdio(service.url, KEY)
    try:
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mixed", "_meta": META},
        }
        result = await _exchange(process, frame)
        assert result["result"]["isError"] is True
        assert result["result"]["content"] == [
            {"type": "text", "text": "stdio_adapter_tool_outcome_unknown"}
        ]
        process.stdin.close()
        tail, errors = await asyncio.wait_for(process.communicate(), timeout=10)
        assert tail == b"" and process.returncode == 0
        assert SECRET.encode() not in errors and KEY.encode() not in errors
        assert b"Traceback" not in errors
        assert [name for name, _ in service.calls].count("mixed") == 1
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()


def test_real_cli_missing_env_file_keeps_protocol_stdout_clean(tmp_path):
    private = tmp_path / "SYNTHETIC_PRIVATE_DIR" / "missing.env"
    result = subprocess.run(
        [sys.executable, "-m", "cardputer_mcp.cli", "stdio", "--env-file", str(private)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1 and result.stdout == ""
    assert result.stderr == "invalid_client_environment\n"


async def test_stdio_wire_passes_six_tool_schemas_and_annotations_unchanged(registry_service):
    """The real 6-tool catalog crosses the stdio wire with schemas and annotations intact."""
    service = registry_service
    key = service.keys["reader"].token
    async with connect(service.url, key) as upstream:
        expected = (await upstream.list_tools()).tools
    assert tuple(tool.name for tool in expected) == SURFACE_TOOLS
    wire_expected = [tool.model_dump(by_alias=True, exclude_none=True) for tool in expected]
    process = await _start_stdio(service.url, key)
    try:
        listing = await _exchange(
            process,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": META}},
        )
        process.stdin.close()
        tail, errors = await asyncio.wait_for(process.communicate(), timeout=10)
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
    assert tail == b"" and process.returncode == 0 and key.encode() not in errors
    tools = listing["result"]["tools"]
    assert [tool["name"] for tool in tools] == list(SURFACE_TOOLS)
    for got, want in zip(tools, wire_expected, strict=True):
        assert got["annotations"] == want["annotations"], got["name"]
        assert got["inputSchema"] == want["inputSchema"], got["name"]
        assert got.get("outputSchema") == want.get("outputSchema"), got["name"]
        assert got.get("description") == want.get("description"), got["name"]
    hints = {tool["name"]: tool["annotations"] for tool in tools}
    assert [name for name, a in hints.items() if a["readOnlyHint"]] == ["status"]
    assert [name for name, a in hints.items() if a["destructiveHint"]] == ["flash"]
    assert all(a["openWorldHint"] is False for a in hints.values())
    assert all("idempotentHint" not in a for a in hints.values())
    # The adapter object also forwards the same catalog (no side effects).
    adapter_tools = (await HTTPBrokerAdapter(service.url, key).list_tools()).tools
    assert adapter_tools == expected
    assert service.resident.session is None and service.resident.active_job is None
