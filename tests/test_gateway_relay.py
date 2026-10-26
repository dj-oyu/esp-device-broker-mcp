"""Claude gateway relay: only MCP, through the broker's real /tunnel/mcp guards."""

import base64
import hashlib
import json
import secrets
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from starlette.responses import JSONResponse
from test_gateway_oauth import PUBLIC, SETTINGS, FakeGoogle

from cardputer_mcp import client_environment
from cardputer_mcp import gateway_oauth as gw
from cardputer_mcp.gateway_relay import MAX_BODY, BrokerRelay, RelayConfigError
from cardputer_mcp.models import Origin
from cardputer_mcp.server import LoopbackOnly, TunnelIngress

BROKER_URL = "http://127.0.0.1:8765/mcp"
KEY1 = "cpk_" + "1" * 32 + "." + "a" * 64
KEY2 = "cpk_" + "2" * 32 + "." + "b" * 64


class Broker:
    """The broker's real ingress guards in front of a recording MCP app."""

    def __init__(self, valid=(KEY1,), inner_status=200):
        self.valid = set(valid)
        self.seen: list[dict] = []
        self.inner_status = inner_status

        async def inner(scope, receive, send):
            body = b""
            while True:
                message = await receive()
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            self.seen.append(
                {
                    "path": scope["path"],
                    "method": scope["method"],
                    "headers": {k.decode(): v.decode() for k, v in scope["headers"]},
                    "body": body,
                }
            )
            response = JSONResponse(
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                status_code=self.inner_status,
                headers={"mcp-session-id": "s-1", "set-cookie": "leak=1", "server": "x"},
            )
            await response(scope, receive, send)

        def authenticate(header):
            if header is None or not header.startswith("Bearer "):
                return None
            if header.removeprefix("Bearer ") not in self.valid:
                return None
            return SimpleNamespace(origin=Origin.REMOTE, scopes=frozenset({"read", "control"}))

        self.app = TunnelIngress(LoopbackOnly(inner, 8765), authenticate)

    def http(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app, client=("127.0.0.1", 40000))
        )


def _relay(broker, token=KEY1, env_file=None, load=None, http=None):
    kwargs = {"load": load} if load else {}
    return BrokerRelay(BROKER_URL, token, env_file, http=http or broker.http(), **kwargs)


async def _call(relay, method="POST", path="/mcp", headers=None, content=b'{"jsonrpc":"2.0"}'):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=relay), base_url=PUBLIC
    ) as client:
        if headers is None:
            headers = {"content-type": "application/json"}
        return await client.request(method, path, headers=headers, content=content)


async def test_relayed_request_passes_broker_guards_with_only_mcp_headers():
    broker = Broker()
    response = await _call(
        _relay(broker),
        path="/mcp?x=1",
        headers={
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-session-id": "s-1",
            "mcp-protocol-version": "2025-11-25",
            "authorization": "Bearer claude-token",
            "cookie": "a=b",
            "origin": "https://claude.ai",
            "x-forwarded-for": "160.79.104.1",
            "forwarded": "for=1.2.3.4",
            "tailscale-user-login": "someone",
        },
    )
    assert response.status_code == 200
    assert response.json()["jsonrpc"] == "2.0"
    # Only MCP response headers come back.
    assert response.headers["mcp-session-id"] == "s-1"
    assert "set-cookie" not in response.headers and "server" not in response.headers
    [seen] = broker.seen
    assert seen["path"] == "/mcp" and seen["body"] == b'{"jsonrpc":"2.0"}'
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer " + KEY1
    assert headers["host"] == "127.0.0.1:8765"
    assert headers["mcp-session-id"] == "s-1"
    assert headers["mcp-protocol-version"] == "2025-11-25"
    for absent in ("cookie", "origin", "x-forwarded-for", "forwarded", "tailscale-user-login"):
        assert absent not in headers


async def test_rotated_broker_key_is_reloaded_once_after_guard_rejection(tmp_path):
    broker = Broker(valid=(KEY2,))
    loads = []

    def load(path):
        loads.append(path)
        return client_environment.ClientEnvironment(url=BROKER_URL, token=KEY2)

    relay = _relay(broker, env_file=tmp_path / "env", load=load)
    assert (await _call(relay)).status_code == 200
    assert (await _call(relay)).status_code == 200
    assert len(loads) == 1  # the second request already uses the new key
    assert [s["headers"]["authorization"] for s in broker.seen] == ["Bearer " + KEY2] * 2


@pytest.mark.parametrize(
    "loaded",
    [
        client_environment.ClientEnvironment(url=BROKER_URL, token=KEY1),
        client_environment.ClientEnvironment(url="http://127.0.0.1:9999/mcp", token=KEY2),
        None,
    ],
)
async def test_rejected_key_is_502_never_401_and_retried_at_most_once(tmp_path, loaded):
    broker = Broker(valid=())
    calls = []

    def load(_path):
        calls.append(1)
        if loaded is None:
            raise client_environment.ClientEnvironmentError()
        return loaded

    response = await _call(_relay(broker, env_file=tmp_path / "env", load=load))
    assert response.status_code == 502
    assert response.json() == {"error": "broker_key_rejected"}
    assert len(calls) == 1 and broker.seen == []


async def test_without_env_file_rejected_key_is_not_retried():
    broker = Broker(valid=())
    response = await _call(_relay(broker))
    assert response.status_code == 502 and broker.seen == []


async def test_other_broker_errors_pass_through_without_retry():
    broker = Broker(inner_status=401)
    response = await _call(_relay(broker))
    # The tunnel guard sanitizes the inner 401; it is not the auth-guard body, so no reload.
    assert response.status_code == 401
    assert len(broker.seen) == 1


async def test_unreachable_broker_is_502():
    def refuse(request):
        raise httpx2.ConnectError("refused", request=request)

    relay = _relay(Broker(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(refuse)))
    response = await _call(relay)
    assert response.status_code == 502 and response.json() == {"error": "broker_unavailable"}


async def test_oversized_request_is_refused_before_relay():
    broker = Broker()
    response = await _call(
        _relay(broker),
        headers={"content-type": "application/json"},
        content=b"x" * (MAX_BODY + 1),
    )
    assert response.status_code == 413 and broker.seen == []


async def test_get_and_delete_are_relayed_put_is_not():
    broker = Broker()
    relay = _relay(broker)
    get = await _call(relay, "GET", headers={"last-event-id": "7"}, content=None)
    assert get.status_code == 200
    assert broker.seen[-1]["headers"]["last-event-id"] == "7"
    assert (await _call(relay, "DELETE", content=None)).status_code == 200
    assert (await _call(relay, "PUT")).status_code == 405
    assert [s["method"] for s in broker.seen] == ["GET", "DELETE"]


def test_relay_refuses_non_canonical_broker_url_and_bad_key():
    for url in ("http://localhost:8765/mcp", "http://127.0.0.1:8765/operator", "https://x/mcp"):
        with pytest.raises(RelayConfigError):
            BrokerRelay(url, KEY1)
    with pytest.raises(RelayConfigError):
        BrokerRelay(BROKER_URL, "not-a-key")


async def test_claude_token_reaches_broker_only_as_gateway_key():
    """End to end: Claude OAuth token at the gateway, broker key at the broker."""
    google = FakeGoogle()
    broker = Broker()
    google_http = httpx2.AsyncClient(transport=httpx2.MockTransport(google.handler))
    app, _provider = gw.create_gateway_app(
        SETTINGS, "google-secret", http=google_http, mcp_app=_relay(broker)
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url=PUBLIC
    ) as client:
        assert (await client.post("/mcp", json={})).status_code == 401
        assert broker.seen == []
        reg = (
            await client.post("/register", json={"redirect_uris": [gw.CLAUDE_REDIRECT_URI]})
        ).json()
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        start = await client.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": reg["client_id"],
                "redirect_uri": gw.CLAUDE_REDIRECT_URI,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "s",
                "resource": PUBLIC + "/mcp",
            },
        )
        code, state = google.approve(start.headers["location"])
        callback = await client.get(gw.GOOGLE_CALLBACK_PATH, params={"code": code, "state": state})
        auth_code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
        tokens = (
            await client.post(
                "/token",
                data={
                    "grant_type": "authorization_code",
                    "code": auth_code,
                    "code_verifier": verifier,
                    "redirect_uri": gw.CLAUDE_REDIRECT_URI,
                    "client_id": reg["client_id"],
                    "client_secret": reg["client_secret"],
                },
            )
        ).json()
        response = await client.post(
            "/mcp",
            content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={
                "authorization": "Bearer " + tokens["access_token"],
                "content-type": "application/json",
            },
        )
    await google_http.aclose()
    assert response.status_code == 200
    [seen] = broker.seen
    assert seen["headers"]["authorization"] == "Bearer " + KEY1
    assert tokens["access_token"] not in json.dumps(seen["headers"])


async def test_client_disconnect_closes_an_endless_broker_stream():
    import anyio

    closed = anyio.Event()
    sent_chunks = []

    class Endless(httpx2.AsyncByteStream):
        async def __aiter__(self):
            n = 0
            while True:
                n += 1
                yield f"event: message\ndata: {n}\n\n".encode()
                await anyio.sleep(0.01)

        async def aclose(self):
            closed.set()

    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Endless())

    relay = _relay(Broker(), http=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    requested = False
    first_chunk = anyio.Event()

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await first_chunk.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            sent_chunks.append(message["body"])
            first_chunk.set()

    scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": []}
    with anyio.fail_after(5):
        await relay(scope, receive, send)
        await closed.wait()
    assert sent_chunks and sent_chunks[0].startswith(b"event: message")
