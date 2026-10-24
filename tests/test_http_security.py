"""Local-only HTTP boundary tests; no sockets, USB, or real credentials."""

from pathlib import Path
from typing import Any

import httpx2
import pytest
from starlette.types import Message, Receive, Scope, Send

from cardputer_mcp.broker import Broker
from cardputer_mcp.client import validate_url
from cardputer_mcp.config import Settings
from cardputer_mcp.server import LoopbackOnly, create_app, create_server


async def request_boundary(
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 12345),
    method: str = "POST",
) -> tuple[list[Message], bool]:
    messages: list[Message] = []
    called = False

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "method": method,
        "path": "/mcp",
        "client": client,
        "headers": headers
        if headers is not None
        else [
            (b"host", b"127.0.0.1:8765"),
            (b"content-type", b"application/json"),
        ],
    }
    await LoopbackOnly(app, 8765)(scope, receive, send)
    return messages, called


def assert_blocked(result: tuple[list[Message], bool]) -> None:
    messages, called = result
    assert called is False
    assert messages[0]["status"] == 403
    assert messages[1]["body"] == b'{"error":"local_only"}'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("host", "client"),
    [(b"127.0.0.1:8765", ("127.0.0.1", 12345)), (b"localhost:8765", ("::1", 12345))],
)
async def test_http_exact_local_request_is_allowed(host: bytes, client: tuple[str, int]) -> None:
    messages, called = await request_boundary(
        headers=[
            (b"host", host),
            (b"origin", b"http://" + host),
            (b"content-type", b"application/json; charset=utf-8"),
        ],
        client=client,
    )
    assert called is True
    assert messages[0]["status"] == 204


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        b"example.invalid:8765",
        b"127.0.0.1",
        b"",
        None,
    ],
)
async def test_http_rejects_untrusted_hosts(host: bytes | None) -> None:
    headers = [(b"content-type", b"application/json")]
    if host is not None:
        headers.insert(0, (b"host", host))
    assert_blocked(await request_boundary(headers=headers))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        b"https://evil.invalid",
        b"null",
        b"https://127.0.0.1:8765",
    ],
)
async def test_http_rejects_untrusted_origins(origin: bytes) -> None:
    assert_blocked(
        await request_boundary(
            headers=[
                (b"host", b"127.0.0.1:8765"),
                (b"origin", origin),
                (b"content-type", b"application/json"),
            ]
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        b"Forwarded",
        b"X-FORWARDED-Custom",
    ],
)
async def test_http_rejects_proxy_headers(header: bytes) -> None:
    assert_blocked(
        await request_boundary(
            headers=[
                (b"host", b"127.0.0.1:8765"),
                (header, b"127.0.0.1"),
                (b"content-type", b"application/json"),
            ]
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client",
    [
        None,
        ("192.0.2.10", 12345),
        ("127.0.0.2", 12345),
    ],
)
async def test_http_rejects_remote_or_unidentified_clients(client: tuple[str, int] | None) -> None:
    assert_blocked(await request_boundary(client=client))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    [
        b"text/plain",
        b"application/jsonp",
        b"",
    ],
)
async def test_http_rejects_non_json_post(content_type: bytes) -> None:
    assert_blocked(
        await request_boundary(
            headers=[
                (b"host", b"127.0.0.1:8765"),
                (b"content-type", content_type),
            ]
        )
    )


@pytest.mark.asyncio
async def test_http_untrusted_header_bytes_are_rejected_without_exception() -> None:
    assert_blocked(
        await request_boundary(
            headers=[
                (b"host", b"127.0.0.1:8765"),
                (b"content-type", b"application/json"),
                (b"x-untrusted", b"\xff"),
            ]
        )
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765/mcp",
        "http://127.0.0.1:8765/mcp?secret=synthetic",
        "http://name:secret@127.0.0.1:8765/mcp",
        "http://127.0.0.1:8765/mcp/",
        "http://evil.invalid\\@127.0.0.1:8765/mcp",
    ],
)
def test_client_rejects_nonlocal_or_ambiguous_urls(url: str) -> None:
    assert validate_url("http://127.0.0.1:8765/mcp") == "http://127.0.0.1:8765/mcp"
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_fields, marker",
    [
        ({"job_id": "https://synthetic.invalid/private-token"}, "synthetic.invalid/private-token"),
        ({"bearer_secret": "SYNTHETIC_ONLY_SHOULD_NOT_ECHO"}, "SYNTHETIC_ONLY_SHOULD_NOT_ECHO"),
    ],
)
async def test_tool_validation_never_echoes_raw_input(
    invalid_fields: dict[str, str],
    marker: str,
) -> None:
    server = create_server(Settings())
    request: dict[str, Any] = {"session": "a" * 64, "job_id": "safe", **invalid_fields}
    try:
        result = await server.call_tool("submit", {"request": {**request}})
    except Exception as exc:
        rendered = str(exc)
    else:
        assert result.is_error is True
        rendered = repr(result)
    assert marker not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25", "2026-07-28"])
async def test_http_envelope_validation_never_echoes_raw_input(
    tmp_path: Path,
    version: str,
) -> None:
    settings = Settings(state_dir=tmp_path / "state")
    broker = Broker(settings, lock_path=tmp_path / "device.lockfile")
    app = create_app(settings, broker)
    inner = app
    while not hasattr(inner, "router"):
        inner = inner.app
    marker = "SYNTHETIC_ENVELOPE_SECRET"
    async with inner.router.lifespan_context(inner):
        transport = httpx2.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
            trust_env=False,
        ) as http:
            response = await http.post(
                "/mcp",
                headers={
                    "accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": version,
                },
                json={"jsonrpc": "2.0", "id": 1, "method": {"secret": marker}},
            )
    assert response.status_code >= 400
    assert marker not in response.text
