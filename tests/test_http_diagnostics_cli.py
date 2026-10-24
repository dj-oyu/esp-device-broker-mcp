"""Safe local HTTP diagnostics client and CLI; all credentials are synthetic."""

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import httpx2
import pytest

from cardputer_mcp import cli
from cardputer_mcp.gui import (
    http_diagnostics_summary,
)
from cardputer_mcp.http_diagnostics import DenialReason
from cardputer_mcp.operator_client import (
    OperatorClient,
    OperatorClientError,
    parse_http_diagnostics,
)

TOKEN = "a" * 64


def snapshot() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": 1,
        "since": "broker_start",
        "total_denials": 0,
        "counts": {reason.value: 0 for reason in DenialReason},
        "last_denial": None,
    }


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> list[dict[str, Any]]:
    original = httpx2.Client
    options: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> httpx2.Client:
        options.append(kwargs)
        return original(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx2, "Client", factory)
    return options


def test_diagnostics_uses_local_get_header_auth_and_no_cookie_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=snapshot(), headers={"Set-Cookie": "tracking=secret"})

    options = install_transport(monkeypatch, handler)
    client = OperatorClient("http://127.0.0.1:9988/mcp", TOKEN)
    assert client.diagnostics() == snapshot()
    assert client.diagnostics() == snapshot()
    assert len(options) == len(requests) == 2
    for config, request in zip(options, requests, strict=True):
        assert config["trust_env"] is False
        assert config["follow_redirects"] is False
        assert config["timeout"].connect == 2
        assert config["timeout"].read == 5
        assert request.method == "GET"
        assert str(request.url) == "http://127.0.0.1:9988/operator/http-diagnostics"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        assert "cookie" not in request.headers
        assert request.content == b""
        assert TOKEN not in str(request.url)


@pytest.mark.parametrize("flag", ["--token", "--key", "--local-token"])
def test_parser_has_no_diagnostics_secret_argument(flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.parser().parse_args(["http-diagnostics", flag, "synthetic-secret"])
    assert error.value.code == 2


def test_cli_reads_only_resident_diagnostics_and_uses_selected_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={**snapshot(), "debug": "synthetic-response-secret"})

    install_transport(monkeypatch, handler)
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", TOKEN)
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", "b" * 64)
    unused = {
        name: Mock(side_effect=AssertionError(f"Unexpected {name}"))
        for name in ("load_settings", "create_app", "manage_keys", "inventory_json", "run_client")
    }
    for name, method in unused.items():
        monkeypatch.setattr(cli, name, method)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cardputer",
            "--url",
            "http://127.0.0.1:9988/mcp",
            "--auth-source",
            "environment",
            "http-diagnostics",
        ],
    )
    cli.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == snapshot()
    assert captured.err == ""
    assert TOKEN not in captured.out
    assert "synthetic-response-secret" not in captured.out
    assert len(requests) == 1
    assert str(requests[0].url) == "http://127.0.0.1:9988/operator/http-diagnostics"
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"
    for method in unused.values():
        method.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--auth-source", "file", "--auth-file", "must-not-read.json"],
        ["--auth-source", "environment", "--client", "remote_mcp"],
    ],
)
def test_cli_requires_local_environment_credentials_before_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: list[str],
) -> None:
    client = Mock()
    environment = Mock()
    file_loader = Mock()
    monkeypatch.setattr(cli, "OperatorClient", client)
    monkeypatch.setattr(cli, "load_environment_token", environment)
    monkeypatch.setattr(cli, "load_client_token", file_loader)
    monkeypatch.setattr("sys.argv", ["cardputer", *options, "http-diagnostics"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "Request failed: http_diagnostics_requires_local_environment_credentials\n"
    )
    client.assert_not_called()
    environment.assert_not_called()
    file_loader.assert_not_called()


def test_cli_missing_local_token_does_not_fall_back_to_remote_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = Mock()
    monkeypatch.setattr(cli, "OperatorClient", client)
    monkeypatch.delenv("CARDPUTER_LOCAL_TOKEN", raising=False)
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", "b" * 64)
    monkeypatch.setattr(
        "sys.argv", ["cardputer", "--auth-source", "environment", "http-diagnostics"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Request failed: runtime_environment_credentials_unavailable\n"
    client.assert_not_called()


@pytest.mark.parametrize("status", [302, 401])
def test_cli_http_failure_does_not_echo_bodies_headers_or_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: int
) -> None:
    install_transport(
        monkeypatch,
        lambda _: httpx2.Response(
            status,
            text="synthetic-response-secret",
            headers={"Location": "https://example.invalid/synthetic-location-secret"},
        ),
    )
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", TOKEN)
    monkeypatch.setattr(
        "sys.argv", ["cardputer", "--auth-source", "environment", "http-diagnostics"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Request failed: ")
    for secret in (TOKEN, "synthetic-response-secret", "synthetic-location-secret"):
        assert secret not in captured.err


def test_cli_rejects_remote_destination_before_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    http = Mock()
    monkeypatch.setattr(httpx2, "Client", http)
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", TOKEN)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cardputer",
            "--url",
            "https://example.invalid/mcp",
            "--auth-source",
            "environment",
            "http-diagnostics",
        ],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert capsys.readouterr().err == (
        "Request failed: Use a local http://127.0.0.1:PORT broker URL.\n"
    )
    http.assert_not_called()


def denied_snapshot() -> dict[str, Any]:
    data = snapshot()
    data["total_denials"] = 1
    data["counts"]["host_not_allowed"] = 1
    data["last_denial"] = {
        "reason": "host_not_allowed",
        "layer": "guard",
        "route": "mcp",
        "http_status": 403,
    }
    return data


def context_snapshot() -> dict[str, Any]:
    data = denied_snapshot()
    data["context_version"] = 1
    data["last_denial"]["context"] = {
        "entry": "tunnel_ingress",
        "stage": "ingress_guard",
        "method": "POST",
        "content_type": "application_json",
        "forwarding_headers": ["x-forwarded-for", "x-forwarded-prefix"],
        "duplicate_headers": ["host"],
    }
    data["recent_denials"] = [{"sequence": 1, **deepcopy(data["last_denial"])}]
    return data


def test_extended_snapshot_strips_unknown_fields_and_copies_context() -> None:
    data = context_snapshot()
    data["last_denial"]["context"]["raw_header_values"] = "synthetic-secret"
    data["recent_denials"][0]["raw_path"] = "synthetic-secret"
    data["recent_denials"][0]["context"]["raw_headers"] = "synthetic-secret"
    parsed = parse_http_diagnostics(data)
    assert parsed == context_snapshot()
    assert "synthetic-secret" not in http_diagnostics_summary(data)
    data["last_denial"]["context"]["forwarding_headers"].clear()
    data["recent_denials"][0]["context"]["duplicate_headers"].clear()
    assert parsed == context_snapshot()
    parsed["last_denial"]["context"]["forwarding_headers"].clear()
    assert parsed["recent_denials"][0]["context"]["forwarding_headers"] == [
        "x-forwarded-for",
        "x-forwarded-prefix",
    ]


@pytest.mark.parametrize("location", ["last", "recent"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("entry", "synthetic-secret"),
        ("content_type", None),
        ("forwarding_headers", [["synthetic-secret"]]),
    ],
)
def test_extended_context_rejects_unrecognized_values_without_leaking(
    location: str, field: str, value: Any
) -> None:
    data = context_snapshot()
    denial = data["last_denial"] if location == "last" else data["recent_denials"][0]
    denial["context"][field] = value
    with pytest.raises(OperatorClientError) as error:
        http_diagnostics_summary(data)
    assert str(error.value) == "The broker returned invalid HTTP diagnostics."


def test_cli_malformed_context_does_not_echo_untrusted_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = context_snapshot()
    data["last_denial"]["context"]["forwarding_headers"] = ["synthetic-secret"]
    install_transport(monkeypatch, lambda _: httpx2.Response(200, json=data))
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", TOKEN)
    monkeypatch.setattr(
        "sys.argv", ["cardputer", "--auth-source", "environment", "http-diagnostics"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Request failed: The broker returned invalid HTTP diagnostics.\n"


@pytest.mark.parametrize(
    "field,value",
    [
        ("ok", 1),
        ("schema_version", 2),
        ("total_denials", -1),
        ("counts", {"synthetic-secret": 1}),
    ],
)
def test_invalid_diagnostics_schema_has_fixed_error(field: str, value: Any) -> None:
    data = snapshot()
    data[field] = value
    with pytest.raises(
        OperatorClientError, match="^The broker returned invalid HTTP diagnostics.$"
    ):
        parse_http_diagnostics(data)
