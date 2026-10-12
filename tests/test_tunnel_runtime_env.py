"""Dummy credentials only; no native process or network access."""

import pytest

from cardputer_mcp.tunnel import TunnelError, child_environment, runtime_api_key


@pytest.mark.parametrize(
    "supplied,env,expected",
    [
        (
            "typed",
            {"CARDPUTER_TUNNEL_API_KEY": "broker", "CONTROL_PLANE_API_KEY": "native"},
            "typed",
        ),
        ("", {"CARDPUTER_TUNNEL_API_KEY": "broker", "CONTROL_PLANE_API_KEY": "native"}, "broker"),
        ("", {"CARDPUTER_TUNNEL_API_KEY": "", "CONTROL_PLANE_API_KEY": "native"}, "native"),
        ("", {}, ""),
    ],
)
def test_runtime_precedence(supplied, env, expected):
    assert runtime_api_key(supplied, env) == expected


def test_only_selected_key_reaches_child():
    env = child_environment(
        {
            "CONTROL_PLANE_API_KEY": "dummy",
            "OPENAI_API_KEY": "unrelated",
            "CARDPUTER_LOCAL_TOKEN": "operator",
        },
        "",
        "Bearer " + "a" * 64,
    )
    assert env == {
        "CARDPUTER_TUNNEL_API_KEY": "dummy",
        "CARDPUTER_TUNNEL_BEARER": "Bearer " + "a" * 64,
    }


def test_invalid_environment_key_has_fixed_error():
    with pytest.raises(TunnelError) as error:
        child_environment({"CONTROL_PLANE_API_KEY": "dummy secret"}, "", "Bearer " + "a" * 64)
    assert str(error.value) == "invalid_runtime_key"


def test_console_consumes_both_runtime_environment_names(monkeypatch):
    from types import SimpleNamespace

    from cardputer_mcp import tunnel

    environment = {"CARDPUTER_TUNNEL_API_KEY": "selected", "CONTROL_PLANE_API_KEY": "fallback"}
    monkeypatch.setattr(tunnel, "os", SimpleNamespace(environ=environment))
    assert tunnel._consume_runtime_api_key() == "selected"
    assert environment == {}
