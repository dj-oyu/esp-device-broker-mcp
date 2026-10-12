"""Fixed diagnostic categories cannot retain request values or arbitrary names."""

import json
from types import SimpleNamespace

import pytest
from test_http_diagnostics import BASE_HEADERS, MARKER, invoke, should_not_run

from cardputer_mcp.http_diagnostics import (
    CONTEXT_FORWARDING_NAMES,
    FORWARDING_NAMES,
    DenialReason,
    HTTPDiagnostics,
    parse_request_context,
    request_context,
)
from cardputer_mcp.models import Origin
from cardputer_mcp.server import LoopbackOnly, Principal, TunnelIngress


def context(headers=None, **extra):
    return request_context(
        {
            "type": "http",
            "path": "/tunnel/mcp",
            "method": "POST",
            "headers": BASE_HEADERS if headers is None else headers,
            **extra,
        },
        "ingress_guard",
    )


@pytest.mark.parametrize("name", sorted(FORWARDING_NAMES))
def test_known_names_are_finite_values_are_not_retained(name):
    result = context(BASE_HEADERS + [(name.upper().encode(), MARKER.encode())])
    assert result["forwarding_headers"] == [name]
    assert MARKER not in json.dumps(result)


def test_arbitrary_names_collapse_to_other_and_duplicates_are_categories():
    private_name = ("x-forwarded-" + MARKER).encode()
    result = context(BASE_HEADERS + [(private_name, MARKER.encode())] * 2)
    assert result["forwarding_headers"] == ["other_forwarding"]
    assert result["duplicate_headers"] == ["other"]
    assert MARKER not in json.dumps(result)


@pytest.mark.parametrize(
    "media,expected",
    [
        (None, "missing"),
        (b"application/json", "application_json"),
        (b"application/json; charset=utf-8", "application_json"),
        (b"Application/JSON ", "application_json_variant"),
        (b"text/plain", "text_plain"),
        (b"application/x-www-form-urlencoded", "form_urlencoded"),
        (b"multipart/form-data; boundary=PRIVATE", "multipart_form_data"),
        (b"application/octet-stream", "octet_stream"),
        (MARKER.encode(), "other"),
    ],
)
def test_content_type_is_classified_not_echoed(media, expected):
    headers = [BASE_HEADERS[0]] + ([] if media is None else [(b"content-type", media)])
    result = context(headers)
    assert result["content_type"] == expected
    assert MARKER not in json.dumps(result) and "PRIVATE" not in json.dumps(result)


def test_unknown_path_method_and_ambiguous_media_are_not_echoed():
    result = context(
        BASE_HEADERS + [(b"Content-Type", MARKER.encode())],
        path="/" + MARKER,
        method=MARKER,
    )
    assert result["entry"] == result["method"] == "other"
    assert result["content_type"] == "ambiguous"
    assert result["duplicate_headers"] == ["content-type"]
    assert MARKER not in json.dumps(result)


@pytest.mark.parametrize(
    "field", ["entry", "stage", "method", "content_type", "forwarding_headers", "duplicate_headers"]
)
def test_context_validation_never_echoes_invalid_fields(field):
    data = context()
    data[field] = [MARKER] if field.endswith("headers") else MARKER
    with pytest.raises(ValueError, match="^invalid_diagnostic_context$") as error:
        parse_request_context(data)
    assert MARKER not in str(error.value)


def test_recent_history_is_bounded_deeply_copied_and_oldest_first():
    diagnostics = HTTPDiagnostics()
    request = context(BASE_HEADERS + [(b"x-forwarded-prefix", MARKER.encode())])
    for _ in range(20):
        diagnostics.record(DenialReason.FORWARDING_HEADER_PRESENT, "mcp", 403, context=request)
    snap = diagnostics.snapshot()
    assert [item["sequence"] for item in snap["recent_denials"]] == list(range(5, 21))
    request["forwarding_headers"].append(MARKER)
    snap["last_denial"]["context"]["forwarding_headers"].append(MARKER)
    snap["recent_denials"][0]["context"]["forwarding_headers"].append(MARKER)
    assert MARKER not in json.dumps(diagnostics.snapshot())


async def test_direct_vs_ingress_rejection_has_original_physical_entry():
    diagnostics = HTTPDiagnostics()
    principal = Principal("synthetic", Origin.REMOTE, frozenset({"read"}))
    guarded = LoopbackOnly(should_not_run, 8765, diagnostics=diagnostics)
    ingress = TunnelIngress(guarded, lambda header: principal)
    direct = await invoke(ingress, headers=BASE_HEADERS + [(b"x-forwarded-for", MARKER.encode())])
    alias = await invoke(
        ingress,
        path="/tunnel/mcp",
        headers=BASE_HEADERS + [(b"x-forwarded-prefix", MARKER.encode())],
    )
    assert direct[0]["status"] == alias[0]["status"] == 403
    recent = diagnostics.snapshot()["recent_denials"]
    assert [(r["context"]["entry"], r["context"]["stage"]) for r in recent] == [
        ("direct_mcp", "direct_guard"),
        ("tunnel_ingress", "ingress_guard"),
    ]
    assert recent[1]["context"]["forwarding_headers"] == ["x-forwarded-prefix"]
    assert MARKER not in json.dumps(recent)


async def test_post_ingress_auth_failure_is_not_mislabeled_direct():
    diagnostics = HTTPDiagnostics()
    principal = Principal("synthetic", Origin.REMOTE, frozenset({"read"}))
    guarded = LoopbackOnly(
        should_not_run, 8765, lambda: SimpleNamespace(authenticate=lambda header: None), diagnostics
    )
    ingress = TunnelIngress(guarded, lambda header: principal)
    response = await invoke(
        ingress, path="/tunnel/mcp", headers=BASE_HEADERS + [(b"x-forwarded-for", MARKER.encode())]
    )
    assert response[0]["status"] == 401
    result = diagnostics.snapshot()["last_denial"]["context"]
    assert result["entry"] == "tunnel_ingress" and result["stage"] == "post_ingress_auth"
    assert result["forwarding_headers"] == []


async def test_post_and_get_classification_does_not_change_content_type_gate():
    diagnostics = HTTPDiagnostics()
    principal = Principal("synthetic", Origin.REMOTE, frozenset({"read"}))

    async def downstream(scope, receive, send):
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": MARKER.encode()})

    ingress = TunnelIngress(
        LoopbackOnly(downstream, 8765, diagnostics=diagnostics), lambda h: principal
    )
    headers = [BASE_HEADERS[0], (b"content-type", b"text/plain")]
    for method in ("POST", "GET"):
        assert (await invoke(ingress, path="/tunnel/mcp", method=method, headers=headers))[0][
            "status"
        ] == 403
    recent = diagnostics.snapshot()["recent_denials"]
    assert recent[0]["reason"] == "content_type_not_json"
    assert recent[0]["context"]["stage"] == "ingress_guard"
    assert recent[1]["reason"] == "mcp_http_rejection"
    assert recent[1]["context"]["stage"] == "mcp_handler"
    assert [r["context"]["method"] for r in recent] == ["POST", "GET"]
    assert MARKER not in json.dumps(recent)


def test_diagnostic_name_catalog_does_not_expand_accepted_metadata():
    assert TunnelIngress.FORWARDING_METADATA == {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-port",
        "x-forwarded-client-cert",
    }
    assert "x-forwarded-prefix" in CONTEXT_FORWARDING_NAMES
    assert "x-forwarded-prefix" not in TunnelIngress.FORWARDING_METADATA
