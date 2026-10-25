"""CLI wiring has no implicit broker, file creation, credential arguments or stdout noise."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from cardputer_mcp import cli, client_environment, stdio_adapter
from cardputer_mcp.client_environment import ClientEnvironment, ClientEnvironmentError

TOKEN = "cpk_" + "a" * 32 + "." + "b" * 64
PRIVATE_PATH = "/synthetic-private-account/client.env"


def test_stdio_cli_loads_only_explicit_file_and_forwards_without_stdout(monkeypatch, capsys):
    load = Mock(return_value=ClientEnvironment(url="http://127.0.0.1:9876/mcp", token=TOKEN))
    run = AsyncMock()
    monkeypatch.setattr(client_environment, "load_client_environment", load)
    monkeypatch.setattr(stdio_adapter, "run_stdio", run)
    monkeypatch.setattr(
        cli, "create_app", Mock(side_effect=AssertionError("must not start broker"))
    )
    monkeypatch.setattr(sys, "argv", ["cardputer", "stdio", "--env-file", PRIVATE_PATH])
    cli.main()
    load.assert_called_once_with(Path(PRIVATE_PATH))
    # The same explicit file is the only source a key rejection may re-read.
    run.assert_awaited_once_with("http://127.0.0.1:9876/mcp", TOKEN, Path(PRIVATE_PATH))
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize(
    "flags",
    [
        ["--auth-source", "environment"],
        ["--url", "http://127.0.0.1:9876/mcp"],
    ],
)
def test_stdio_does_not_silently_merge_global_auth_or_url(monkeypatch, capsys, flags):
    load = Mock(side_effect=AssertionError("must reject before reading file"))
    monkeypatch.setattr(client_environment, "load_client_environment", load)
    monkeypatch.setattr(sys, "argv", ["cardputer", *flags, "stdio", "--env-file", PRIVATE_PATH])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert capsys.readouterr() == ("", "stdio_adapter_use_private_env_file_only\n")
    load.assert_not_called()


def test_stdio_bad_private_file_has_fixed_stderr_and_no_stdout(monkeypatch, capsys):
    monkeypatch.setattr(
        client_environment, "load_client_environment", Mock(side_effect=ClientEnvironmentError())
    )
    monkeypatch.setattr(sys, "argv", ["cardputer", "stdio", "--env-file", PRIVATE_PATH])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert capsys.readouterr() == ("", "invalid_client_environment\n")


def test_registration_examples_share_one_adapter_and_private_file():
    import tomllib

    root = Path(__file__).resolve().parents[1]
    claude = json.loads((root / "examples/claude-code.mcp.json").read_text())["mcpServers"][
        "device-broker"
    ]
    codex = tomllib.loads((root / "examples/codex-project-config.toml").read_text())["mcp_servers"][
        "device_broker"
    ]
    assert claude["type"] == "stdio"
    assert claude["command"] == codex["command"]
    assert claude["args"] == codex["args"]
    assert claude["args"][:4] == ["-m", "cardputer_mcp.cli", "stdio", "--env-file"]
    assert "env" not in claude and "env" not in codex
    assert "Bearer" not in json.dumps([claude, codex])
    assert "cpk_" not in json.dumps([claude, codex])
