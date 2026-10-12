"""Safe local HTTP diagnostics client and CLI; all credentials are synthetic."""

import json
import queue
import threading
from collections.abc import Callable
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import httpx2
import pytest

from cardputer_mcp import cli
from cardputer_mcp.gui import (
    Operation,
    QueueController,
    _Window,
    http_diagnostics_summary,
    perform,
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


def test_parser_accepts_diagnostics_with_explicit_url_and_environment_auth() -> None:
    args = cli.parser().parse_args(
        [
            "--url",
            "http://127.0.0.1:9988/mcp",
            "--auth-source",
            "environment",
            "http-diagnostics",
        ]
    )
    assert args.command == "http-diagnostics"
    assert args.url == "http://127.0.0.1:9988/mcp"
    assert args.auth_source == "environment"
    assert args.client == "local_cli"


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
        ["--auth-source", "none"],
        ["--auth-source", "file", "--auth-file", "must-not-read.json"],
        ["--auth-source", "key-environment"],
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


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
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


def test_diagnostics_parser_discards_unknown_fields_before_display() -> None:
    data = denied_snapshot()
    data["raw_request"] = "synthetic-secret"
    data["last_denial"]["raw_headers"] = "synthetic-secret"
    parsed = parse_http_diagnostics(data)
    assert parsed == denied_snapshot()
    text = http_diagnostics_summary(data)
    assert "Total denials: 1" in text
    assert "host_not_allowed: 1" in text
    assert "Layer: guard | Route: mcp | HTTP: 403" in text
    assert "synthetic-secret" not in text
    assert "raw_request" not in text
    assert "raw_headers" not in text


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


def test_old_snapshot_keeps_schema_and_labels_missing_details() -> None:
    data = denied_snapshot()
    data["last_denial"]["context"] = {"raw_headers": "synthetic-secret"}
    data["recent_denials"] = ["synthetic-secret"]
    assert parse_http_diagnostics(data) == denied_snapshot()
    summary = http_diagnostics_summary(data)
    assert "Request details unavailable (older broker)." in summary
    assert "synthetic-secret" not in summary
    assert "Request details unavailable (older broker)." in http_diagnostics_summary(snapshot())


def test_extended_snapshot_strips_unknown_fields_and_copies_context() -> None:
    data = context_snapshot()
    data["last_denial"]["context"]["raw_header_values"] = "synthetic-secret"
    data["recent_denials"][0]["raw_path"] = "synthetic-secret"
    data["recent_denials"][0]["context"]["raw_headers"] = "synthetic-secret"
    parsed = parse_http_diagnostics(data)
    assert parsed == context_snapshot()
    data["last_denial"]["context"]["forwarding_headers"].clear()
    data["recent_denials"][0]["context"]["duplicate_headers"].clear()
    assert parsed == context_snapshot()
    parsed["last_denial"]["context"]["forwarding_headers"].clear()
    assert parsed["recent_denials"][0]["context"]["forwarding_headers"] == [
        "x-forwarded-for",
        "x-forwarded-prefix",
    ]


def test_gui_shows_nonzero_counts_last_context_and_latest_four_rows() -> None:
    data = context_snapshot()
    data["recent_denials"] = [
        {"sequence": sequence, **deepcopy(data["last_denial"])} for sequence in range(1, 17)
    ]
    parsed = parse_http_diagnostics(data)
    assert len(parsed["recent_denials"]) == 16
    assert len(parsed["counts"]) == len(DenialReason)
    summary = http_diagnostics_summary(data)
    assert "host_not_allowed: 1" in summary
    assert "non_ascii_headers: 0" not in summary
    assert "Entry: tunnel_ingress | Stage: ingress_guard" in summary
    assert "Method: POST | Content-Type: application_json" in summary
    assert "Forwarding headers: x-forwarded-for, x-forwarded-prefix" in summary
    assert "Duplicate headers: host" in summary
    rows = [line for line in summary.splitlines() if line.startswith("#")]
    assert len(rows) == 4
    assert [line.split()[0] for line in rows] == ["#16", "#15", "#14", "#13"]
    assert all("tunnel_ingress/ingress_guard" in row for row in rows)


def test_gui_empty_context_lists_and_zero_counts_are_concise() -> None:
    data = context_snapshot()
    data["last_denial"]["context"]["forwarding_headers"] = []
    data["last_denial"]["context"]["duplicate_headers"] = []
    summary = http_diagnostics_summary(data)
    assert "Forwarding headers: none" in summary
    assert "Duplicate headers: none" in summary
    assert "No denial counters recorded." in http_diagnostics_summary(snapshot())


@pytest.mark.parametrize("location", ["last", "recent"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("entry", "synthetic-secret"),
        ("entry", []),
        ("stage", "synthetic-secret"),
        ("method", "synthetic-secret"),
        ("content_type", "application/json; boundary=synthetic-secret"),
        ("content_type", None),
        ("forwarding_headers", ["x-forwarded-synthetic-secret"]),
        ("forwarding_headers", "x-forwarded-for"),
        ("forwarding_headers", [["synthetic-secret"]]),
        ("duplicate_headers", ["synthetic-secret"]),
        ("duplicate_headers", {}),
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


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_version", True),
        ("context_version", 2),
        ("context_version", "synthetic-secret"),
        ("recent_denials", None),
        ("recent_denials", {}),
        ("recent_denials", ["synthetic-secret"]),
    ],
)
def test_extended_schema_has_fixed_error(field: str, value: Any) -> None:
    data = context_snapshot()
    data[field] = value
    with pytest.raises(
        OperatorClientError, match="^The broker returned invalid HTTP diagnostics.$"
    ):
        parse_http_diagnostics(data)


@pytest.mark.parametrize("sequence", [0, -1, True, 1.0, 2**63, "synthetic-secret"])
def test_recent_denial_sequence_is_bounded_integer(sequence: Any) -> None:
    data = context_snapshot()
    data["recent_denials"][0]["sequence"] = sequence
    with pytest.raises(
        OperatorClientError, match="^The broker returned invalid HTTP diagnostics.$"
    ):
        parse_http_diagnostics(data)


def test_recent_denials_are_bounded_to_sixteen() -> None:
    data = context_snapshot()
    data["recent_denials"] *= 17
    with pytest.raises(
        OperatorClientError, match="^The broker returned invalid HTTP diagnostics.$"
    ):
        parse_http_diagnostics(data)


def test_recent_denial_requires_context() -> None:
    data = context_snapshot()
    del data["recent_denials"][0]["context"]
    with pytest.raises(
        OperatorClientError, match="^The broker returned invalid HTTP diagnostics.$"
    ):
        parse_http_diagnostics(data)


def test_versioned_last_denial_can_lack_context() -> None:
    data = context_snapshot()
    del data["last_denial"]["context"]
    assert parse_http_diagnostics(data) == data
    summary = http_diagnostics_summary(data)
    assert "Request details unavailable." in summary
    assert "older broker" not in summary


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
        ("schema_version", True),
        ("schema_version", 2),
        ("since", "synthetic-secret"),
        ("total_denials", -1),
        ("total_denials", 2**63),
        ("total_denials", True),
        ("total_denials", 1.0),
        ("counts", {"synthetic-secret": 1}),
        ("counts", []),
        ("last_denial", "synthetic-secret"),
    ],
)
def test_invalid_diagnostics_schema_has_fixed_error(field: str, value: Any) -> None:
    data = snapshot()
    data[field] = value
    with pytest.raises(
        OperatorClientError, match="^The broker returned invalid HTTP diagnostics.$"
    ):
        parse_http_diagnostics(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", "synthetic-secret"),
        ("layer", "synthetic-secret"),
        ("route", "synthetic-secret"),
        ("http_status", 200),
        ("http_status", 600),
        ("http_status", "synthetic-secret"),
    ],
)
def test_last_denial_rejects_unrecognized_display_values(field: str, value: Any) -> None:
    data = denied_snapshot()
    data["last_denial"][field] = value
    with pytest.raises(
        OperatorClientError, match="^The broker returned invalid HTTP diagnostics.$"
    ):
        http_diagnostics_summary(data)


def gui_api() -> Mock:
    api = Mock(spec=OperatorClient)
    api.diagnostics.return_value = denied_snapshot()
    api.state.return_value = {
        "ok": True,
        "backend": "mock",
        "queue_revision": "revision-1",
        "sessions": [],
        "waiters": [],
        "owner": None,
        "active_job": None,
        "needs_review": False,
        "ownership_uncertain": False,
    }
    return api


def test_gui_diagnostics_uses_existing_worker_and_preserves_state_refresh() -> None:
    api = gui_api()
    controller = QueueController()
    operation = controller.diagnostics()
    assert operation == Operation(1, "diagnostics")
    assert controller.busy
    assert controller.diagnostics() is None
    assert controller.refresh() is None
    outcome = perform(api, operation)
    assert [call[0] for call in api.mock_calls] == ["diagnostics", "state"]
    assert outcome.diagnostics == http_diagnostics_summary(denied_snapshot())
    assert outcome.snapshot is not None
    assert not outcome.failed
    assert controller.accept(outcome)
    assert controller.fresh and not controller.busy
    assert not controller.accept(outcome)
    controller.close()
    assert controller.diagnostics() is None


def test_gui_diagnostics_failure_redacts_response_and_still_refreshes_queue() -> None:
    api = gui_api()
    api.diagnostics.return_value["last_denial"]["reason"] = "synthetic-secret"
    outcome = perform(api, Operation(1, "diagnostics"))
    assert outcome.failed
    assert outcome.diagnostics is None
    assert outcome.snapshot is not None
    assert outcome.message == "The broker returned invalid HTTP diagnostics."
    assert [call[0] for call in api.mock_calls] == ["diagnostics", "state"]


def test_gui_queue_refresh_failure_keeps_successful_read_only_diagnostics() -> None:
    api = gui_api()
    api.state.side_effect = OperatorClientError("Cannot reach the local broker.")
    outcome = perform(api, Operation(1, "diagnostics"))
    assert outcome.failed and outcome.snapshot is None
    assert outcome.diagnostics == http_diagnostics_summary(denied_snapshot())
    assert outcome.message == "Cannot reach the local broker."


@pytest.mark.parametrize("closed", [False, True])
def test_gui_poll_displays_once_and_discards_result_after_close(closed: bool) -> None:
    window = object.__new__(_Window)
    window.controller = QueueController()
    window.stopped = threading.Event()
    window.root = Mock()
    window.results = queue.Queue()
    window._render = Mock()
    window._show_diagnostics = Mock()
    window._render_plan_badge = Mock()
    operation = window.controller.diagnostics()
    assert operation is not None
    outcome = perform(gui_api(), operation)
    window.results.put(outcome)
    window.results.put(outcome)
    if closed:
        window.controller.close()
    window._poll()
    if closed:
        window._render.assert_not_called()
        window._show_diagnostics.assert_not_called()
    else:
        window._render.assert_called_once()
        window._show_diagnostics.assert_called_once_with(outcome.diagnostics)


def test_recent_gui_rows_include_method_and_media_for_earlier_rejection() -> None:
    data = context_snapshot()
    data["recent_denials"][0]["context"]["method"] = "POST"
    data["recent_denials"][0]["context"]["content_type"] = "text_plain"
    summary = http_diagnostics_summary(data)
    assert "POST text_plain" in summary
