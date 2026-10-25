"""Tools-only stdio adapter for an already running, authenticated HTTP broker.

The adapter owns transport connections only. It never starts a broker, creates a
work session, acquires a device, releases ownership, or cancels a broker job.
Every downstream request gets a fresh, bounded HTTP connection; a failed call is
never replayed, even when its response was lost.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
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

_KEY = re.compile(r"cpk_[a-f0-9]{32}\.[a-f0-9]{64}\Z")
_URL = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{3,4})/mcp\Z")
_REQUEST_SECONDS = 30.0
# Tools that queue work verify firmware inputs before they answer; one deadline
# per request still covers connect, identity, the call and teardown.
_TOOL_SECONDS = {"flash": 120.0, "execute": 120.0, "submit": 120.0}
_OUTCOME_UNKNOWN = "stdio_adapter_tool_outcome_unknown"
_PLAN_HASH = re.compile(r"[a-f0-9]{64}\Z")
_JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MAX_CONNECTIONS = 4
_T = TypeVar("_T")


class StdioAdapterError(ValueError):
    """Fixed public error codes, never upstream exceptions or credential values."""


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

    def __init__(self, url: str, token: str) -> None:
        parsed = _URL.fullmatch(url) if isinstance(url, str) else None
        if parsed is None or not 1024 <= int(parsed[1]) <= 65535:
            raise StdioAdapterError("stdio_adapter_invalid_url")
        self._url = url
        if not isinstance(token, str) or _KEY.fullmatch(token) is None:
            raise StdioAdapterError("stdio_adapter_invalid_key")
        self._token = token
        self._slots = asyncio.Semaphore(_MAX_CONNECTIONS)
        self.instructions: str | None = None

    async def _request(
        self,
        action: Callable[[Client, float], Awaitable[_T]],
        *,
        tool_call: bool = False,
        seconds: float = _REQUEST_SECONDS,
    ) -> _T:
        dispatched = False
        received = False
        result: _T | None = None
        rejection: str | None = None

        async def check_response(response: httpx2.Response) -> None:
            nonlocal rejection
            if response.status_code in (401, 403):
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
                    headers={"Authorization": f"Bearer {self._token}"},
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
        except StdioAdapterError:
            raise
        except Exception as exc:
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


def create_stdio_server(adapter: HTTPBrokerAdapter) -> Server[Any]:
    """Advertise only the two forwarded tool methods; all schemas stay upstream-owned."""

    async def list_tools(
        _: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        try:
            return await adapter.list_tools(params)
        except StdioAdapterError as exc:
            raise MCPError(code=INTERNAL_ERROR, message=str(exc)) from None

    async def call_tool(
        _: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        try:
            return await adapter.call_tool(params)
        except StdioAdapterError as exc:
            content: list[Any] = [TextContent(type="text", text=str(exc))]
            if str(exc) == _OUTCOME_UNKNOWN:
                content.append(TextContent(type="text", text=outcome_unknown_guidance(params)))
            return CallToolResult(content=content, is_error=True)

    return Server(
        "device-broker-adapter",
        instructions=adapter.instructions,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


async def run_stdio(url: str, token: str) -> None:
    """Fail closed before serving, then run the official dual-era stdio transport."""
    with _quiet_dependencies():
        adapter = HTTPBrokerAdapter(url, token)
        await adapter.check_broker()
        server = create_stdio_server(adapter)
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        except Exception:
            raise StdioAdapterError("stdio_adapter_transport_failed") from None
