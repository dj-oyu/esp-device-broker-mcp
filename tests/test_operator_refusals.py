"""Real operator HTTP clients retain only fixed refusal diagnostics."""

import asyncio
import json

import httpx2
import pytest

from cardputer_mcp.gui_live_transport import AUTH_FAILED, LiveRunner, _TransportFailure
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError, safe_refusal

TOKEN = "synthetic-private-token"
URL = "http://127.0.0.1:8765/mcp"
CODES = (
    "not_authorized",
    "unavailable",
    "expired",
    "unsupported",
    "authentication_required",
    "local_operator_required",
    "invalid_request",
)


@pytest.mark.parametrize("code", CODES)
def test_safe_refusal_keeps_fixed_code_only(code):
    assert safe_refusal({"ok": False, "error": code, "detail": TOKEN}) == {
        "ok": False,
        "error": code,
    }


@pytest.mark.parametrize("value", [TOKEN, "not_authorized " + TOKEN, {}, [], 1, None])
def test_unknown_refusal_is_generic(value):
    assert safe_refusal({"ok": False, "error": value, "body": TOKEN}) == {"ok": False}
    assert safe_refusal({"ok": True, "error": "not_authorized"}) == {"ok": False}


@pytest.mark.parametrize("code", [*CODES, TOKEN])
def test_sync_client_preserves_fixed_refusal_without_server_fields(monkeypatch, code):
    original = httpx2.Client
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json={"ok": False, "error": code, "detail": TOKEN})

    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx2.MockTransport(handler)),
    )
    result = OperatorClient(URL, TOKEN)._request(
        "POST", "/operator/job-logs", {"job_id": "job"}, allow_refusal=True
    )
    assert result == ({"ok": False, "error": code} if code in CODES else {"ok": False})
    assert len(requests) == 1 and requests[0].url.path == "/operator/job-logs"
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "status,code", [(401, "authentication_required"), (403, "local_operator_required")]
)
def test_sync_http_denial_uses_status_without_parsing_body(monkeypatch, status, code):
    original = httpx2.Client
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(status, content=("not JSON " + TOKEN).encode())

    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda **kwargs: original(**kwargs, transport=httpx2.MockTransport(handler)),
    )
    client = OperatorClient(URL, TOKEN)
    assert client._request("GET", "/operator/images", allow_refusal=True) == {
        "ok": False,
        "error": code,
    }
    with pytest.raises(OperatorClientError, match="authentication failed"):
        client.state()
    assert len(requests) == 2  # One explicit read each; no retry.


@pytest.mark.parametrize("code", [*CODES, TOKEN])
def test_live_read_once_filters_refusals_without_retry(monkeypatch, code):
    original = httpx2.AsyncClient
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx2.Response(200, json={"ok": False, "error": code, "detail": TOKEN})

    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx2.MockTransport(handler)),
    )
    runner = LiveRunner(OperatorClient(URL, TOKEN))
    try:
        result = asyncio.run_coroutine_threadsafe(
            runner._request("/operator/job-logs", {"job_id": "job"}, read_once=True), runner._loop
        ).result(timeout=2)
        assert result == ({"ok": False, "error": code} if code in CODES else {"ok": False})
        assert TOKEN not in json.dumps(result) and len(requests) == 1
        assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
        assert requests[0].url.path == "/operator/job-logs"
    finally:
        runner.close()
        assert not runner._thread.is_alive()


@pytest.mark.parametrize(
    "status,code", [(401, "authentication_required"), (403, "local_operator_required")]
)
def test_live_read_once_http_denial_ignores_body_and_keeps_global_auth_failure(
    monkeypatch, status, code
):
    class UnreadBody(httpx2.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("HTTP denial body must not be read or parsed")
            yield b""

        async def aclose(self):
            # Cleanup must not overwrite an established fixed denial reason.
            raise httpx2.ReadError(TOKEN)

    original = httpx2.AsyncClient
    requests = []

    async def handler(request):
        requests.append(request)
        return httpx2.Response(status, stream=UnreadBody())

    monkeypatch.setattr(
        httpx2,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx2.MockTransport(handler)),
    )
    runner = LiveRunner(OperatorClient(URL, TOKEN))
    try:
        result = asyncio.run_coroutine_threadsafe(
            runner._request("/operator/image", {"job_id": "job"}, read_once=True), runner._loop
        ).result(timeout=2)
        assert result == {"ok": False, "error": code} and len(requests) == 1
        future = asyncio.run_coroutine_threadsafe(
            runner._request("/operator/live", {"cursor": None}), runner._loop
        )
        with pytest.raises(_TransportFailure) as failure:
            future.result(timeout=2)
        assert str(failure.value) == AUTH_FAILED
        assert failure.value.reason == "authentication_failed"
        assert failure.value.status == status and len(requests) == 2
        assert TOKEN not in str(failure.value)
    finally:
        runner.close()
        assert not runner._thread.is_alive()
