"""Tools-only stdio adapter for an already running, authenticated HTTP broker.

The adapter owns transport connections only. It never starts a broker, creates a
work session, acquires a device, releases ownership, or cancels a broker job.
Every downstream request gets a fresh, bounded HTTP connection; a failed call is
never replayed, even when its response was lost.

One exception is narrow and provable: when the broker's HTTP authentication
guard rejects the bearer (401 with its fixed body), the rejected HTTP request
never reached an MCP handler. The adapter then re-reads its private env file
once; only if that yields a different valid key for the same broker URL does it
reconnect and send the request once more. A tool call is resent only when the
rejected request was the call itself or the call had not been sent yet.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.types import (
    INTERNAL_ERROR,
    CallToolRequest,
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
)

from . import client_environment
from .broker_link import canonical_broker_port

_KEY = re.compile(r"cpk_[a-f0-9]{32}\.[a-f0-9]{64}\Z")
_REQUEST_SECONDS = 30.0
# Tools that queue work verify firmware inputs before they answer; one deadline
# per request still covers connect, identity, the call and teardown.
_TOOL_SECONDS = {"flash": 120.0, "execute": 120.0, "submit": 120.0}
_OUTCOME_UNKNOWN = "stdio_adapter_tool_outcome_unknown"
_KEY_REJECTED = "stdio_adapter_client_key_rejected"
_ENV_INVALID = "stdio_adapter_client_environment_invalid"
_URL_CHANGED = "stdio_adapter_broker_url_changed"
# The exact 401 body of the broker's pre-dispatch authentication guards
# (server.LoopbackOnly / TunnelIngress). Handler error bodies are rewritten to
# request_rejected, so this body proves the request never reached the MCP app.
_AUTH_GUARD_BODY = b'{"error":"authentication_required"}'
_PLAN_HASH = re.compile(r"[a-f0-9]{64}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_CONNECTIONS = 4
_T = TypeVar("_T")


class StdioAdapterError(ValueError):
    """Fixed public error codes, never upstream exceptions or credential values."""


class _KeyRejected(Exception):
    """Internal: the broker's authentication guard rejected this connection's bearer.

    `replay_safe` is True only when the operation provably did not run: it is
    read-only, its request had not been sent, or the guard rejected that request.
    """

    def __init__(self, replay_safe: bool) -> None:
        super().__init__()
        self.replay_safe = replay_safe


async def _is_auth_guard_rejection(response: httpx2.Response) -> bool:
    """Read at most a few bytes; anything but the guard's fixed body is not proof."""
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > len(_AUTH_GUARD_BODY):
                return False
    except Exception:
        return False
    return bytes(body) == _AUTH_GUARD_BODY


def _is_jsonrpc_request(request: httpx2.Request) -> bool:
    """A JSON-RPC request (not a notification) was sent on this HTTP request."""
    try:
        frame = json.loads(request.content)
    except Exception:
        return False
    return isinstance(frame, dict) and "id" in frame and isinstance(frame.get("method"), str)


@contextmanager
def _quiet_dependencies() -> Iterator[None]:
    """SDK transport debug/exception logs can contain complete protocol payloads.

    This dedicated stdio entry point reports only our fixed errors. Restore the
    process logging configuration on exit for callers embedding the entry point.
    """
    prefixes = ("mcp", "httpx2", "httpx", "httpcore", "httpcore2")
    # mcp.client.session uses this unqualified logger instead of its module name.
    names = (
        set(prefixes)
        | {"client"}
        | {
            name
            for name in logging.Logger.manager.loggerDict
            if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
        }
    )
    previous = {name: logging.getLogger(name).level for name in names}
    try:
        for name in names:
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        yield
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


class HTTPBrokerAdapter:
    """One adapter process, with independent requests sharing only a concurrency bound."""

    def __init__(self, url: str, token: str, env_file: Path | None = None) -> None:
        if canonical_broker_port(url) is None:
            raise StdioAdapterError("stdio_adapter_invalid_url")
        self._url = url
        if not isinstance(token, str) or _KEY.fullmatch(token) is None:
            raise StdioAdapterError("stdio_adapter_invalid_key")
        self._token = token
        # The explicitly selected private env file, re-read only after the
        # broker rejects the current key. None disables reloading.
        self._env_file = env_file
        self._reloading = asyncio.Lock()
        self._slots = asyncio.Semaphore(_MAX_CONNECTIONS)
        self.instructions: str | None = None

    async def _request(
        self,
        action: Callable[[Client, float], Awaitable[_T]],
        *,
        tool_call: bool = False,
        seconds: float = _REQUEST_SECONDS,
    ) -> _T:
        """At most one reconnect, only after a provably unexecuted key rejection."""
        token = self._token
        try:
            return await self._attempt(action, token, tool_call=tool_call, seconds=seconds)
        except _KeyRejected as rejected:
            if not rejected.replay_safe:
                raise StdioAdapterError(_OUTCOME_UNKNOWN) from None
        fresh = await self._reload(token)
        try:
            return await self._attempt(action, fresh, tool_call=tool_call, seconds=seconds)
        except _KeyRejected as rejected:
            # No further reload or retry, whatever the env file now holds.
            raise StdioAdapterError(
                _KEY_REJECTED if rejected.replay_safe else _OUTCOME_UNKNOWN
            ) from None

    async def _reload(self, rejected: str) -> str:
        """Return a newer key from the env file, or raise a fixed public error."""
        async with self._reloading:
            if self._token != rejected:
                # A concurrent request already switched to the file's newer key.
                return self._token
            if self._env_file is None:
                raise StdioAdapterError(_KEY_REJECTED)
            try:
                loaded = await asyncio.to_thread(
                    client_environment.load_client_environment, self._env_file
                )
            except client_environment.ClientEnvironmentError:
                raise StdioAdapterError(_ENV_INVALID) from None
            if loaded.url != self._url:
                # Never switch brokers inside a running client session.
                raise StdioAdapterError(_URL_CHANGED)
            if loaded.token == rejected or _KEY.fullmatch(loaded.token) is None:
                raise StdioAdapterError(_KEY_REJECTED)
            self._token = loaded.token
            return loaded.token

    async def _attempt(
        self,
        action: Callable[[Client, float], Awaitable[_T]],
        token: str,
        *,
        tool_call: bool,
        seconds: float,
    ) -> _T:
        dispatched = False
        received = False
        result: _T | None = None
        rejection: str | None = None
        key_rejected: bool | None = None

        async def check_response(response: httpx2.Response) -> None:
            nonlocal rejection, key_rejected
            if response.status_code == 401 and await _is_auth_guard_rejection(response):
                rejection = _KEY_REJECTED
                # The guard answered before any MCP handler ran. Once the
                # operation was dispatched, only a rejection of a JSON-RPC
                # request proves that; identity finished before dispatch, so
                # such a request is the operation's own.
                key_rejected = (
                    not tool_call or not dispatched or _is_jsonrpc_request(response.request)
                )
            elif response.status_code in (401, 403):
                rejection = "stdio_adapter_authentication_failed"
            elif response.is_redirect:
                # Even a same-origin redirect would change the approved /mcp endpoint.
                rejection = "stdio_adapter_endpoint_rejected"
            if rejection is not None:
                raise StdioAdapterError(rejection)

        try:
            # Include queueing, negotiation, identity, operation, and teardown in
            # the deadline. At most four upstream connections may exist at once.
            async with asyncio.timeout(seconds), self._slots:
                async with httpx2.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=seconds,
                    headers={"Authorization": f"Bearer {token}"},
                    event_hooks={"response": [check_response]},
                ) as http:
                    transport = streamable_http_client(
                        self._url, http_client=http, terminate_on_close=False
                    )
                    async with Client(
                        transport, read_timeout_seconds=seconds, cache=None
                    ) as client:
                        identity = await self._call_once(
                            client,
                            CallToolRequestParams(
                                name="status", arguments={"request": {"view": "identity"}}
                            ),
                            seconds,
                        )
                        self._verify_identity(identity)
                        dispatched = True
                        result = await action(client, seconds)
                        received = True
            return result
        except Exception as exc:
            if key_rejected is not None:
                if received:
                    return cast(_T, result)
                raise _KeyRejected(key_rejected) from None
            if isinstance(exc, StdioAdapterError):
                raise
            # Transport task groups may wrap the original error. The response
            # hook records only a fixed classification, never the response body.
            if rejection is not None:
                raise StdioAdapterError(rejection) from None
            if received:
                # The complete result already arrived; a slow or failed teardown
                # of this connection does not make its outcome unknown.
                return cast(_T, result)
            fixed = self._fixed_error(exc)
            if fixed is not None:
                raise StdioAdapterError(fixed) from None
            error = (
                _OUTCOME_UNKNOWN if tool_call and dispatched else "stdio_adapter_broker_unavailable"
            )
            raise StdioAdapterError(error) from None

    @staticmethod
    def _fixed_error(error: BaseException) -> str | None:
        if isinstance(error, StdioAdapterError):
            return str(error)
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                fixed = HTTPBrokerAdapter._fixed_error(child)
                if fixed is not None:
                    return fixed
        return None

    @staticmethod
    def _verify_identity(result: CallToolResult) -> None:
        data = result.structured_content
        if (
            result.is_error
            or not isinstance(data, dict)
            or data.get("ok") is not True
            or data.get("authenticated") is not True
            or data.get("origin") != "remote_mcp"
            or not isinstance(data.get("scopes"), list)
            or "read" not in data["scopes"]
        ):
            raise StdioAdapterError("stdio_adapter_remote_read_key_required")

    @staticmethod
    async def _call_once(
        client: Client, params: CallToolRequestParams, seconds: float
    ) -> CallToolResult:
        # The high-level helper can reissue tools/call for input-required rounds.
        # The session helper can perform another tools/list after tool execution.
        # Send exactly one typed request and preserve its full result instead.
        result = await client.session.send_request(
            CallToolRequest(params=params),
            CallToolResult,
            request_read_timeout_seconds=seconds,
        )
        if result.result_type != "complete":
            raise StdioAdapterError("stdio_adapter_unsupported_tool_result")
        return result

    async def check_broker(self) -> None:
        async def ready(client: Client, seconds: float) -> None:
            self.instructions = client.instructions

        await self._request(ready)

    async def list_tools(self, params: PaginatedRequestParams | None = None) -> ListToolsResult:
        async def listing(client: Client, seconds: float) -> ListToolsResult:
            return await client.session.list_tools(params=params)

        return await self._request(listing)

    async def call_tool(self, params: CallToolRequestParams) -> CallToolResult:
        if params.task is not None or params.input_responses is not None or params.request_state:
            raise StdioAdapterError("stdio_adapter_unsupported_tool_request")

        async def calling(client: Client, seconds: float) -> CallToolResult:
            return await self._call_once(client, params, seconds)

        return await self._request(
            calling, tool_call=True, seconds=_TOOL_SECONDS.get(params.name, _REQUEST_SECONDS)
        )


def outcome_unknown_guidance(params: CallToolRequestParams) -> str:
    """Fixed next steps; echoes only the caller's own well-formed plan_hash/job_id.

    Nothing from the upstream response is included.
    """
    arguments = params.arguments if isinstance(params.arguments, dict) else {}
    request = arguments.get("request")
    fields = request if isinstance(request, dict) else arguments
    guidance: dict[str, object] = {
        "code": _OUTCOME_UNKNOWN,
        "request_may_have_been_accepted": True,
        "next_step": (
            "Do not assume failure. Read status(view=job) for the plan's job_id to see "
            "whether the job was created and how it ended. Calling flash or execute "
            "again with the same plan_hash is deduplicated: it returns the existing "
            "job and never replays it."
        ),
    }
    plan_hash = fields.get("plan_hash")
    if isinstance(plan_hash, str) and _PLAN_HASH.match(plan_hash):
        guidance["plan_hash"] = plan_hash
    job_id = fields.get("job_id")
    if isinstance(job_id, str) and _JOB_ID.match(job_id):
        guidance["job_id"] = job_id
    return json.dumps(guidance, sort_keys=True)


_KEY_GUIDANCE: dict[str, dict[str, object]] = {
    _KEY_REJECTED: {
        "client_key_rejected": True,
        "detail": (
            "The broker rejected the client key from this adapter's env file: it has "
            "expired, was revoked, or is not in the broker's key registry. The env "
            "file held no newer key, so nothing was resent."
        ),
        "next_step": (
            "Put a valid key in CARDPUTER_CLIENT_KEY of the env file this adapter was "
            "started with, then retry the call; no client restart is needed. To "
            "replace a key that is still active, run: cardputer.exe keys rotate "
            "--key-id <key-id> --write-env <env-file>. An expired or revoked key "
            "cannot be rotated: issue a new one with cardputer.exe keys issue and "
            "write it into the same env file."
        ),
    },
    _ENV_INVALID: {
        "client_key_rejected": True,
        "detail": (
            "The broker rejected the current client key, and the env file could not "
            "be re-read: it failed the same location, permission and format checks "
            "as at startup. The adapter kept its previous key and resent nothing."
        ),
        "next_step": (
            "Fix the env file (only CARDPUTER_CLIENT_KEY and optionally "
            "CARDPUTER_BROKER_URL, no quotes or spaces), then retry the call; no "
            "client restart is needed."
        ),
    },
    _URL_CHANGED: {
        "client_key_rejected": True,
        "detail": (
            "The broker rejected the current client key, and the env file now names "
            "a different CARDPUTER_BROKER_URL. A running adapter never switches "
            "brokers, so it resent nothing."
        ),
        "next_step": (
            "Restore the previous broker URL in the env file and retry, or restart "
            "the MCP client to connect to the new broker URL."
        ),
    },
}


def key_rejection_guidance(code: str) -> dict[str, object] | None:
    """Fixed next steps for a rejected key; no key, key id, path or upstream text."""
    guidance = _KEY_GUIDANCE.get(code)
    return None if guidance is None else {"code": code, **guidance}


def create_stdio_server(adapter: HTTPBrokerAdapter) -> Server[Any]:
    """Advertise only the two forwarded tool methods; all schemas stay upstream-owned."""

    async def list_tools(
        _: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        try:
            return await adapter.list_tools(params)
        except StdioAdapterError as exc:
            raise MCPError(
                code=INTERNAL_ERROR, message=str(exc), data=key_rejection_guidance(str(exc))
            ) from None

    async def call_tool(
        _: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        try:
            return await adapter.call_tool(params)
        except StdioAdapterError as exc:
            content: list[Any] = [TextContent(type="text", text=str(exc))]
            if str(exc) == _OUTCOME_UNKNOWN:
                content.append(TextContent(type="text", text=outcome_unknown_guidance(params)))
            guidance = key_rejection_guidance(str(exc))
            if guidance is not None:
                content.append(TextContent(type="text", text=json.dumps(guidance, sort_keys=True)))
            return CallToolResult(content=content, is_error=True)

    return Server(
        "device-broker-adapter",
        instructions=adapter.instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio(url: str, token: str, env_file: Path | None = None) -> None:
    """Fail closed before serving, then run the official dual-era stdio transport.

    `env_file` is the private file `url`/`token` were loaded from; it is re-read
    only after the broker rejects the key (see the module docstring).
    """
    with _quiet_dependencies():
        adapter = HTTPBrokerAdapter(url, token, env_file)
        await adapter.check_broker()
        server = create_stdio_server(adapter)
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        except Exception:
            raise StdioAdapterError("stdio_adapter_transport_failed") from None
