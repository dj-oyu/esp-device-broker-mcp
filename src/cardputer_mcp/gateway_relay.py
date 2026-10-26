"""Claude gateway relay: authenticated ``/mcp`` requests to the broker's ``/tunnel/mcp``.

Only MCP is relayed, always to the fixed loopback alias that requires a remote
``read`` principal; ``/operator/*`` is unreachable from here. The request is
rebuilt from an allowlist of MCP headers: Claude's bearer token, cookies, Origin
and any ``Forwarded``/``X-Forwarded-*`` metadata never reach the broker, and the
broker sees ``Host: 127.0.0.1:<port>`` with the gateway's own broker key.

The broker key comes from the same private env file as the stdio adapter. Only
when the broker's authentication guard rejected a request (so it provably did not
run) is the file re-read once and that request sent once more. A rejected key is
reported to Claude as 502, never 401, so Claude does not loop through OAuth.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from pathlib import Path

import anyio
import httpx2
from starlette.responses import JSONResponse
from starlette.types import Message, Receive, Scope, Send

from . import client_environment
from .broker_link import canonical_broker_port

logger = logging.getLogger("cardputer_mcp.gateway")

MAX_BODY = 4 * 1024 * 1024
_KEY = re.compile(r"cpk_[a-f0-9]{32}\.[a-f0-9]{64}\Z")
_AUTH_GUARD_BODY = b'{"error":"authentication_required"}'
_REQUEST_HEADERS = ("accept", "content-type", "mcp-session-id", "mcp-protocol-version")
_GET_HEADERS = ("last-event-id",)
_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "mcp-protocol-version", "cache-control")
_METHODS = ("GET", "POST", "DELETE")


class RelayConfigError(ValueError):
    """A fixed public error; never carries the key, the URL or file contents."""


class BrokerRelay:
    """ASGI app mounted behind the gateway's bearer check."""

    def __init__(
        self,
        url: str,
        token: str,
        env_file: Path | None = None,
        *,
        http: httpx2.AsyncClient | None = None,
        load: Callable[[Path], client_environment.ClientEnvironment] = (
            client_environment.load_client_environment
        ),
    ) -> None:
        port = canonical_broker_port(url)
        if port is None:
            raise RelayConfigError("gateway_broker_url_invalid")
        if not isinstance(token, str) or _KEY.fullmatch(token) is None:
            raise RelayConfigError("gateway_broker_key_invalid")
        self._url = url
        self._target = f"http://127.0.0.1:{port}/tunnel/mcp"
        self._token = token
        self._env_file = env_file
        self._load = load
        self._reloading = asyncio.Lock()
        self._http = http or httpx2.AsyncClient(
            timeout=httpx2.Timeout(connect=5.0, read=None, write=30.0, pool=5.0),
            trust_env=False,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _reload(self, rejected: str) -> str | None:
        """A newer key from the env file, or None (no retry)."""
        async with self._reloading:
            if self._token != rejected:
                return self._token
            if self._env_file is None:
                return None
            try:
                loaded = await asyncio.to_thread(self._load, self._env_file)
            except client_environment.ClientEnvironmentError:
                logger.warning("gateway_broker_key_reload_failed reason=env_invalid")
                return None
            if loaded.url != self._url:
                # Never switch brokers under a running gateway.
                logger.warning("gateway_broker_key_reload_failed reason=url_changed")
                return None
            if loaded.token == rejected or _KEY.fullmatch(loaded.token) is None:
                logger.warning("gateway_broker_key_reload_failed reason=same_key")
                return None
            self._token = loaded.token
            logger.info("gateway_broker_key_reloaded")
            return loaded.token

    async def _open(
        self, method: str, headers: dict[str, str], body: bytes, token: str
    ) -> httpx2.Response:
        request = self._http.build_request(
            method,
            self._target,
            headers={**headers, "authorization": "Bearer " + token},
            content=body if method == "POST" else None,
        )
        return await self._http.send(request, stream=True)

    @staticmethod
    async def _guard_rejected(response: httpx2.Response) -> bool:
        if response.status_code != 401:
            return False
        # Broker 401 bodies are fixed and tiny; the body stays readable for relaying.
        await response.aread()
        return response.content == _AUTH_GUARD_BODY

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = scope["method"]
        if scope["type"] != "http" or method not in _METHODS:
            await JSONResponse({"error": "method_not_allowed"}, status_code=405)(
                scope, receive, send
            )
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY:
                await JSONResponse({"error": "request_too_large"}, status_code=413)(
                    scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break
        incoming = {}
        for raw_key, raw_value in scope["headers"]:
            key = raw_key.decode("latin-1").lower()
            if key in _REQUEST_HEADERS or (method == "GET" and key in _GET_HEADERS):
                incoming[key] = raw_value.decode("latin-1")
        response = await self._exchange(method, incoming, bytes(body))
        if response is None:
            await JSONResponse({"error": "broker_unavailable"}, status_code=502)(
                scope, receive, send
            )
            return
        if isinstance(response, str):
            await JSONResponse({"error": response}, status_code=502)(scope, receive, send)
            return
        try:
            await self._stream(response, receive, send)
        finally:
            await response.aclose()

    async def _exchange(
        self, method: str, headers: dict[str, str], body: bytes
    ) -> httpx2.Response | str | None:
        token = self._token
        for attempt in (0, 1):
            try:
                response = await self._open(method, headers, body, token)
            except httpx2.HTTPError:
                logger.warning("gateway_relay_failed reason=broker_unreachable")
                return None
            if not await self._guard_rejected(response):
                return response
            await response.aclose()
            if attempt == 1:
                break
            fresh = await self._reload(token)
            if fresh is None:
                break
            token = fresh
        logger.warning("gateway_relay_failed reason=broker_key_rejected")
        return "broker_key_rejected"

    @staticmethod
    async def _stream(response: httpx2.Response, receive: Receive, send: Send) -> None:
        headers = [
            (key.encode("latin-1"), value.encode("latin-1"))
            for key, value in response.headers.items()
            if key.lower() in _RESPONSE_HEADERS
        ]
        await send(
            {"type": "http.response.start", "status": response.status_code, "headers": headers}
        )

        async with anyio.create_task_group() as group:

            async def watch_disconnect() -> None:
                while True:
                    message: Message = await receive()
                    if message["type"] == "http.disconnect":
                        group.cancel_scope.cancel()
                        return

            group.start_soon(watch_disconnect)
            # Decoded bytes: Content-Encoding is never forwarded.
            async for chunk in response.aiter_bytes():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            group.cancel_scope.cancel()
