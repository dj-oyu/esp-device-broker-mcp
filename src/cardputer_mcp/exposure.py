"""Exposure ``mode`` types and the translation of the legacy boolean flags.

Each component that can be reached from outside this machine has ONE ``mode``
setting whose default is the closed state ``off``:

- web UI: ``off`` | ``tailnet`` (``tailscale serve``) | ``public`` (``tailscale funnel``)
- Claude gateway: ``off`` | ``public`` (``tailscale funnel``; it has no tailnet-only form)
- OpenAI Tunnel ingress: ``off`` | ``ingress`` (the broker serves the Tunnel route)

Earlier configuration files used booleans (``serve``/``funnel``/``ingress_enabled``).
``resolve_mode`` still accepts them so existing files start, under one rule: a
legacy key can only select the state it always selected, never a more open one, and
anything contradictory is a fixed-code error. The local desktop GUI can reuse the
same types and ``resolve_mode`` to validate a mode before persisting it. Nothing here
opens a listener or is reachable from MCP.
"""

from collections.abc import Mapping
from typing import Any, Literal

WebUIMode = Literal["off", "tailnet", "public"]
GatewayMode = Literal["off", "public"]
TunnelIngressMode = Literal["off", "ingress"]

CLOSED_MODE = "off"

# legacy boolean key -> the mode ``key = true`` always meant.
WEBUI_LEGACY_FLAGS: Mapping[str, str] = {"serve": "tailnet", "funnel": "public"}
GATEWAY_LEGACY_FLAGS: Mapping[str, str] = {"funnel": "public"}
TUNNEL_LEGACY_FLAGS: Mapping[str, str] = {"ingress_enabled": "ingress"}


def resolve_mode(data: Any, component: str, legacy_flags: Mapping[str, str]) -> Any:
    """Return ``data`` with legacy boolean flags folded into ``mode``.

    Non-mapping input, and input with no legacy key, is returned unchanged (an absent
    ``mode`` stays absent and defaults to ``off``). Errors carry fixed codes only and
    never the supplied values.
    """
    if not isinstance(data, dict) or not any(key in data for key in legacy_flags):
        return data
    selected: list[str] = []
    for key, mode in legacy_flags.items():
        if key not in data:
            continue
        if type(data[key]) is not bool:
            raise ValueError(f"{component}_{key}_must_be_boolean")
        if data[key]:
            selected.append(mode)
    if len(selected) > 1:
        raise ValueError(f"{component}_exposure_flags_exclusive")
    implied = selected[0] if selected else CLOSED_MODE
    explicit = data.get("mode")
    if "mode" in data and explicit != implied:
        raise ValueError(f"{component}_mode_conflicts_with_legacy_flags")
    return {**{k: v for k, v in data.items() if k not in legacy_flags}, "mode": implied}
