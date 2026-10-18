"""Authenticated, loopback-only client for the broker's operator API.

No device access lives here. Credentials are held in memory, sent only in the
Authorization header, and never included in diagnostics or returned objects.
"""

from typing import Any
from urllib.parse import urlsplit

import httpx2

from .http_diagnostics import DenialReason, parse_request_context


class OperatorClientError(ValueError):
    """A safe, fixed diagnostic suitable for an operator-facing window."""


def parse_http_diagnostics(data: dict[str, Any]) -> dict[str, Any]:
    """Keep only the fixed diagnostics schema; never echo extra server content."""

    def count(value: Any) -> int:
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("invalid_count")
        return value

    def denial(value: Any, *, with_context: bool, require_context: bool = False) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("invalid_denial")
        reason = DenialReason(value["reason"]).value
        layer, route, status = value["layer"], value["route"], value["http_status"]
        if (
            layer not in ("guard", "authentication", "mcp", "operator", "other")
            or route not in ("mcp", "operator", "other")
            or type(status) is not int
            or not 400 <= status <= 599
        ):
            raise ValueError("invalid_denial")
        result: dict[str, Any] = {
            "reason": reason,
            "layer": layer,
            "route": route,
            "http_status": status,
        }
        if with_context and (require_context or "context" in value):
            result["context"] = parse_request_context(value["context"])
        return result

    try:
        if (
            data["ok"] is not True
            or type(data["schema_version"]) is not int
            or data["schema_version"] != 1
            or data["since"] != "broker_start"
        ):
            raise ValueError("invalid_schema")
        raw_counts = data["counts"]
        if not isinstance(raw_counts, dict) or set(raw_counts) != {
            reason.value for reason in DenialReason
        }:
            raise ValueError("invalid_reasons")
        counts = {reason.value: count(raw_counts[reason.value]) for reason in DenialReason}
        with_context = "context_version" in data
        if with_context and (
            type(data["context_version"]) is not int or data["context_version"] != 1
        ):
            raise ValueError("invalid_context_version")
        last = None
        if data["last_denial"] is not None:
            last = denial(data["last_denial"], with_context=with_context)
        result = {
            "ok": True,
            "schema_version": 1,
            "since": "broker_start",
            "total_denials": count(data["total_denials"]),
            "counts": counts,
            "last_denial": last,
        }
        if with_context:
            raw_recent = data["recent_denials"]
            if not isinstance(raw_recent, list) or len(raw_recent) > 16:
                raise ValueError("invalid_recent_denials")
            recent = []
            for raw_entry in raw_recent:
                entry = denial(raw_entry, with_context=True, require_context=True)
                sequence = count(raw_entry["sequence"])
                if sequence == 0:
                    raise ValueError("invalid_sequence")
                recent.append({"sequence": sequence, **entry})
            result.update(context_version=1, recent_denials=recent)
        return result
    except (KeyError, TypeError, ValueError):
        raise OperatorClientError("The broker returned invalid HTTP diagnostics.") from None


def operator_base_url(url: str) -> str:
    """Accept a local broker URL or its MCP URL, never an arbitrary destination."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
        valid = (
            parsed.scheme == "http"
            and parsed.hostname == "127.0.0.1"
            and port is not None
            and 1 <= port <= 65535
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.path in ("", "/", "/mcp")
            and not any(character.isspace() for character in url)
        )
    except ValueError:
        valid = False
        port = None
    if not valid:
        raise OperatorClientError("Use a local http://127.0.0.1:PORT broker URL.")
    return f"http://127.0.0.1:{port}"


class OperatorClient:
    """Each request gets a fresh client, so response cookies are never reused."""

    def __init__(self, url: str, token: str) -> None:
        self.base_url = operator_base_url(url)
        if not token or any(character.isspace() or ord(character) < 32 for character in token):
            raise OperatorClientError("CARDPUTER_LOCAL_TOKEN must contain the local broker token.")
        self._token = token

    def state(self) -> dict[str, Any]:
        return self._request("GET", "/operator/state")

    def timeline(self) -> dict[str, Any]:
        return self._request("GET", "/operator/timeline")

    def diagnostics(self) -> dict[str, Any]:
        return parse_http_diagnostics(self._request("GET", "/operator/http-diagnostics"))

    def cancel_job(self, job_id: str, plan_hash: str, broker_epoch: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/operator/job-cancel",
            {"job_id": job_id, "plan_hash": plan_hash, "broker_epoch": broker_epoch},
            allow_refusal=True,
        )

    def local_home_line(self, job_id: str, plan_hash: str, diagnostic_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/operator/local-home-line",
            {"job_id": job_id, "plan_hash": plan_hash, "diagnostic_id": diagnostic_id},
        )

    def images(self) -> dict[str, Any]:
        return self._request("GET", "/operator/images")

    def image(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", "/operator/image", {"job_id": job_id})

    def catalog(self) -> dict[str, Any]:
        from .mcp_diagnostics import MCPDiagnosticsError, probe

        try:
            return probe(self.base_url + "/mcp", self._token)
        except MCPDiagnosticsError as error:
            message = (
                "MCP catalog probe timed out. The broker was not restarted."
                if str(error) == "live_probe_timed_out"
                else "MCP catalog probe failed. Check the existing broker connection."
            )
            raise OperatorClientError(message) from None

    def cancel(self, request_id: str) -> dict[str, Any]:
        return self._request("POST", "/operator/cancel", {"request_id": request_id})

    def reorder(self, request_id: str, position: int, expected_revision: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/operator/reorder",
            {
                "request_id": request_id,
                "position": position,
                "expected_revision": expected_revision,
            },
        )

    def release(self, lease_id: str) -> dict[str, Any]:
        return self._request("POST", "/operator/release", {"lease_id": lease_id})

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_refusal: bool = False,
    ) -> dict[str, Any]:
        try:
            # Explicitly disable environment proxies and redirects. A redirect must
            # never move the local token to another origin. No persistent cookie jar.
            with httpx2.Client(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx2.Timeout(5.0, connect=2.0),
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
            ) as http:
                response = http.request(method, self.base_url + path, json=payload)
        except httpx2.HTTPError:
            raise OperatorClientError(
                "Cannot reach the local broker. Check that it is running, then refresh."
            ) from None
        if response.status_code in (401, 403):
            raise OperatorClientError(
                "Local operator authentication failed. Check CARDPUTER_LOCAL_TOKEN."
            )
        if response.status_code != 200:
            raise OperatorClientError(
                "The broker rejected the operator request. Refresh and retry."
            )
        try:
            data = response.json()
        except ValueError:
            raise OperatorClientError("The broker returned an invalid operator response.") from None
        if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
            raise OperatorClientError("The broker returned an invalid operator response.")
        if not data["ok"]:
            if allow_refusal:
                return {"ok": False}
            # Never display server-controlled strings, response bodies, URLs, or
            # exception text. The next state refresh supplies the current facts.
            raise OperatorClientError(
                "Operation refused: the selection changed or the device is busy."
            )
        return data
