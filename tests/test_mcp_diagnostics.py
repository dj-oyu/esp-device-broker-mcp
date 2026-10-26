"""GUI diagnostics use synthetic credentials and mock-only loopback requests."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from test_registry_http import registry_service as registry_service

from cardputer_mcp import mcp_diagnostics as diagnostic
from cardputer_mcp.mcp_diagnostics import MCPDiagnosticsError, parse_summary, probe
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError


def summary():
    return {
        "gui_version": "0.13.1",
        "server_version": "0.7.0",
        "tools_count": 17,
        "read_capture": True,
        "capture_home": True,
        "broker_epoch": "a" * 32,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("gui_version", "SYNTHETIC_PRIVATE_PATH"),
        ("server_version", "x" * 1024),
        ("tools_count", -1),
        ("unexpected", "SYNTHETIC_PRIVATE_TOKEN"),
    ],
)
def test_only_bounded_allowlisted_summary(field, value):
    assert parse_summary(summary()) == summary()
    data = summary() | {field: value}
    with pytest.raises(MCPDiagnosticsError, match="^invalid_mcp_summary$"):
        parse_summary(data)


async def test_real_http_probe_uses_existing_operator_identity_and_does_no_work(registry_service):
    service = registry_service
    api = OperatorClient(service.url, "a" * 64)
    result = await asyncio.to_thread(api.catalog)
    assert result == summary() | {
        "server_version": "0.13.1",
        "tools_count": 6,
        "broker_epoch": service.resident.epoch,
    }
    assert service.backend.calls == 0
    assert service.resident.session is None and service.resident.queue.empty()
    assert service.resident._store().record_count("jobs") == 0
    assert service.resident._store().record_count("plans") == 0
    assert "a" * 64 not in str(result)


def test_total_timeout_also_covers_context_cleanup(monkeypatch):
    catalog = SimpleNamespace(next_cursor=None, tools=[])

    class Client:
        server_info = SimpleNamespace(version="0.5.4")

        async def list_tools(self):
            return catalog

    @asynccontextmanager
    async def connect(url, token):
        yield Client()
        await asyncio.Event().wait()

    async def call(client):
        return {"ok": True, "broker_epoch": "b" * 32}

    monkeypatch.setattr(diagnostic, "connect", connect)
    monkeypatch.setattr(diagnostic, "_status", call)
    monkeypatch.setattr(diagnostic, "TOTAL_SECONDS", 0.02)
    with pytest.raises(MCPDiagnosticsError, match="^live_probe_timed_out$"):
        probe("http://127.0.0.1:8765/mcp", "a" * 64)


def test_old_catalog_and_unknown_version_are_reported_without_schema_echo(monkeypatch):
    class Client:
        server_info = SimpleNamespace(version="SYNTHETIC_PRIVATE_VERSION")

        async def list_tools(self):
            return SimpleNamespace(
                next_cursor=None,
                tools=[
                    SimpleNamespace(
                        name="submit",
                        input_schema={
                            "$defs": {
                                "Kind": {"enum": ["diagnostics", "hello"]},
                            }
                        },
                    ),
                    SimpleNamespace(name="SYNTHETIC_PRIVATE_TOOL", input_schema={}),
                ],
            )

    @asynccontextmanager
    async def connect(url, token):
        yield Client()

    async def call(client):
        return {"ok": True, "broker_epoch": "b" * 32, "private": "SYNTHETIC_PRIVATE_DATA"}

    monkeypatch.setattr(diagnostic, "connect", connect)
    monkeypatch.setattr(diagnostic, "_status", call)
    assert probe("http://127.0.0.1:8765/mcp", "a" * 64) == {
        "gui_version": "0.13.1",
        "server_version": "unknown",
        "tools_count": 2,
        "read_capture": False,
        "capture_home": False,
        "broker_epoch": "b" * 32,
    }


@pytest.mark.parametrize("logger", ["client", "httpx2"])
def test_dependency_logging_and_exception_payloads_are_suppressed(monkeypatch, caplog, logger):
    secret = "SYNTHETIC_PRIVATE_PAYLOAD"
    previous = logging.getLogger(logger).level

    @asynccontextmanager
    async def connect(url, token):
        logging.getLogger(logger).critical(secret)
        raise ValueError(secret)
        yield

    monkeypatch.setattr(diagnostic, "connect", connect)
    with pytest.raises(MCPDiagnosticsError, match="^live_probe_failed$"):
        probe("http://127.0.0.1:8765/mcp", "a" * 64)
    assert secret not in caplog.text
    assert logging.getLogger(logger).level == previous


def test_operator_surface_never_reflects_a_probe_error(monkeypatch):
    def failing(url, token):
        raise MCPDiagnosticsError("SYNTHETIC_PRIVATE_PAYLOAD")

    monkeypatch.setattr(diagnostic, "probe", failing)
    with pytest.raises(OperatorClientError) as error:
        OperatorClient("http://127.0.0.1:8765/mcp", "a" * 64).catalog()
    assert str(error.value) == "MCP catalog probe failed. Check the existing broker connection."


async def test_transport_disables_delete_redirects_proxies_and_uses_initialize(monkeypatch):
    import httpx2

    recorded = {}

    @asynccontextmanager
    async def http(**kwargs):
        recorded.update(kwargs)
        yield object()

    def transport(url, **kwargs):
        recorded["transport"] = kwargs
        return object()

    @asynccontextmanager
    async def sdk(wire, **kwargs):
        recorded["sdk"] = kwargs
        yield object()

    monkeypatch.setattr(diagnostic.httpx2, "AsyncClient", http)
    monkeypatch.setattr(diagnostic, "streamable_http_client", transport)
    monkeypatch.setattr(diagnostic, "Client", sdk)
    async with diagnostic.connect("http://127.0.0.1:8765/mcp", "a" * 64):
        assert recorded["trust_env"] is False
        assert recorded["follow_redirects"] is False
        assert recorded["transport"]["terminate_on_close"] is False
        assert recorded["sdk"] == {"mode": "legacy", "read_timeout_seconds": 5, "cache": None}
        hook = recorded["event_hooks"]["response"][0]
        with pytest.raises(MCPDiagnosticsError, match="^redirect_rejected$"):
            await hook(httpx2.Response(307, headers={"Location": "/alternate"}))
        await hook(httpx2.Response(200))


@pytest.mark.parametrize("result_type", ["complete", "input_required"])
async def test_status_sends_exactly_one_typed_read_request(result_type):
    calls = []

    async def send(request, result_class, **kwargs):
        calls.append(request)
        return SimpleNamespace(
            is_error=False, result_type=result_type, structured_content={"ok": True}
        )

    client = SimpleNamespace(session=SimpleNamespace(send_request=send))
    if result_type == "complete":
        assert await diagnostic._status(client) == {"ok": True}
    else:
        with pytest.raises(MCPDiagnosticsError, match="^status_unavailable$"):
            await diagnostic._status(client)
    assert len(calls) == 1
    assert calls[0].params.name == "status"
    assert calls[0].params.arguments == {"request": {"view": "device"}}
