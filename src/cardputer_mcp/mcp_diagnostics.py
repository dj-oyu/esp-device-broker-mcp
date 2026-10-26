"""Read-only live MCP catalog diagnostics for the already-authenticated local GUI."""

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult

from .client import validate_url
from .stdio_adapter import _quiet_dependencies

GUI_CODE_VERSION = "0.13.1"
TOTAL_SECONDS = 5.0
_VERSION = re.compile(
    r"(?:unknown|[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:[-+][A-Za-z0-9.-]{1,16})?)\Z"
)
_FIELDS = {
    "gui_version",
    "server_version",
    "tools_count",
    "read_capture",
    "capture_home",
    "broker_epoch",
}


class MCPDiagnosticsError(ValueError):
    """Only fixed public categories; no SDK response or credential values."""


def parse_summary(data: dict[str, Any]) -> dict[str, Any]:
    try:
        if set(data) != _FIELDS or data["gui_version"] != GUI_CODE_VERSION:
            raise ValueError
        for name in ("gui_version", "server_version"):
            if not isinstance(data[name], str) or _VERSION.fullmatch(data[name]) is None:
                raise ValueError
        if type(data["tools_count"]) is not int or not 0 <= data["tools_count"] <= 256:
            raise ValueError
        if type(data["read_capture"]) is not bool or type(data["capture_home"]) is not bool:
            raise ValueError
        if (
            not isinstance(data["broker_epoch"], str)
            or re.fullmatch(r"[a-f0-9]{32}", data["broker_epoch"]) is None
        ):
            raise ValueError
        return {key: data[key] for key in sorted(_FIELDS)}
    except (KeyError, TypeError, ValueError):
        raise MCPDiagnosticsError("invalid_mcp_summary") from None


@asynccontextmanager
async def connect(url: str, token: str) -> AsyncIterator[Client]:
    async def reject_redirect(response: httpx2.Response) -> None:
        if response.is_redirect:
            raise MCPDiagnosticsError("redirect_rejected")

    async with httpx2.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=TOTAL_SECONDS,
        headers={"Authorization": f"Bearer {token}"},
        event_hooks={"response": [reject_redirect]},
    ) as http:
        transport = streamable_http_client(
            validate_url(url), http_client=http, terminate_on_close=False
        )
        async with Client(
            transport, mode="legacy", read_timeout_seconds=TOTAL_SECONDS, cache=None
        ) as client:
            yield client


async def _status(client: Client) -> dict[str, Any]:
    result = await client.session.send_request(
        CallToolRequest(
            params=CallToolRequestParams(name="status", arguments={"request": {"view": "device"}})
        ),
        CallToolResult,
        request_read_timeout_seconds=TOTAL_SECONDS,
    )
    if (
        result.is_error
        or result.result_type != "complete"
        or not isinstance(result.structured_content, dict)
    ):
        raise MCPDiagnosticsError("status_unavailable")
    return result.structured_content


def _enum_values(tool: Any, name: str) -> list[str]:
    values = tool.input_schema.get("$defs", {}).get(name, {}).get("enum", []) if tool else []
    if (
        not isinstance(values, list)
        or len(values) > 256
        or any(not isinstance(value, str) or len(value) > 64 for value in values)
    ):
        raise MCPDiagnosticsError("invalid_catalog_schema")
    return [str(value) for value in values]


async def _read_summary(url: str, token: str) -> dict[str, Any]:
    async with asyncio.timeout(TOTAL_SECONDS):
        async with connect(url, token) as client:
            catalog = await client.list_tools()
            status = await _status(client)
            if catalog.next_cursor is not None:
                raise MCPDiagnosticsError("catalog_incomplete")
            if status.get("ok") is not True:
                raise MCPDiagnosticsError("status_unavailable")
            info = client.server_info
            version = info.version if info is not None else None
            if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
                version = "unknown"
            submit = next((tool for tool in catalog.tools if tool.name == "submit"), None)
            status_tool = next((tool for tool in catalog.tools if tool.name == "status"), None)
            kinds = _enum_values(submit, "Kind")
            views = _enum_values(status_tool, "View")
            return parse_summary(
                {
                    "gui_version": GUI_CODE_VERSION,
                    "server_version": version,
                    "tools_count": len(catalog.tools),
                    "read_capture": "capture" in views,
                    "capture_home": "capture" in kinds,
                    "broker_epoch": status.get("broker_epoch"),
                }
            )


def probe(url: str, token: str) -> dict[str, Any]:
    """Run on the existing GUI worker; never creates credentials or a broker."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise MCPDiagnosticsError("diagnostic_worker_required")
    with _quiet_dependencies():
        try:
            return asyncio.run(_read_summary(url, token))
        except TimeoutError:
            raise MCPDiagnosticsError("live_probe_timed_out") from None
        except Exception:
            raise MCPDiagnosticsError("live_probe_failed") from None
