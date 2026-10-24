"""Local handoff tests use synthetic keys and fake terminals, never real credentials."""

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cardputer_mcp import key_operator
from cardputer_mcp.key_operator import KeyOperatorError, run
from cardputer_mcp.key_registry import KeyRegistry, KeyRegistryError
from cardputer_mcp.models import Origin


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class Factory:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.random_sizes: list[int] = []
        self.counter = 0

    def random(self, size: int) -> bytes:
        self.random_sizes.append(size)
        self.counter += 1
        return self.counter.to_bytes(size, "big")

    def __call__(self, path: Path, *, create: bool = True) -> KeyRegistry:
        self.calls.append(create)
        return KeyRegistry(
            path, clock=lambda: 1_700_000_000.0, random_bytes=self.random, create=create
        )


def parameters(factory: Factory, output: Terminal | None = None) -> dict[str, Any]:
    return {
        "input_stream": Terminal(),
        "output_stream": Terminal() if output is None else output,
        "registry_factory": factory,
    }


def confirmed(exact: str) -> Callable[[str], str]:
    return lambda prompt: exact


def _interrupted(prompt: str) -> str:
    raise KeyboardInterrupt


@pytest.mark.parametrize(
    "confirm",
    [confirmed("no"), confirmed("issue"), confirmed("ISSUE "), confirmed(""), _interrupted],
)
def test_issue_denial_creates_no_database_or_key(
    tmp_path: Path, confirm: Callable[[str], str]
) -> None:
    factory = Factory()
    path = tmp_path / "keys.sqlite3"
    with pytest.raises(KeyOperatorError, match="^key_action_cancelled$"):
        run("issue", path, label="remote", confirm=confirm, **parameters(factory))
    assert not path.exists()
    assert factory.calls == []
    assert factory.random_sizes == []


@pytest.mark.parametrize("input_tty,output_tty", [(False, True), (True, False)])
@pytest.mark.parametrize("command", ["issue", "verify"])
def test_piped_sensitive_commands_rejected(
    tmp_path: Path, input_tty: bool, output_tty: bool, command: key_operator.KeyCommand
) -> None:
    factory = Factory()
    incoming = Terminal() if input_tty else io.StringIO()
    outgoing = Terminal() if output_tty else io.StringIO()
    with pytest.raises(KeyOperatorError, match="^key_interactive_terminal_required$"):
        run(
            command,
            tmp_path / "keys.sqlite3",
            label="remote",
            confirm=confirmed("ISSUE"),
            input_stream=incoming,
            output_stream=outgoing,
            registry_factory=factory,
        )
    assert factory.calls == []
    assert factory.random_sizes == []


def test_issue_discloses_scope_ttl_and_warning_before_generating(tmp_path: Path) -> None:
    factory = Factory()
    output = Terminal()
    path = tmp_path / "keys.sqlite3"

    def approve(prompt: str) -> str:
        assert "Type ISSUE" in prompt
        assert "Scopes: read, control" in output.getvalue()
        assert "Origin: local_cli" in output.getvalue()
        assert "Expires after: 10 seconds" in output.getvalue()
        assert "scrollback" in output.getvalue() and "recording" in output.getvalue()
        assert factory.calls == [] and factory.random_sizes == []
        assert not path.exists()
        return "ISSUE"

    result = run(
        "issue",
        path,
        label="operator",
        origin=Origin.LOCAL,
        control=True,
        ttl_seconds=10,
        confirm=approve,
        **parameters(factory, output),
    )
    assert result["token_shown"] is True
    assert result["key"]["origin"] == "local_cli"  # type: ignore[index]
    assert result["key"]["scopes"] == ["control", "read"]  # type: ignore[index]
    token = output.getvalue().split("API key (shown once): ")[1].strip()
    assert output.getvalue().count(token) == 1
    assert token not in repr(result) and token not in json.dumps(result)
    assert factory.calls == [True] and factory.random_sizes == [16, 32]
    registry = factory(path, create=False)
    assert registry.authenticate("Bearer " + token) is not None
    assert token.encode() not in path.read_bytes()


def test_default_issue_is_remote_read_one_hour(tmp_path: Path) -> None:
    factory = Factory()
    output = Terminal()
    result = run(
        "issue",
        tmp_path / "keys.sqlite3",
        label="remote",
        confirm=confirmed("ISSUE"),
        **parameters(factory, output),
    )
    metadata = result["key"]
    assert isinstance(metadata, dict)
    assert metadata["origin"] == "remote_mcp"
    assert metadata["scopes"] == ["read"]
    assert metadata["expires_at"] - metadata["issued_at"] == 3600


@pytest.mark.parametrize("command", ["list", "rotate"])
def test_missing_database_never_created(tmp_path: Path, command: key_operator.KeyCommand) -> None:
    factory = Factory()
    path = tmp_path / "absent.sqlite3"
    with pytest.raises(KeyRegistryError, match="^key_registry_unavailable$"):
        run(
            command,
            path,
            key_id="cpk_" + "a" * 32,
            confirm=confirmed("ISSUE"),
            **parameters(factory),
        )
    assert not path.exists()
    assert factory.random_sizes == []
    assert factory.calls == [False]


def test_list_and_verify_only_return_safe_metadata(tmp_path: Path) -> None:
    factory = Factory()
    path = tmp_path / "keys.sqlite3"
    registry = factory(path)
    key = registry.issue("remote", Origin.REMOTE)
    output = Terminal()
    listed = run(
        "list",
        path,
        input_stream=io.StringIO(),
        output_stream=io.StringIO(),
        registry_factory=factory,
    )
    result = run(
        "verify", path, secret_input=lambda prompt: key.token, **parameters(factory, output)
    )
    assert result["valid"] is True
    assert result["principal"]["key_id"] == key.metadata.key_id  # type: ignore[index]
    assert result["principal"]["origin"] == "remote_mcp"  # type: ignore[index]
    assert key.token not in json.dumps(listed) + json.dumps(result) + output.getvalue()
    assert factory.random_sizes == [16, 32]


def test_invalid_verify_does_not_echo_input(tmp_path: Path) -> None:
    factory = Factory()
    path = tmp_path / "keys.sqlite3"
    factory(path).close()
    output = Terminal()
    private = "synthetic-secret-invalid"
    result = run("verify", path, secret_input=lambda prompt: private, **parameters(factory, output))
    assert result == {"valid": False}
    assert private not in repr(result) + output.getvalue()


def test_getpass_echo_fallback_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import warnings

    factory = Factory()
    path = tmp_path / "keys.sqlite3"
    factory(path).close()

    def echo_fallback(prompt: str, stream: object) -> str:
        warnings.warn("synthetic echo fallback", key_operator.getpass.GetPassWarning, stacklevel=2)
        pytest.fail("Fallback must abort before reading echoing input")

    monkeypatch.setattr(key_operator.getpass, "getpass", echo_fallback)
    with pytest.raises(KeyOperatorError, match="^key_hidden_input_unavailable$"):
        run("verify", path, **parameters(factory))


def test_rotate_discloses_inherited_scope_and_revokes_only_after_confirmation(
    tmp_path: Path,
) -> None:
    factory = Factory()
    path = tmp_path / "keys.sqlite3"
    registry = factory(path)
    old = registry.issue("controller", Origin.REMOTE, scopes={"control"})
    output = Terminal()

    def approve(prompt: str) -> str:
        assert "Scopes: read, control" in output.getvalue()
        assert "Expires after: 20 seconds" in output.getvalue()
        assert "revoked immediately" in output.getvalue()
        assert registry.authenticate("Bearer " + old.token) is not None
        assert factory.random_sizes == [16, 32]
        return "ROTATE " + old.metadata.key_id

    result = run(
        "rotate",
        path,
        key_id=old.metadata.key_id,
        ttl_seconds=20,
        confirm=approve,
        **parameters(factory, output),
    )
    new_token = output.getvalue().split("API key (shown once): ")[1].strip()
    assert output.getvalue().count(new_token) == 1
    assert new_token not in repr(result)
    assert old.token not in output.getvalue()
    assert registry.authenticate("Bearer " + old.token) is None
    assert registry.authenticate("Bearer " + new_token) is not None


def test_cancelled_rotation_keeps_old_key_and_generates_nothing(tmp_path: Path) -> None:
    factory = Factory()
    path = tmp_path / "keys.sqlite3"
    registry = factory(path)
    old = registry.issue("remote", Origin.REMOTE)
    with pytest.raises(KeyOperatorError, match="^key_action_cancelled$"):
        run(
            "rotate",
            path,
            key_id=old.metadata.key_id,
            confirm=confirmed("no"),
            **parameters(factory),
        )
    assert registry.authenticate("Bearer " + old.token) is not None
    assert registry.list_keys() == [old.metadata]
    assert factory.random_sizes == [16, 32]


def test_revoke_requires_exact_confirmation_and_returns_no_token(tmp_path: Path) -> None:
    factory = Factory()
    path = tmp_path / "keys.sqlite3"
    registry = factory(path)
    key = registry.issue("remote", Origin.REMOTE)
    with pytest.raises(KeyOperatorError, match="^key_action_cancelled$"):
        run(
            "revoke",
            path,
            key_id=key.metadata.key_id,
            confirm=confirmed("REVOKE"),
            **parameters(factory),
        )
    assert registry.authenticate("Bearer " + key.token) is not None
    output = Terminal()
    result = run(
        "revoke",
        path,
        key_id=key.metadata.key_id,
        confirm=confirmed("REVOKE " + key.metadata.key_id),
        **parameters(factory, output),
    )
    assert result == {"key_id": key.metadata.key_id, "revoked": True}
    assert registry.authenticate("Bearer " + key.token) is None
    assert key.token not in repr(result) + output.getvalue()


@pytest.mark.parametrize("partial", [False, True])
def test_failed_token_display_revokes_without_redisplay(tmp_path: Path, partial: bool) -> None:
    class FailingTerminal(Terminal):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def write(self, value: str) -> int:
            if value.startswith("API key (shown once): "):
                self.attempts += 1
                if partial:
                    return super().write(value[:10])
                raise OSError("synthetic terminal failure")
            return super().write(value)

    factory = Factory()
    output = FailingTerminal()
    path = tmp_path / "keys.sqlite3"
    with pytest.raises(KeyOperatorError, match="^key_display_failed_new_key_revoked$"):
        run(
            "issue", path, label="remote", confirm=confirmed("ISSUE"), **parameters(factory, output)
        )
    assert output.attempts == 1
    registry = factory(path, create=False)
    assert registry.list_keys()[0].revoked_at is not None
    assert "synthetic terminal failure" not in output.getvalue()


@pytest.mark.parametrize("ttl", [0])
def test_invalid_expiry_never_prompts_or_creates(tmp_path: Path, ttl: int) -> None:
    factory = Factory()
    output = Terminal()
    with pytest.raises(KeyOperatorError, match="^invalid_key_expiry$"):
        run(
            "issue",
            tmp_path / "keys.sqlite3",
            label="remote",
            ttl_seconds=ttl,
            confirm=confirmed("ISSUE"),
            **parameters(factory, output),
        )
    assert output.getvalue() == ""
    assert factory.calls == []


@pytest.mark.parametrize(
    ("command", "interrupt", "during"),
    [("issue", KeyboardInterrupt, "write"), ("rotate", SystemExit, "flush")],
)
def test_interrupted_token_display_revokes_before_propagating_without_secret(
    tmp_path: Path,
    command: key_operator.KeyCommand,
    interrupt: type[BaseException],
    during: str,
) -> None:
    class InterruptedTerminal(Terminal):
        def __init__(self) -> None:
            super().__init__()
            self.token_attempts = 0
            self.secret_line = ""

        def write(self, value: str) -> int:
            if value.startswith("API key (shown once): "):
                self.token_attempts += 1
                self.secret_line = value
                if during == "write":
                    raise interrupt(value)
            return super().write(value)

        def flush(self) -> None:
            if self.secret_line and during == "flush":
                raise interrupt(self.secret_line)
            super().flush()

    factory = Factory()
    output = InterruptedTerminal()
    path = tmp_path / "keys.sqlite3"
    identifier = None
    phrase = "ISSUE"
    if command == "rotate":
        old = factory(path).issue("remote", Origin.REMOTE)
        identifier = old.metadata.key_id
        phrase = "ROTATE " + identifier
    with pytest.raises(interrupt) as caught:
        run(
            command,
            path,
            label="remote",
            key_id=identifier,
            confirm=confirmed(phrase),
            **parameters(factory, output),
        )
    assert output.token_attempts == 1
    token = output.secret_line.removeprefix("API key (shown once): ").strip()
    assert token and token not in str(caught.value) + repr(caught.value)
    registry = factory(path, create=False)
    assert all(key.revoked_at is not None for key in registry.list_keys())
    assert registry.authenticate("Bearer " + token) is None
