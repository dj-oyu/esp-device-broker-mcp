"""Synthetic env files and registries only; never the user's real key store or env file."""

import io
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cardputer_mcp import client_environment as environment
from cardputer_mcp import env_file_writer as writer
from cardputer_mcp.env_file_writer import EnvFileWriteError, check_env_file, prepare_env_write
from cardputer_mcp.key_operator import KeyOperatorError, run
from cardputer_mcp.key_registry import KeyRegistry

OLD = "cpk_" + "a" * 32 + "." + "b" * 64
NEW = "cpk_" + "c" * 32 + "." + "d" * 64
URL = "http://127.0.0.1:9123/mcp"


def write_private(path: Path, data: bytes | str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    if os.name == "posix":
        os.chmod(path, mode)
    return path


@pytest.fixture
def account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Same isolation as test_client_environment: only fixture .git markers count.
    real_lstat = Path.lstat

    def fixture_lstat(path: Path) -> os.stat_result:
        if path.name == ".git" and not path.parent.is_relative_to(tmp_path):
            raise FileNotFoundError
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fixture_lstat)
    module = tmp_path / "adapter" / "src" / "cardputer_mcp" / "client_environment.py"
    module.parent.mkdir(parents=True)
    module.touch()
    monkeypatch.setattr(environment, "__file__", str(module))
    home = tmp_path / "account"
    home.mkdir()
    monkeypatch.chdir(home)
    return home


@pytest.fixture
def env_path(account: Path) -> Path:
    return account / "private" / "client.env"


def listing(path: Path) -> list[str]:
    return sorted(entry.name for entry in path.parent.iterdir())


def assert_invalid(path: Path, code: str = "key_env_file_invalid") -> None:
    before = listing(path) if path.parent.exists() else []
    with pytest.raises(EnvFileWriteError, match=f"^{code}$"):
        prepare_env_write(path)
    with pytest.raises(EnvFileWriteError, match=f"^{code}$"):
        check_env_file(path)
    if path.parent.exists():
        assert listing(path) == before


# --- rendering -------------------------------------------------------------


def test_replaces_only_key_value_preserving_bom_crlf_comments_and_url(env_path: Path) -> None:
    original = (
        b"\xef\xbb\xbf# private client\r\n\r\n  # CARDPUTER_CLIENT_KEY=commented\r\n"
        + b"CARDPUTER_BROKER_URL="
        + URL.encode()
        + b"\r\nCARDPUTER_CLIENT_KEY="
        + OLD.encode()
        + b"\r\n# trailer\r\n"
    )
    write_private(env_path, original)
    prepare_env_write(env_path).commit(NEW)
    assert env_path.read_bytes() == original.replace(OLD.encode(), NEW.encode())
    loaded = environment.load_client_environment(env_path)
    assert (loaded.url, loaded.token) == (URL, NEW)
    assert listing(env_path) == ["client.env"]


def test_lf_file_without_trailing_newline_keeps_style(env_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD)
    prepare_env_write(env_path).commit(NEW)
    assert env_path.read_bytes() == ("CARDPUTER_CLIENT_KEY=" + NEW).encode()


@pytest.mark.parametrize(
    "original,expected",
    [
        (b"CARDPUTER_BROKER_URL=" + URL.encode() + b"\r\n", b"\r\n"),
        (b"# only a comment", b"\n"),
        (b"", b"\n"),
    ],
)
def test_adds_key_line_when_missing(env_path: Path, original: bytes, expected: bytes) -> None:
    write_private(env_path, original)
    prepare_env_write(env_path).commit(NEW)
    separator = expected if original and not original.endswith(b"\n") else b""
    assert (
        env_path.read_bytes()
        == original + separator + b"CARDPUTER_CLIENT_KEY=" + NEW.encode() + expected
    )
    assert environment.load_client_environment(env_path).token == NEW


def test_placeholder_key_from_documentation_is_replaced(env_path: Path) -> None:
    write_private(
        env_path,
        "CARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp\n"
        "CARDPUTER_CLIENT_KEY=REPLACE_WITH_ISSUED_LOCAL_DEVELOPMENT_KEY\n",
    )
    prepare_env_write(env_path).commit(NEW)
    assert environment.load_client_environment(env_path).token == NEW


# --- rejected targets ------------------------------------------------------


def test_rejects_missing_file_and_relative_path(env_path: Path) -> None:
    env_path.parent.mkdir()
    assert_invalid(env_path)
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD)
    assert_invalid(Path("private") / "client.env")


@pytest.mark.parametrize(
    "content",
    [
        "UNKNOWN=1\n",
        "CARDPUTER_CLIENT_KEY=" + OLD + "\nCARDPUTER_CLIENT_KEY=" + OLD + "\n",
        "CARDPUTER_BROKER_URL=https://example.invalid/mcp\n",
        "export CARDPUTER_CLIENT_KEY=" + OLD + "\n",
        "CARDPUTER_CLIENT_KEY=" + OLD + "\rCARDPUTER_BROKER_URL=" + URL + "\n",
    ],
)
def test_rejects_invalid_existing_content(env_path: Path, content: str) -> None:
    write_private(env_path, content)
    assert_invalid(env_path)
    assert env_path.read_bytes() == content.encode()


def test_rejects_oversized_file(env_path: Path) -> None:
    write_private(env_path, "#" * 8200 + "\nCARDPUTER_CLIENT_KEY=" + OLD + "\n")
    assert_invalid(env_path)


def test_rejects_file_that_would_grow_past_the_reader_limit(env_path: Path) -> None:
    filler = "#" * (8192 - 1)
    write_private(env_path, filler + "\n")
    assert_invalid(env_path)


def test_rejects_multiply_linked_file(env_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    os.link(env_path, env_path.with_name("other.env"))
    assert_invalid(env_path)


def test_rejects_symbolic_link(env_path: Path) -> None:
    target = write_private(env_path.with_name("real.env"), "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    try:
        env_path.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links unavailable to this account")
    assert_invalid(env_path)
    assert target.read_bytes() == ("CARDPUTER_CLIENT_KEY=" + OLD + "\n").encode()


def test_rejects_path_inside_git_checkout(account: Path) -> None:
    checkout = account / "checkout"
    (checkout / ".git").mkdir(parents=True)
    path = write_private(checkout / "client.env", "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    assert_invalid(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows read-only attribute")
def test_rejects_read_only_attribute(env_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    os.chmod(env_path, stat.S_IREAD)
    try:
        assert_invalid(env_path, "key_env_file_not_writable")
    finally:
        os.chmod(env_path, stat.S_IREAD | stat.S_IWRITE)


def test_expected_key_id_refuses_a_file_holding_another_valid_key(env_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    with pytest.raises(EnvFileWriteError, match="^key_env_file_holds_other_key$"):
        check_env_file(env_path, expected_key_id="cpk_" + "e" * 32)
    check_env_file(env_path, expected_key_id=OLD.partition(".")[0])


# --- replacement safety ------------------------------------------------------


def test_target_changed_after_prepare_is_not_replaced(env_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    prepared = prepare_env_write(env_path)
    assert len(listing(env_path)) == 2
    env_path.unlink()
    write_private(env_path, "# swapped\nCARDPUTER_CLIENT_KEY=" + OLD + "\n")
    with pytest.raises(EnvFileWriteError, match="^key_env_file_changed$"):
        prepared.commit(NEW)
    assert env_path.read_bytes() == ("# swapped\nCARDPUTER_CLIENT_KEY=" + OLD + "\n").encode()
    assert listing(env_path) == ["client.env"]


def test_swap_failure_cleans_up_and_keeps_original(
    env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ("CARDPUTER_CLIENT_KEY=" + OLD + "\n").encode()
    write_private(env_path, original)

    def failing_swap(temporary: Path, path: Path) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(writer, "_swap", failing_swap)
    prepared = prepare_env_write(env_path)
    with pytest.raises(EnvFileWriteError, match="^key_env_file_replace_failed$"):
        prepared.commit(NEW)
    assert env_path.read_bytes() == original
    assert listing(env_path) == ["client.env"]


def test_discard_removes_unused_temporary_file(env_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    prepared = prepare_env_write(env_path)
    assert len(listing(env_path)) == 2
    assert "client.env" not in repr(prepared)
    prepared.discard()
    assert listing(env_path) == ["client.env"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")
@pytest.mark.parametrize("mode", [0o400, 0o600])
def test_posix_mode_preserved(env_path: Path, mode: int) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n", mode)
    prepare_env_write(env_path).commit(NEW)
    info = env_path.stat()
    assert stat.S_IMODE(info.st_mode) == mode
    assert info.st_uid == os.geteuid()


def _sddl(path: Path) -> str:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    information = 0x1 | 0x4  # OWNER | DACL
    needed = wintypes.DWORD(0)
    advapi32.GetFileSecurityW(str(path), information, None, 0, ctypes.byref(needed))
    buffer = ctypes.create_string_buffer(needed.value)
    assert advapi32.GetFileSecurityW(
        str(path), information, buffer, needed, ctypes.byref(needed)
    ), ctypes.get_last_error()
    text = wintypes.LPWSTR()
    assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
        buffer, 1, information, ctypes.byref(text), None
    ), ctypes.get_last_error()
    try:
        return str(text.value)
    finally:
        kernel32.LocalFree(text)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL preservation")
def test_windows_explicit_acl_preserved_after_replace(env_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    user = os.environ["USERNAME"]
    subprocess.run(
        ["icacls", str(env_path), "/inheritance:r", "/grant:r", f"{user}:(R,W,D)"],
        check=True,
        capture_output=True,
    )
    before = _sddl(env_path)
    assert "D:P" in before  # protected, explicit DACL unlike the directory's
    temporary = prepare_env_write(env_path)
    temp_files = [name for name in listing(env_path) if name != "client.env"]
    assert _sddl(env_path.parent / temp_files[0]) != before
    temporary.commit(NEW)
    assert _sddl(env_path) == before
    assert environment.load_client_environment(env_path).token == NEW
    assert listing(env_path) == ["client.env"]


# --- operator flows ----------------------------------------------------------


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class Factory:
    def __init__(self) -> None:
        self.counter = 0

    def random(self, size: int) -> bytes:
        self.counter += 1
        return self.counter.to_bytes(size, "big")

    def __call__(self, path: Path, *, create: bool = True) -> KeyRegistry:
        return KeyRegistry(
            path, clock=lambda: 1_700_000_000.0, random_bytes=self.random, create=create
        )


def confirmed(exact: str) -> Callable[[str], str]:
    return lambda prompt: exact


def options(factory: Factory, output: io.StringIO | None = None) -> dict[str, Any]:
    return {
        "input_stream": Terminal(),
        "output_stream": Terminal() if output is None else output,
        "registry_factory": factory,
    }


def registry_keys(path: Path, factory: Factory) -> dict[str, Any]:
    registry = factory(path, create=False)
    try:
        return {key.key_id: key for key in registry.list_keys()}
    finally:
        registry.close()


def authenticates(path: Path, factory: Factory, token: str) -> bool:
    registry = factory(path, create=False)
    try:
        return registry.authenticate("Bearer " + token) is not None
    finally:
        registry.close()


@pytest.fixture
def keys_path(tmp_path: Path) -> Path:
    path = tmp_path / "state" / "keys.sqlite3"
    path.parent.mkdir()
    return path


def issue_displayed(keys_path: Path, factory: Factory) -> tuple[str, str]:
    output = Terminal()
    result = run(
        "issue",
        keys_path,
        label="client",
        confirm=confirmed("ISSUE"),
        **options(factory, output),
    )
    token = output.getvalue().rpartition("API key (shown once): ")[2].strip()
    return str(result["key"]["key_id"]), token  # type: ignore[index]


def test_issue_writes_env_without_displaying_token(env_path: Path, keys_path: Path) -> None:
    write_private(env_path, "CARDPUTER_BROKER_URL=" + URL + "\n")
    factory = Factory()
    output = Terminal()
    result = run(
        "issue",
        keys_path,
        label="client",
        confirm=confirmed("ISSUE"),
        write_env=env_path,
        **options(factory, output),
    )
    assert result["token_shown"] is False and result["env_file_updated"] is True
    token = environment.load_client_environment(env_path).token
    assert token not in output.getvalue() and token.partition(".")[2] not in output.getvalue()
    assert str(env_path) not in output.getvalue()
    assert "not be displayed" in output.getvalue()
    assert authenticates(keys_path, factory, token)
    assert listing(env_path) == ["client.env"]


def test_rotate_writes_env_and_revokes_predecessor(env_path: Path, keys_path: Path) -> None:
    factory = Factory()
    key_id, old = issue_displayed(keys_path, factory)
    write_private(env_path, "# mine\nCARDPUTER_CLIENT_KEY=" + old + "\n")
    output = Terminal()
    result = run(
        "rotate",
        keys_path,
        key_id=key_id,
        confirm=confirmed(f"ROTATE {key_id}"),
        write_env=env_path,
        **options(factory, output),
    )
    assert result["token_shown"] is False and result["env_file_updated"] is True
    token = environment.load_client_environment(env_path).token
    assert token != old and token not in output.getvalue()
    assert env_path.read_bytes().startswith(b"# mine\n")
    assert authenticates(keys_path, factory, token)
    assert not authenticates(keys_path, factory, old)


def test_rotate_refuses_env_file_of_another_key_before_any_change(
    env_path: Path, keys_path: Path
) -> None:
    factory = Factory()
    key_id, old = issue_displayed(keys_path, factory)
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    asked: list[str] = []
    with pytest.raises(KeyOperatorError, match="^key_env_file_holds_other_key$"):
        run(
            "rotate",
            keys_path,
            key_id=key_id,
            confirm=lambda prompt: asked.append(prompt) or "",
            write_env=env_path,
            **options(factory),
        )
    assert asked == []
    assert authenticates(keys_path, factory, old)


def test_invalid_env_file_fails_before_confirmation_or_database(
    env_path: Path, keys_path: Path
) -> None:
    write_private(env_path, "UNKNOWN=1\n")
    asked: list[str] = []
    with pytest.raises(KeyOperatorError, match="^key_env_file_invalid$"):
        run(
            "issue",
            keys_path,
            label="client",
            confirm=lambda prompt: asked.append(prompt) or "ISSUE",
            write_env=env_path,
            **options(Factory()),
        )
    assert asked == [] and not keys_path.exists()


def test_issue_denial_with_env_touches_nothing(env_path: Path, keys_path: Path) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    with pytest.raises(KeyOperatorError, match="^key_action_cancelled$"):
        run(
            "issue",
            keys_path,
            label="client",
            confirm=confirmed("no"),
            write_env=env_path,
            **options(Factory()),
        )
    assert not keys_path.exists()
    assert listing(env_path) == ["client.env"]
    assert environment.load_client_environment(env_path).token == OLD


@pytest.mark.parametrize("command", ["list", "verify", "revoke"])
def test_write_env_rejected_for_other_commands(
    env_path: Path, keys_path: Path, command: Any
) -> None:
    with pytest.raises(KeyOperatorError, match="^key_write_env_not_supported$"):
        run(command, keys_path, key_id="cpk_" + "0" * 32, write_env=env_path, **options(Factory()))


def _fail_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_swap(temporary: Path, path: Path) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(writer, "_swap", failing_swap)


def test_issue_write_failure_after_commit_revokes_new_key(
    env_path: Path, keys_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")
    _fail_swap(monkeypatch)
    factory = Factory()
    output = Terminal()
    with pytest.raises(KeyOperatorError, match="^key_env_write_failed_new_key_revoked$"):
        run(
            "issue",
            keys_path,
            label="client",
            confirm=confirmed("ISSUE"),
            write_env=env_path,
            **options(factory, output),
        )
    keys = registry_keys(keys_path, factory)
    assert len(keys) == 1 and all(key.revoked_at is not None for key in keys.values())
    assert "cpk_" not in output.getvalue().replace("CARDPUTER_CLIENT_KEY", "")
    assert environment.load_client_environment(env_path).token == OLD
    assert listing(env_path) == ["client.env"]


def test_rotate_write_failure_falls_back_to_one_time_display(
    env_path: Path, keys_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = Factory()
    key_id, old = issue_displayed(keys_path, factory)
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + old + "\n")
    _fail_swap(monkeypatch)
    output = Terminal()
    result = run(
        "rotate",
        keys_path,
        key_id=key_id,
        confirm=confirmed(f"ROTATE {key_id}"),
        write_env=env_path,
        **options(factory, output),
    )
    assert result["token_shown"] is True and result["env_file_updated"] is False
    text = output.getvalue()
    assert "NOT updated" in text
    token = text.rpartition("API key (shown once): ")[2].strip()
    assert text.count(token) == 1
    assert authenticates(keys_path, factory, token)
    assert not authenticates(keys_path, factory, old)
    assert environment.load_client_environment(env_path).token == old
    assert listing(env_path) == ["client.env"]


class FailingDisplay(Terminal):
    def write(self, text: str) -> int:
        if "API key (shown once)" in text:
            raise OSError("synthetic")
        return super().write(text)


def test_rotate_write_and_display_failure_revokes_new_key(
    env_path: Path, keys_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = Factory()
    key_id, old = issue_displayed(keys_path, factory)
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + old + "\n")
    _fail_swap(monkeypatch)
    with pytest.raises(
        KeyOperatorError,
        match="^key_env_write_and_display_failed_new_key_revoked_run_keys_issue$",
    ):
        run(
            "rotate",
            keys_path,
            key_id=key_id,
            confirm=confirmed(f"ROTATE {key_id}"),
            write_env=env_path,
            **options(factory, FailingDisplay()),
        )
    keys = registry_keys(keys_path, factory)
    assert len(keys) == 2 and all(key.revoked_at is not None for key in keys.values())
    assert listing(env_path) == ["client.env"]


def test_interrupted_write_revokes_new_key_and_propagates(
    env_path: Path, keys_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_private(env_path, "CARDPUTER_CLIENT_KEY=" + OLD + "\n")

    def interrupted_swap(temporary: Path, path: Path) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(writer, "_swap", interrupted_swap)
    factory = Factory()
    with pytest.raises(KeyboardInterrupt):
        run(
            "issue",
            keys_path,
            label="client",
            confirm=confirmed("ISSUE"),
            write_env=env_path,
            **options(factory),
        )
    keys = registry_keys(keys_path, factory)
    assert all(key.revoked_at is not None for key in keys.values())
    assert listing(env_path) == ["client.env"]


# --- CLI ----------------------------------------------------------------------


def test_cli_passes_write_env_and_prints_fixed_error_codes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from cardputer_mcp import cli

    seen: dict[str, Any] = {}

    def fake_manage(command: str, path: Path | None, **kwargs: Any) -> dict[str, object]:
        seen.update(kwargs, command=command)
        raise KeyOperatorError("key_env_write_failed_new_key_revoked")

    target = tmp_path / "client.env"
    monkeypatch.setattr(cli, "manage_keys", fake_manage)
    monkeypatch.setattr(
        "sys.argv",
        ["cardputer", "keys", "rotate", "--key-id", "cpk_" + "0" * 32, "--write-env", str(target)],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    assert seen["command"] == "rotate" and seen["write_env"] == target
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Request failed: key_env_write_failed_new_key_revoked\n"


@pytest.mark.skipif(sys.platform != "win32", reason="ReplaceFileW error handling")
def test_windows_replace_error_keeps_original(
    env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ("CARDPUTER_CLIENT_KEY=" + OLD + "\n").encode()
    write_private(env_path, original)
    monkeypatch.setattr(writer, "_replace_file", lambda replaced, replacement, backup: 5)
    with pytest.raises(EnvFileWriteError, match="^key_env_file_replace_failed$"):
        prepare_env_write(env_path).commit(NEW)
    assert env_path.read_bytes() == original
    assert listing(env_path) == ["client.env"]


@pytest.mark.skipif(sys.platform != "win32", reason="ReplaceFileW error handling")
def test_windows_original_left_as_backup_is_reported_and_new_key_revoked(
    env_path: Path, keys_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ("CARDPUTER_CLIENT_KEY=" + OLD + "\n").encode()
    write_private(env_path, original)

    def renamed_only(replaced: Path, replacement: Path, backup: Path) -> int:
        os.rename(replaced, backup)
        return 1177  # ERROR_UNABLE_TO_MOVE_REPLACEMENT_2

    monkeypatch.setattr(writer, "_replace_file", renamed_only)
    factory = Factory()
    with pytest.raises(
        KeyOperatorError, match="^key_env_write_failed_new_key_revoked_original_kept_as_backup$"
    ):
        run(
            "issue",
            keys_path,
            label="client",
            confirm=confirmed("ISSUE"),
            write_env=env_path,
            **options(factory),
        )
    assert all(key.revoked_at is not None for key in registry_keys(keys_path, factory).values())
    (backup,) = env_path.parent.iterdir()
    assert backup.name.startswith(".client.env.") and backup.name.endswith(".bak")
    assert backup.read_bytes() == original
