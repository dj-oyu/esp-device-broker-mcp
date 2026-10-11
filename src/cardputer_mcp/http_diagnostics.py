"""Process-local denial counters; never retain HTTP input or response content."""

from collections import deque
from copy import deepcopy
from enum import StrEnum
from threading import Lock
from typing import Any, Literal

from starlette.types import Scope


class DenialReason(StrEnum):
    NON_ASCII_HEADERS = "non_ascii_headers"
    DUPLICATE_HEADERS = "duplicate_headers"
    NON_LOOPBACK_PEER = "non_loopback_peer"
    HOST_NOT_ALLOWED = "host_not_allowed"
    ORIGIN_NOT_ALLOWED = "origin_not_allowed"
    FORWARDING_HEADER_PRESENT = "forwarding_header_present"
    CONTENT_TYPE_NOT_JSON = "content_type_not_json"
    AUTHENTICATION_REQUIRED = "authentication_required"
    TUNNEL_ROUTE_REJECTED = "tunnel_route_rejected"
    TUNNEL_PRINCIPAL_REJECTED = "tunnel_principal_rejected"
    MCP_HTTP_REJECTION = "mcp_http_rejection"
    OPERATOR_HTTP_REJECTION = "operator_http_rejection"
    OTHER_HTTP_REJECTION = "other_http_rejection"


Route = Literal["mcp", "operator", "other"]
_MAX_COUNT = 2**63 - 1


def route_category(path: str) -> Route:
    if path in ("/mcp", "/mcp/"):
        return "mcp"
    if path == "/operator" or path.startswith("/operator/"):
        return "operator"
    return "other"


CONTEXT_ENTRIES = frozenset({"direct_mcp", "tunnel_ingress", "operator", "other"})
CONTEXT_STAGES = frozenset(
    {
        "direct_guard",
        "ingress_guard",
        "ingress_auth",
        "post_ingress_guard",
        "post_ingress_auth",
        "mcp_handler",
        "operator_handler",
        "other_handler",
    }
)
CONTEXT_METHODS = frozenset({"GET", "POST", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH", "other"})
CONTEXT_CONTENT_TYPES = frozenset(
    {
        "application_json",
        "application_json_variant",
        "missing",
        "text_plain",
        "form_urlencoded",
        "multipart_form_data",
        "octet_stream",
        "other",
        "ambiguous",
    }
)
FORWARDING_NAMES = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "x-forwarded-path",
        "x-forwarded-uri",
        "x-forwarded-server",
        "x-forwarded-scheme",
        "x-forwarded-protocol",
        "x-forwarded-ssl",
        "x-forwarded-client-cert",
        "x-forwarded-client-ip",
        "x-forwarded-address",
        "x-forwarded-by",
        "x-forwarded-script-name",
    }
)
CONTEXT_FORWARDING_NAMES = FORWARDING_NAMES | {"other_forwarding"}
CONTEXT_DUPLICATE_NAMES = FORWARDING_NAMES | {
    "authorization",
    "host",
    "origin",
    "content-type",
    "other",
}
INGRESS_CONTEXT_KEY = "cardputer.diagnostic_tunnel_ingress"


def parse_request_context(value: object) -> dict[str, Any]:
    """Validate and copy ONLY finite diagnostic categories, never arbitrary text."""
    if not isinstance(value, dict):
        raise ValueError("invalid_diagnostic_context")
    result: dict[str, Any] = {}
    for key, allowed in (
        ("entry", CONTEXT_ENTRIES),
        ("stage", CONTEXT_STAGES),
        ("method", CONTEXT_METHODS),
        ("content_type", CONTEXT_CONTENT_TYPES),
    ):
        item = value.get(key)
        if not isinstance(item, str) or item not in allowed:
            raise ValueError("invalid_diagnostic_context")
        result[key] = item
    for key, allowed in (
        ("forwarding_headers", CONTEXT_FORWARDING_NAMES),
        ("duplicate_headers", CONTEXT_DUPLICATE_NAMES),
    ):
        items = value.get(key)
        if not isinstance(items, list) or len(items) > len(allowed):
            raise ValueError("invalid_diagnostic_context")
        if any(not isinstance(item, str) or item not in allowed for item in items):
            raise ValueError("invalid_diagnostic_context")
        if len(set(items)) != len(items):
            raise ValueError("invalid_diagnostic_context")
        result[key] = sorted(items)
    return result


def request_context(scope: Scope, stage: str) -> dict[str, Any]:
    """Reduce a denied request to fixed names/categories; never preserve values."""
    path = scope.get("path", "")
    if path == "/tunnel/mcp" or scope.get(INGRESS_CONTEXT_KEY) is True:
        entry = "tunnel_ingress"
    elif route_category(path) == "mcp":
        entry = "direct_mcp"
    elif route_category(path) == "operator":
        entry = "operator"
    else:
        entry = "other"
    seen: set[bytes] = set()
    forwarding: set[str] = set()
    duplicates: set[str] = set()
    content_types: list[bytes] = []
    for key, value in scope.get("headers", []):
        name = key.lower()
        # Comparing bytes to this fixed mapping cannot expose unknown names.
        known = next((item for item in CONTEXT_DUPLICATE_NAMES if name == item.encode()), "other")
        if name in seen:
            duplicates.add(known)
        seen.add(name)
        if name == b"forwarded" or name.startswith(b"x-forwarded-"):
            forwarding.add(known if known in FORWARDING_NAMES else "other_forwarding")
        if name == b"content-type":
            content_types.append(value)
    media = "missing"
    if len(content_types) > 1:
        media = "ambiguous"
    elif content_types:
        raw_media = content_types[0].split(b";", 1)[0]
        if raw_media == b"application/json":
            media = "application_json"
        else:
            media = {
                b"application/json": "application_json_variant",
                b"text/plain": "text_plain",
                b"application/x-www-form-urlencoded": "form_urlencoded",
                b"multipart/form-data": "multipart_form_data",
                b"application/octet-stream": "octet_stream",
            }.get(raw_media.strip().lower(), "other")
    method = scope.get("method", "other")
    return parse_request_context(
        {
            "entry": entry,
            "stage": stage,
            "method": method if method in CONTEXT_METHODS else "other",
            "content_type": media,
            "forwarding_headers": sorted(forwarding),
            "duplicate_headers": sorted(duplicates),
        }
    )


class HTTPDiagnostics:
    """A fixed-size snapshot, accessible only through the local operator API.

    Counts are cumulative for this broker process. The last denial is not an
    audit trail and has no request identity. Never inspect bodies, query strings,
    header values, peer addresses, principal IDs, or exception text here.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts = {reason.value: 0 for reason in DenialReason}
        self._total = 0
        self._last: dict[str, Any] | None = None
        self._recent: deque[dict[str, Any]] = deque(maxlen=16)

    def record(
        self,
        reason: DenialReason,
        route: Route,
        status: int,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        # Defend the output contract even if a future caller ignores annotations.
        if not isinstance(reason, DenialReason) or route not in ("mcp", "operator", "other"):
            return
        if type(status) is not int or not 400 <= status <= 599:
            return
        safe_context = None
        if context is not None:
            try:
                safe_context = parse_request_context(context)
            except ValueError:
                pass
        layer = "guard"
        if reason in (DenialReason.AUTHENTICATION_REQUIRED, DenialReason.TUNNEL_PRINCIPAL_REJECTED):
            layer = "authentication"
        elif reason in (
            DenialReason.MCP_HTTP_REJECTION,
            DenialReason.OPERATOR_HTTP_REJECTION,
            DenialReason.OTHER_HTTP_REJECTION,
        ):
            layer = route
        with self._lock:
            self._counts[reason.value] = min(_MAX_COUNT, self._counts[reason.value] + 1)
            self._total = min(_MAX_COUNT, self._total + 1)
            self._last = {
                "reason": reason.value,
                "layer": layer,
                "route": route,
                "http_status": status,
            }
            if safe_context is not None:
                self._last["context"] = safe_context
                self._recent.append({"sequence": self._total, **deepcopy(self._last)})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": 1,
                "since": "broker_start",
                "total_denials": self._total,
                "counts": dict(self._counts),
                "last_denial": deepcopy(self._last),
                "context_version": 1,
                "recent_denials": deepcopy(list(self._recent)),
            }
