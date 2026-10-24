"""Sanitized denial observation only; synthetic credentials and mock hardware."""

import json
from unittest.mock import Mock

import httpx2
import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from test_authenticated_mock import authenticated_mock as authenticated_mock

from cardputer_mcp.backend import MockBackend
from cardputer_mcp.broker import Broker
from cardputer_mcp.client import connect
from cardputer_mcp.http_diagnostics import DenialReason, HTTPDiagnostics
from cardputer_mcp.models import Origin
from cardputer_mcp.operator_api import OperatorAPI
from cardputer_mcp.server import LoopbackOnly, Principal

LOCAL = "a" * 64
REMOTE = "b" * 64
MARKER = "SYNTHETIC_PRIVATE_INPUT_NEVER_RETAIN"
BASE_HEADERS = [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json")]


async def invoke(
    app: ASGIApp,
    *,
    path: str = "/mcp",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 12345),
    method: str = "POST",
    query: bytes = b"",
) -> list[Message]:
    messages: list[Message] = []

    async def receive() -> Message:
        pytest.fail("Denial diagnostics must not read the request body")

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "method": method,
        "path": path,
        "client": client,
        "headers": list(BASE_HEADERS) if headers is None else headers,
        "query_string": query,
    }
    await app(scope, receive, send)
    return messages


async def should_not_run(scope: Scope, receive: Receive, send: Send) -> None:
    pytest.fail("Denied request reached the downstream application")


def assert_one_denial(
    diagnostics: HTTPDiagnostics, reason: DenialReason, route: str, status: int, layer: str
) -> None:
    snapshot = diagnostics.snapshot()
    snapshot.pop("context_version")
    snapshot.pop("recent_denials")
    if snapshot["last_denial"] is not None:
        snapshot["last_denial"].pop("context", None)
    assert snapshot == {
        "schema_version": 1,
        "since": "broker_start",
        "total_denials": 1,
        "counts": {item.value: int(item == reason) for item in DenialReason},
        "last_denial": {
            "reason": reason.value,
            "layer": layer,
            "route": route,
            "http_status": status,
        },
    }


@pytest.mark.parametrize(
    "headers,client,reason",
    [
        (
            [(b"host", b"bad"), (b"HOST", b"bad"), (b"x-private", b"\xff")],
            None,
            DenialReason.NON_ASCII_HEADERS,
        ),
        (
            [(b"host", b"bad"), (b"HOST", b"bad")],
            None,
            DenialReason.DUPLICATE_HEADERS,
        ),
        ([(b"host", b"bad")], None, DenialReason.NON_LOOPBACK_PEER),
        (
            [(b"host", b"bad"), (b"origin", b"bad"), (b"forwarded", b"bad")],
            ("127.0.0.1", 1),
            DenialReason.HOST_NOT_ALLOWED,
        ),
        (
            [(b"host", b"localhost:8765"), (b"origin", b"bad"), (b"forwarded", b"bad")],
            ("127.0.0.1", 1),
            DenialReason.ORIGIN_NOT_ALLOWED,
        ),
        (
            [(b"host", b"localhost:8765"), (b"forwarded", b"bad")],
            ("127.0.0.1", 1),
            DenialReason.FORWARDING_HEADER_PRESENT,
        ),
        (
            [(b"host", b"localhost:8765"), (b"content-type", b"text/plain")],
            ("127.0.0.1", 1),
            DenialReason.CONTENT_TYPE_NOT_JSON,
        ),
    ],
)
async def test_guard_records_first_rejection_without_changing_response_or_reading_body(
    headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None, reason: DenialReason
) -> None:
    diagnostics = HTTPDiagnostics()
    authenticate = Mock(side_effect=AssertionError("Authentication followed a guard denial"))
    with_diagnostics = LoopbackOnly(should_not_run, 8765, authenticate, diagnostics)
    without_diagnostics = LoopbackOnly(should_not_run, 8765, authenticate)
    observed = await invoke(with_diagnostics, headers=headers, client=client)
    assert observed == await invoke(without_diagnostics, headers=headers, client=client)
    assert observed == [
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        },
        {"type": "http.response.body", "body": b'{"error":"local_only"}'},
    ]
    authenticate.assert_not_called()
    assert_one_denial(diagnostics, reason, "mcp", 403, "guard")


@pytest.mark.parametrize(
    "path,reason,route",
    [("/operator/" + MARKER, DenialReason.OPERATOR_HTTP_REJECTION, "operator")],
)
@pytest.mark.parametrize("status", [403])
async def test_downstream_errors_are_categorized_and_response_redaction_is_unchanged(
    path: str, reason: DenialReason, route: str, status: int
) -> None:
    async def downstream(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"x-private", MARKER.encode()), (b"content-length", b"999")],
            }
        )
        await send({"type": "http.response.body", "body": MARKER.encode(), "more_body": True})
        await send({"type": "http.response.body", "body": MARKER.encode(), "more_body": False})

    diagnostics = HTTPDiagnostics()
    observed = await invoke(LoopbackOnly(downstream, 8765, diagnostics=diagnostics), path=path)
    assert observed == await invoke(LoopbackOnly(downstream, 8765), path=path)
    assert observed == [
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json")],
        },
        {"type": "http.response.body", "body": b'{"error":"request_rejected"}', "more_body": False},
    ]
    assert_one_denial(diagnostics, reason, route, status, route)
    assert MARKER not in repr(observed) + json.dumps(diagnostics.snapshot())


@pytest.mark.parametrize(
    "principal,status",
    [
        (None, 401),
        (Principal(MARKER, Origin.REMOTE, frozenset({"read", "control"})), 403),
        (Principal(MARKER, Origin.LOCAL, frozenset({"read"})), 403),
        (Principal(MARKER, Origin.LOCAL, frozenset({"control"})), 200),
    ],
)
async def test_operator_diagnostics_require_local_control_without_using_broker(
    principal: Principal | None, status: int
) -> None:
    broker = Mock(spec=Broker)
    diagnostics = HTTPDiagnostics()
    snapshot = Mock(side_effect=diagnostics.snapshot)
    operator = OperatorAPI(should_not_run, broker, lambda _: principal, diagnostics=snapshot)
    messages = await invoke(
        LoopbackOnly(operator, 8765, diagnostics=diagnostics),
        path="/operator/http-diagnostics",
        method="GET",
        headers=BASE_HEADERS + [(b"authorization", MARKER.encode())],
    )
    assert messages[0]["status"] == status
    if status == 200:
        assert json.loads(messages[1]["body"]) == {"ok": True, **diagnostics.snapshot()}
        assert (b"cache-control", b"no-store") in messages[0]["headers"]
        snapshot.assert_called_once_with()
    else:
        assert messages[1]["body"] == b'{"error":"request_rejected"}'
        snapshot.assert_not_called()
        assert_one_denial(
            diagnostics, DenialReason.OPERATOR_HTTP_REJECTION, "operator", status, "operator"
        )
    assert broker.mock_calls == []
    assert MARKER not in repr(messages) + json.dumps(diagnostics.snapshot())


@pytest.mark.parametrize(
    "path,method,query,headers,client,status,reason",
    [
        (
            "/operator/http-diagnostics/",
            "GET",
            b"",
            BASE_HEADERS,
            ("127.0.0.1", 1),
            404,
            DenialReason.OPERATOR_HTTP_REJECTION,
        ),
        (
            "/operator/http-diagnostics",
            "POST",
            b"",
            BASE_HEADERS,
            ("127.0.0.1", 1),
            405,
            DenialReason.OPERATOR_HTTP_REJECTION,
        ),
        (
            "/operator/http-diagnostics",
            "GET",
            MARKER.encode(),
            BASE_HEADERS,
            ("127.0.0.1", 1),
            400,
            DenialReason.OPERATOR_HTTP_REJECTION,
        ),
    ],
)
async def test_operator_snapshot_obeys_exact_route_and_existing_guards(
    path: str,
    method: str,
    query: bytes,
    headers: list[tuple[bytes, bytes]],
    client: tuple[str, int],
    status: int,
    reason: DenialReason,
) -> None:
    broker = Mock(spec=Broker)
    diagnostics = HTTPDiagnostics()
    snapshot = Mock(side_effect=diagnostics.snapshot)
    principal = Principal(MARKER, Origin.LOCAL, frozenset({"read", "control"}))
    operator = OperatorAPI(should_not_run, broker, lambda _: principal, diagnostics=snapshot)
    messages = await invoke(
        LoopbackOnly(operator, 8765, diagnostics=diagnostics),
        path=path,
        method=method,
        query=query,
        headers=headers,
        client=client,
    )
    assert messages[0]["status"] == status
    snapshot.assert_not_called()
    assert broker.mock_calls == []
    assert_one_denial(diagnostics, reason, "operator", status, "operator")
    assert MARKER not in repr(messages) + json.dumps(diagnostics.snapshot())


async def test_authenticated_mock_http_exposes_sanitized_snapshot_only_to_local_operator(
    authenticated_mock: tuple[str, Broker],
) -> None:
    url, resident = authenticated_mock
    assert isinstance(resident.backend, MockBackend)
    endpoint = url.removesuffix("/mcp") + "/operator/http-diagnostics"
    async with httpx2.AsyncClient(trust_env=False) as http:
        assert (await http.get(endpoint)).status_code == 401
        assert (
            await http.get(endpoint, headers={"Authorization": f"Bearer {REMOTE}"})
        ).status_code == 403
        rejection = await http.post(
            url + "?private=" + MARKER,
            content=json.dumps({"private": MARKER}),
            headers={
                "Authorization": f"Bearer {LOCAL}",
                "Origin": "https://" + MARKER + ".invalid",
                "Content-Type": "application/json",
            },
        )
        assert rejection.status_code == 403
        assert rejection.json() == {"error": "local_only"}
        response = await http.get(endpoint, headers={"Authorization": f"Bearer {LOCAL}"})
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["ok"] is True
        assert snapshot["total_denials"] == 3
        assert snapshot["counts"] == {
            reason.value: int(
                reason
                in {
                    DenialReason.AUTHENTICATION_REQUIRED,
                    DenialReason.OPERATOR_HTTP_REJECTION,
                    DenialReason.ORIGIN_NOT_ALLOWED,
                }
            )
            for reason in DenialReason
        }
        assert snapshot["last_denial"] == {
            "reason": "origin_not_allowed",
            "layer": "guard",
            "route": "mcp",
            "http_status": 403,
            "context": {
                "entry": "direct_mcp",
                "stage": "direct_guard",
                "method": "POST",
                "content_type": "application_json",
                "forwarding_headers": [],
                "duplicate_headers": [],
            },
        }
        assert response.headers["cache-control"] == "no-store"
        for private in (LOCAL, REMOTE, MARKER, "ephemeral:", str(resident.settings.state_dir)):
            assert private not in response.text
    async with connect(url, LOCAL) as local:
        names = {tool.name for tool in (await local.list_tools()).tools}
        assert not any("http" in name or "operator" in name for name in names)
    assert resident.session is None
    assert resident.backend.calls == 0
    assert resident.backend.active == 0
    assert resident.backend.max_active == 0
    assert resident.backend.hardware_connected is False
