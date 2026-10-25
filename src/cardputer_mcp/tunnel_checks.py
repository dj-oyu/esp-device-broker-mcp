"""Shared decision core for the console and GUI Tunnel checks.

The console launcher (``tunnel.preflight``/``tunnel.check_broker``) and the GUI
manager (``TunnelManager``) deliberately apply different policies: check order,
an idle/closed-hardware gate (console only), a backend match (GUI only) and
their own discovery code. Each caller passes its policy in. The decisions here
only return fixed codes; raising ``TunnelError`` stays with the caller, and the
MCP ``connect``/``call`` seams are passed in by each caller so patches of the
caller's module keep taking effect.

Process supervision (``subprocess.Popen`` for the console, a Job Object for the
GUI) and readiness loops are intentionally not shared.
"""

from collections.abc import Awaitable, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal

PLACEHOLDER_TUNNEL_ID = "tunnel_" + "0" * 32

ConfigurationCheck = Literal["ingress", "url", "health_port", "serial"]

# Console: no URL argument; serial opt-in before the health-port conflict.
CONSOLE_CONFIGURATION: tuple[ConfigurationCheck, ...] = ("ingress", "serial", "health_port")
# GUI: the configured URL must equal the broker's; health port before serial.
GUI_CONFIGURATION: tuple[ConfigurationCheck, ...] = ("ingress", "url", "health_port", "serial")


def configuration_refusal(
    settings: Any,
    broker: Any,
    order: Iterable[ConfigurationCheck],
    *,
    enable_serial: bool,
    url: str | None = None,
) -> str | None:
    """First refusal for loaded broker settings in the caller's ``order``.

    The placeholder ID check and loading the broker configuration stay with the
    caller: both run before this, and loading uses the caller's own module seam.
    """
    for check in order:
        if check == "ingress":
            runtime = broker.tunnel.ingress_enabled if broker.tunnel else False
            if settings.ingress_enabled != runtime:
                return "tunnel_config_conflict"
        elif check == "url":
            if url != f"http://127.0.0.1:{broker.port}/mcp":
                return "tunnel_broker_url_mismatch"
        elif check == "health_port":
            if settings.health_port == broker.port:
                return "health_port_conflicts_with_broker"
        elif check == "serial":
            if broker.backend == "serial" and not enable_serial:
                return "serial_tunnel_requires_explicit_enable_serial"
    return None


def ingress_refusal(identity: dict[str, Any], ingress_enabled: bool) -> str | None:
    """A stale or older broker must never silently receive a different route."""
    if (
        type(ingress_enabled) is not bool
        or identity.get("tunnel_ingress_enabled") is not ingress_enabled
    ):
        return "tunnel_broker_ingress_mismatch"
    return None


IdentityCheck = Literal[
    "ok_and_authenticated", "authenticated", "origin", "scope", "backend", "ingress"
]

# Console: the identity result itself must also be ok; no backend comparison.
CONSOLE_IDENTITY: tuple[IdentityCheck, ...] = ("ok_and_authenticated", "origin", "scope", "ingress")
# GUI: the configured backend must match before the ingress mode is compared.
GUI_IDENTITY: tuple[IdentityCheck, ...] = ("authenticated", "origin", "scope", "backend", "ingress")


def identity_refusal(
    identity: dict[str, Any],
    order: Iterable[IdentityCheck],
    *,
    ingress_enabled: bool,
    backend: str | None = None,
) -> str | None:
    """First refusal of a broker identity (GUI: the merged probe result)."""
    for check in order:
        if check == "ok_and_authenticated":
            if identity.get("ok") is not True or identity.get("authenticated") is not True:
                return "tunnel_broker_auth_failed"
        elif check == "authenticated":
            if identity.get("authenticated") is not True:
                return "tunnel_broker_auth_failed"
        elif check == "origin":
            if identity.get("origin") != "remote_mcp":
                return "tunnel_broker_origin_rejected"
        elif check == "scope":
            scopes = identity.get("scopes")
            if not isinstance(scopes, list) or "read" not in scopes:
                return "tunnel_broker_scope_rejected"
        elif check == "backend":
            if identity.get("backend") != backend:
                return "tunnel_broker_backend_mismatch"
        elif check == "ingress":
            if code := ingress_refusal(identity, ingress_enabled):
                return code
    return None


def console_device_refusal(status: dict[str, Any]) -> str | None:
    """Console only: the broker must be idle with its hardware closed."""
    if not status.get("ok") or status.get("active_job") or status.get("owner"):
        return "broker_not_idle"
    if status.get("hardware_connected") or status.get("ownership_uncertain"):
        return "broker_hardware_not_closed"
    return None


def gui_status_refusal(status: Any) -> str | None:
    """GUI only: a valid status, with no idle, owner, job or hardware gate."""
    if not isinstance(status, dict) or status.get("ok") is not True:
        return "tunnel_broker_status_invalid"
    return None


def discovery_refusal(catalog: Any, required: Iterable[str], code: str) -> str | None:
    """``code`` is the caller's: ``mcp_discovery_failed`` or ``tunnel_broker_discovery_failed``."""
    if not set(required) <= {item.name for item in catalog.tools}:
        return code
    return None


Connect = Callable[[str, str], AbstractAsyncContextManager[Any]]
Call = Callable[[Any, str, dict[str, Any]], Awaitable[dict[str, Any]]]


async def read_broker(
    connect: Connect,
    call: Call,
    url: str,
    bearer: str,
    *,
    after_identity: Callable[[dict[str, Any]], None] | None = None,
    after_catalog: Callable[[Any], None] | None = None,
) -> tuple[dict[str, Any], Any, Any]:
    """The read-only probe sequence both callers make, inside one MCP session.

    ``identity`` status, then ``device`` status, then ``list_tools``. The hooks
    run inside the session and may raise to stop before the remaining calls.
    The caller owns the overall timeout.
    """
    async with connect(url, bearer.removeprefix("Bearer ")) as client:
        identity = await call(client, "status", {"view": "identity"})
        if after_identity is not None:
            after_identity(identity)
        status = await call(client, "status", {"view": "device"})
        catalog = await client.list_tools()
        if after_catalog is not None:
            after_catalog(catalog)
    return identity, status, catalog
