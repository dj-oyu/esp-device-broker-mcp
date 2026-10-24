"""Characterization: the host-visible tools/list catalog and result text encoding (mock only).

The catalog is what a host imports and cannot refresh automatically, so any change to it must be
a deliberate fixture update. Regenerate after an intended change with
UPDATE_SURFACE_SNAPSHOT=1 and review the fixture diff.
"""

import asyncio
import difflib
import json
import os
from pathlib import Path

import httpx2
import pytest
from surface_harness import REMOTE, raw, serve

from cardputer_mcp.client import SURFACE_TOOLS, connect

SNAPSHOT = Path(__file__).parent / "fixtures" / "surface_tools_list.json"


@pytest.fixture(scope="module")
def event_loop_policy():
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.mark.parametrize("require_auth", [True, False])
async def test_tools_list_matches_the_golden_snapshot(tmp_path: Path, require_auth: bool) -> None:
    # The catalog must not depend on whether the broker requires authentication.
    async with serve(tmp_path, require_auth=require_auth) as surface:
        async with connect(surface.url, REMOTE) as client:
            tools = (await client.list_tools()).tools
    actual = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
    rendered = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_SURFACE_SNAPSHOT") == "1":
        SNAPSHOT.write_text(rendered, encoding="utf-8", newline="\n")
    expected = SNAPSHOT.read_text(encoding="utf-8")
    if rendered != expected:
        diff = "".join(
            difflib.unified_diff(
                expected.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"{SNAPSHOT.name} (checked in)",
                tofile="tools/list (current server)",
            )
        )
        pytest.fail(
            "tools/list drifted from the golden snapshot. Hosts keep imported metadata until a "
            "manual Refresh; if intended, rerun with UPDATE_SURFACE_SNAPSHOT=1.\n" + diff,
            pytrace=False,
        )
    # Cheap structural pins that read better than a fixture diff when they break.
    assert [tool["name"] for tool in actual] == list(SURFACE_TOOLS)
    assert [
        name for name, tool in zip(SURFACE_TOOLS, actual, strict=True) if "outputSchema" in tool
    ] == [
        "submit",
        "execute",
        "flash",
        "cancel",
        "session",
    ]
    assert all("idempotentHint" not in tool["annotations"] for tool in actual)


async def test_list_changed_capability_by_handshake(tmp_path: Path) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with httpx2.AsyncClient(trust_env=False) as http:
            response = await http.post(
                surface.url,
                headers={
                    "Authorization": f"Bearer {REMOTE}",
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "characterization", "version": "0"},
                    },
                },
            )
        async with connect(surface.url, REMOTE) as client:
            negotiated = client.protocol_version
            modern = client.server_capabilities
    # Legacy initialize (what hosts importing metadata use): no tool-list notifications.
    assert response.status_code == 200
    assert response.json()["result"]["capabilities"] == {
        "experimental": {},
        "prompts": {"listChanged": False},
        "resources": {"listChanged": False, "subscribe": False},
        "tools": {"listChanged": False},
    }
    # Current behaviour, pinned so a change is noticed: the in-repo client negotiates the
    # 2026-07-28 discover handshake, where the SDK reports listChanged=true although no
    # notification path exists in this repository.
    assert negotiated == "2026-07-28"
    assert modern.tools is not None and modern.tools.list_changed is True


@pytest.mark.parametrize(
    "tool,request_body,encoding",
    [
        # status returns its own CallToolResult: compact JSON with sorted keys.
        ("status", {"view": "identity", "job_id": None}, "sorted"),
        # The other tools return dicts: the SDK renders indent=2 in insertion order.
        ("session", {"action": "renew"}, "indented"),
        # Pre-validation failures (any tool) are compact and sorted, with isError.
        ("execute", {"op": "run", "plan_hash": None}, "sorted"),
    ],
)
async def test_text_content_encoding_per_tool(
    tmp_path: Path, tool: str, request_body: dict, encoding: str
) -> None:
    async with serve(tmp_path, require_auth=True) as surface:
        async with connect(surface.url, REMOTE) as client:
            _, body, text = await raw(client, tool, request_body)
    assert isinstance(body, dict)
    if encoding == "sorted":
        assert text == json.dumps(body, sort_keys=True)
    else:
        assert text == json.dumps(body, indent=2)
        assert list(body) == list(json.loads(text))  # insertion order, not sorted
        assert text != json.dumps(body, indent=2, sort_keys=True)
