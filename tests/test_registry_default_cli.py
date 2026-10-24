"""Default selection through installed entrypoint arguments; no real credentials."""

from unittest.mock import Mock

import pytest
from test_key_operator import Factory, confirmed, parameters

from cardputer_mcp import cli
from cardputer_mcp.config import Settings
from cardputer_mcp.key_operator import KeyOperatorError, run


def test_installed_serve_environment_needs_no_registry_flag(tmp_path, monkeypatch):
    settings = Settings(state_dir=tmp_path / "old-working-state")
    create = Mock(return_value=object())
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "create_app", create)
    monkeypatch.setattr(cli.uvicorn, "run", Mock())
    monkeypatch.setattr("sys.argv", ["cardputer.exe", "--auth-source", "environment", "serve"])
    cli.main()
    assert create.call_args.kwargs["key_registry_path"] is None
    assert create.call_args.kwargs["require_auth"] is True
    assert create.call_args.kwargs["auth_source"] == "environment"


def test_default_issue_denial_does_not_create_state(tmp_path, monkeypatch):
    path = tmp_path / "DeviceBroker" / "keys.sqlite3"
    monkeypatch.setattr("cardputer_mcp.key_operator.default_key_registry_path", lambda: path)
    factory = Factory()
    with pytest.raises(KeyOperatorError, match="key_action_cancelled"):
        run("issue", label="synthetic", confirm=confirmed("no"), **parameters(factory))
    assert not path.parent.exists() and not factory.calls
