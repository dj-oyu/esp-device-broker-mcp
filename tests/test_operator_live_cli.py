"""Old CLI approval verbs are terminal and cannot read credentials or call HTTP."""

import sys

import pytest

from cardputer_mcp import cli


@pytest.mark.parametrize("command", ["approve"])
def test_old_command_is_retired_before_credentials_network_or_state(monkeypatch, capsys, command):
    monkeypatch.setattr(cli, "version", lambda _name: "test")

    def forbidden(*args, **kwargs):
        raise AssertionError("Retired commands must have no external effects")

    monkeypatch.setattr(cli, "load_environment_token", forbidden)
    monkeypatch.setattr(cli, "connect", forbidden)
    monkeypatch.setattr(cli, "load_settings", forbidden)
    monkeypatch.setattr(
        sys, "argv", ["broker", command, "--plan", "old-plan", "--attestation", "old"]
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert '"error": "product_approval_retired"' in capsys.readouterr().out


async def test_demo_requires_authenticated_clients_before_http():
    with pytest.raises(cli.AuthError, match="demo_requires_authenticated_clients"):
        await cli.demo("http://localhost/mcp")
