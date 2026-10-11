"""Authentication tests use synthetic tokens and isolated local files only."""

import json
import os
import stat
from pathlib import Path

import pytest

from cardputer_mcp import auth
from cardputer_mcp.auth import AuthError, AuthState, load_client_token, load_environment_token
from cardputer_mcp.models import Origin

LOCAL = "a" * 64
REMOTE = "b" * 64
TOKENS = {Origin.LOCAL: LOCAL, Origin.REMOTE: REMOTE}


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    if os.name != "posix":
        pytest.skip("Private runtime-file tests require POSIX permissions")
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return directory / "runtime-auth.json"


def test_auth_generation_requires_explicit_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[int] = []

    def synthetic_random(size: int) -> str:
        requests.append(size)
        return LOCAL if len(requests) == 1 else REMOTE

    monkeypatch.setattr(auth.secrets, "token_hex", synthetic_random)
    with pytest.raises(AuthError, match="^real_auth_not_enabled$"):
        AuthState()
    assert requests == []
    state = AuthState(enable_real_auth=True)
    assert requests == [32, 32]
    assert state.authenticate(f"Bearer {LOCAL}") == Origin.LOCAL
    assert state.authenticate(f"Bearer {REMOTE}") == Origin.REMOTE


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        LOCAL,
        f"bearer {LOCAL}",
        f"Bearer  {LOCAL}",
        f"Bearer {LOCAL} ",
        f"Bearer {LOCAL}\n",
        f"Bearer {LOCAL}\r\n",
        f"Bearer {LOCAL.upper()}",
        "Bearer " + "c" * 64,
        "Bearer " + "a" * 63,
        "Bearer " + "a" * 65,
        "Bearer " + "é" * 64,
    ],
)
def test_auth_requires_exact_bearer_header(header: str | None) -> None:
    assert AuthState(TOKENS).authenticate(header) is None


def test_auth_compares_both_identities_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    state = AuthState(TOKENS)
    original = auth.secrets.compare_digest
    comparisons: list[tuple[str, str]] = []

    def compare(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return original(left, right)

    monkeypatch.setattr(auth.secrets, "compare_digest", compare)
    assert state.authenticate(f"Bearer {LOCAL}") == Origin.LOCAL
    assert comparisons == [(LOCAL, LOCAL), (LOCAL, REMOTE)]
    comparisons.clear()
    assert state.authenticate(f"Bearer {REMOTE}") == Origin.REMOTE
    assert comparisons == [(REMOTE, LOCAL), (REMOTE, REMOTE)]


@pytest.mark.parametrize(
    "tokens",
    [
        {},
        {Origin.LOCAL: LOCAL},
        {Origin.LOCAL: LOCAL, Origin.REMOTE: LOCAL},
        {Origin.LOCAL: "synthetic-secret-invalid", Origin.REMOTE: REMOTE},
    ],
)
def test_auth_rejects_invalid_token_maps_without_echo(tokens: dict[Origin, str]) -> None:
    with pytest.raises(AuthError) as exc:
        AuthState(tokens)
    assert str(exc.value) == "invalid_runtime_credentials"
    assert "synthetic-secret-invalid" not in repr(exc.value)


def test_auth_repr_hides_tokens() -> None:
    rendered = repr(AuthState(TOKENS))
    assert LOCAL not in rendered
    assert REMOTE not in rendered
    assert rendered == "AuthState(<redacted>)"


def test_runtime_round_trip_private_permissions_and_cleanup(runtime: Path) -> None:
    state = AuthState(TOKENS)
    state.write_runtime(runtime)
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o600
    assert stat.S_IMODE(runtime.parent.stat().st_mode) == 0o700
    assert load_client_token(runtime, Origin.LOCAL) == LOCAL
    assert load_client_token(runtime, Origin.REMOTE) == REMOTE
    assert state.close_runtime(runtime) is True
    assert not runtime.exists()
    assert state.close_runtime(runtime) is False


def test_runtime_new_epoch_replaces_stale_file_atomically(runtime: Path) -> None:
    old = AuthState(TOKENS)
    old.write_runtime(runtime)
    previous = runtime.read_bytes()
    new = AuthState({Origin.LOCAL: "c" * 64, Origin.REMOTE: "d" * 64})
    new.write_runtime(runtime)
    assert runtime.read_bytes() != previous
    assert load_client_token(runtime, Origin.LOCAL) == "c" * 64
    assert old.close_runtime(runtime) is False
    assert runtime.exists()
    assert new.close_runtime(runtime) is True


def test_runtime_cleanup_preserves_unrelated_epoch_version(runtime: Path) -> None:
    state = AuthState(TOKENS)
    state.write_runtime(runtime)
    payload = json.loads(runtime.read_text())
    payload["epoch"] = "f" * 32
    runtime.write_text(json.dumps(payload))
    assert state.close_runtime(runtime) is False
    payload["version"] = 999
    runtime.write_text(json.dumps(payload))
    assert state.close_runtime(runtime) is False
    assert runtime.exists()


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o770, 0o777])
def test_runtime_rejects_nonprivate_directory(runtime: Path, mode: int) -> None:
    runtime.parent.chmod(mode)
    with pytest.raises(AuthError, match="^unsafe_runtime_directory$"):
        AuthState(TOKENS).write_runtime(runtime)
    assert not runtime.exists()


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o666])
def test_runtime_rejects_nonprivate_file(runtime: Path, mode: int) -> None:
    state = AuthState(TOKENS)
    state.write_runtime(runtime)
    runtime.chmod(mode)
    with pytest.raises(AuthError, match="^unsafe_runtime_file$"):
        load_client_token(runtime, Origin.LOCAL)
    with pytest.raises(AuthError, match="^unsafe_runtime_file$"):
        state.write_runtime(runtime)
    assert state.close_runtime(runtime) is False
    assert runtime.exists()


def test_runtime_rejects_symlink_file_without_touching_target(runtime: Path) -> None:
    target = runtime.parent / "target"
    target.write_text("synthetic untouched")
    target.chmod(0o600)
    runtime.symlink_to(target)
    state = AuthState(TOKENS)
    with pytest.raises(AuthError):
        state.write_runtime(runtime)
    with pytest.raises(AuthError):
        load_client_token(runtime, Origin.LOCAL)
    assert state.close_runtime(runtime) is False
    assert target.read_text() == "synthetic untouched"


def test_runtime_rejects_symlink_ancestor(runtime: Path, tmp_path: Path) -> None:
    link = tmp_path / "linked"
    link.symlink_to(runtime.parent, target_is_directory=True)
    with pytest.raises(AuthError, match="^unsafe_runtime_path$"):
        AuthState(TOKENS).write_runtime(link / "runtime-auth.json")
    assert not runtime.exists()


def test_runtime_rejects_hardlinked_file(runtime: Path) -> None:
    state = AuthState(TOKENS)
    state.write_runtime(runtime)
    os.link(runtime, runtime.parent / "other")
    with pytest.raises(AuthError, match="^unsafe_runtime_file$"):
        load_client_token(runtime, Origin.LOCAL)
    assert state.close_runtime(runtime) is False


def test_runtime_bounded_read_and_malformed_json(runtime: Path) -> None:
    runtime.write_bytes(b"x" * 4097)
    runtime.chmod(0o600)
    with pytest.raises(AuthError, match="^invalid_runtime_credentials$"):
        load_client_token(runtime, Origin.LOCAL)
    runtime.write_text('{"password":"synthetic-do-not-echo"')
    with pytest.raises(AuthError) as exc:
        load_client_token(runtime, Origin.LOCAL)
    assert str(exc.value) == "invalid_runtime_credentials"


def test_runtime_wrong_file_name_and_parent_traversal_rejected(runtime: Path) -> None:
    state = AuthState(TOKENS)
    with pytest.raises(AuthError, match="^unsafe_runtime_path$"):
        state.write_runtime(runtime.with_name("other.json"))
    with pytest.raises(AuthError, match="^unsafe_runtime_path$"):
        state.write_runtime(runtime.parent / ".." / "state" / "runtime-auth.json")


def test_runtime_missing_file_is_fixed_error(runtime: Path) -> None:
    with pytest.raises(AuthError, match="^runtime_credentials_unavailable$"):
        load_client_token(runtime, Origin.LOCAL)


def test_runtime_windows_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = tmp_path / "runtime-auth.json"
    state = AuthState(TOKENS)
    monkeypatch.setattr(auth.os, "name", "nt")
    with pytest.raises(AuthError, match="^private_runtime_credentials_unsupported$"):
        state.write_runtime(runtime)
    with pytest.raises(AuthError, match="^private_runtime_credentials_unsupported$"):
        load_client_token(runtime, Origin.LOCAL)


def test_environment_auth_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", LOCAL)
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", REMOTE)
    with pytest.raises(AuthError, match="^real_auth_not_enabled$"):
        AuthState.from_environment()
    state = AuthState.from_environment(enable_real_auth=True)
    assert state.authenticate(f"Bearer {LOCAL}") == Origin.LOCAL
    assert state.authenticate(f"Bearer {REMOTE}") == Origin.REMOTE


def test_environment_client_needs_only_its_own_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", LOCAL)
    monkeypatch.delenv("CARDPUTER_REMOTE_TOKEN", raising=False)
    assert load_environment_token(Origin.LOCAL) == LOCAL
    with pytest.raises(AuthError, match="^runtime_environment_credentials_unavailable$"):
        load_environment_token(Origin.REMOTE)
    with pytest.raises(AuthError, match="^runtime_environment_credentials_unavailable$"):
        AuthState.from_environment(enable_real_auth=True)


def test_environment_rejects_invalid_or_duplicate_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", "synthetic-invalid-do-not-echo")
    monkeypatch.setenv("CARDPUTER_REMOTE_TOKEN", REMOTE)
    with pytest.raises(AuthError) as exc:
        AuthState.from_environment(enable_real_auth=True)
    assert str(exc.value) == "runtime_environment_credentials_unavailable"
    monkeypatch.setenv("CARDPUTER_LOCAL_TOKEN", REMOTE)
    with pytest.raises(AuthError, match="^invalid_runtime_credentials$"):
        AuthState.from_environment(enable_real_auth=True)
