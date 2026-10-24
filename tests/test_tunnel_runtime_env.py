"""Dummy credentials only; no native process or network access."""

import pytest

from cardputer_mcp.tunnel import runtime_api_key


@pytest.mark.parametrize(
    "supplied,env,expected",
    [
        (
            "typed",
            {"CARDPUTER_TUNNEL_API_KEY": "broker", "CONTROL_PLANE_API_KEY": "native"},
            "typed",
        ),
        ("", {"CARDPUTER_TUNNEL_API_KEY": "", "CONTROL_PLANE_API_KEY": "native"}, "native"),
    ],
)
def test_runtime_precedence(supplied, env, expected):
    assert runtime_api_key(supplied, env) == expected


def test_console_consumes_both_runtime_environment_names(monkeypatch):
    from types import SimpleNamespace

    from cardputer_mcp import tunnel

    environment = {"CARDPUTER_TUNNEL_API_KEY": "selected", "CONTROL_PLANE_API_KEY": "fallback"}
    monkeypatch.setattr(tunnel, "os", SimpleNamespace(environ=environment))
    assert tunnel._consume_runtime_api_key() == "selected"
    assert environment == {}
