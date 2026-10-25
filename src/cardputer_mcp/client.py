"""Every CLI operation uses the same running HTTP broker. No in-process device fallback."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .broker_link import is_local_mcp_url

# The complete MCP tool surface; every in-repo client discovers exactly these names.
SURFACE_TOOLS = ("status", "submit", "execute", "flash", "cancel", "session")


def validate_url(url: str) -> str:
    if not is_local_mcp_url(url):
        raise ValueError("This MVP connects only to a local HTTP /mcp endpoint")
    return url


async def call(client: Client, tool: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await client.call_tool(tool, {} if request is None else {"request": request})
    if result.is_error:
        raise ValueError("MCP request rejected; check the typed arguments")
    data = result.structured_content
    if not isinstance(data, dict):
        raise ValueError("MCP response had no structured result")
    return data


@asynccontextmanager
async def connect(url: str, token: str | None = None) -> AsyncIterator[Client]:
    # Loopback traffic must never inherit an external HTTP/SOCKS proxy.
    url = validate_url(url)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx2.AsyncClient(trust_env=False, timeout=30, headers=headers) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            yield client
