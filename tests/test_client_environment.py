"""Synthetic private-file fixtures only; no user credentials or network access."""

import os
import stat
import traceback
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from cardputer_mcp import client_environment as environment

KEY = "cpk_" + "a" * 32 + "." + "b" * 64
ASSIGNMENT = "CARDPUTER_CLIENT_KEY=" + KEY


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    return path


@pytest.fixture
def layout(tmp_path, monkeypatch):
    # Model account/project directories independently of host checkout markers.
    # This execution sandbox itself has a /tmp/.git boundary; do not alter it.
    real_lstat = Path.lstat

    def fixture_lstat(path):
        if path.name == ".git" and not path.parent.is_relative_to(tmp_path):
            raise FileNotFoundError
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", fixture_lstat)
    project = tmp_path / "adapter"
    module = project / "src" / "cardputer_mcp" / "client_environment.py"
    module.parent.mkdir(parents=True)
    module.touch()
    monkeypatch.setattr(environment, "__file__", str(module))
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.chdir(account)
    return project, account


@pytest.fixture
def private_file(layout):
    return layout[1] / "private" / "client.env"


def assert_safe_failure(path):
    with pytest.raises(environment.ClientEnvironmentError) as error:
        environment.load_client_environment(path)
    assert str(error.value) == "invalid_client_environment"
    rendered = "".join(traceback.format_exception(error.value))
    assert str(path) not in str(error.value)
    assert str(path) not in repr(error.value)
    assert str(path) not in rendered
    assert KEY not in rendered


@pytest.mark.parametrize(
    "data",
    [
        ASSIGNMENT,
        ASSIGNMENT + "\n",
        "# Synthetic fixture\n\n" + ASSIGNMENT + "\n",
        "  # Comment\n \t\n" + ASSIGNMENT,
        "\ufeff# Windows fixture\r\n" + ASSIGNMENT + "\r\n",
    ],
)
def test_loads_minimal_file_and_windows_bom_crlf(private_file, data):
    result = environment.load_client_environment(write_private(private_file, data))
    assert result.url == "http://127.0.0.1:8765/mcp"
    assert result.token == KEY
    assert KEY not in repr(result)
    assert KEY not in str(result)
    with pytest.raises(FrozenInstanceError):
        result.token = "changed"


@pytest.mark.parametrize("port", [1024, 8765, 18765, 65535])
def test_exact_loopback_url(private_file, port):
    url = f"http://127.0.0.1:{port}/mcp"
    result = environment.load_client_environment(
        write_private(private_file, ASSIGNMENT + "\nCARDPUTER_BROKER_URL=" + url)
    )
    assert result.url == url


def test_no_environment_merge_or_modification(private_file, monkeypatch):
    values = {
        "CARDPUTER_CLIENT_KEY": "unrelated",
        "CARDPUTER_BROKER_URL": "https://example.invalid/mcp",
        "CARDPUTER_LOCAL_TOKEN": "1" * 64,
        "CARDPUTER_REMOTE_TOKEN": "2" * 64,
        "HTTP_PROXY": "http://example.invalid:9999",
        "HTTPS_PROXY": "http://example.invalid:9999",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    result = environment.load_client_environment(write_private(private_file, ASSIGNMENT))
    assert result.token == KEY
    assert result.url == "http://127.0.0.1:8765/mcp"
    assert all(os.environ[key] == value for key, value in values.items())


@pytest.mark.parametrize(
    "data",
    [
        "",
        "# comment only\n \t\n",
        "CARDPUTER_CLIENT_KEY=",
        "CARDPUTER_CLIENT_KEY=" + "a" * 64,
        "CARDPUTER_CLIENT_KEY=" + KEY.upper(),
        "CARDPUTER_CLIENT_KEY=" + KEY[:-1],
        "CARDPUTER_CLIENT_KEY=" + KEY + "0",
        "CARDPUTER_CLIENT_KEY='" + KEY + "'",
        'CARDPUTER_CLIENT_KEY="' + KEY + '"',
        ASSIGNMENT + " # inline comment",
        ASSIGNMENT + " ",
        " " + ASSIGNMENT,
        "CARDPUTER_CLIENT_KEY =" + KEY,
        "export " + ASSIGNMENT,
        "set " + ASSIGNMENT,
        "CARDPUTER_CLIENT_KEY=$TOKEN",
        "CARDPUTER_CLIENT_KEY=${TOKEN}",
        "CARDPUTER_CLIENT_KEY=$(echo token)",
        "CARDPUTER_CLIENT_KEY=`echo token`",
        "CARDPUTER_CLIENT_KEY=\\" + KEY,
        ASSIGNMENT + "; echo token",
        ASSIGNMENT + "\n" + ASSIGNMENT,
        ASSIGNMENT + "\nCARDPUTER_LOCAL_TOKEN=" + "a" * 64,
        ASSIGNMENT + "\nHTTP_PROXY=http://example.invalid:80",
        ASSIGNMENT + "\nUNRECOGNIZED=value",
        ASSIGNMENT + "\nCARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp\n"
        "CARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp",
        "CARDPUTER_BROKER_URL=http://127.0.0.1:8765/mcp",
        ASSIGNMENT + "\r",
        ASSIGNMENT + "\x00",
        ASSIGNMENT + "\v",
        ASSIGNMENT + "\f",
        ASSIGNMENT + "\x85",
        ASSIGNMENT + "\u2028",
        ASSIGNMENT + "\u2029",
        ASSIGNMENT + "\n\ufeff# second BOM",
        b"\xff\xfe" + ASSIGNMENT.encode("ascii"),
    ],
)
def test_rejects_nonliteral_or_malformed_files(private_file, data):
    assert_safe_failure(write_private(private_file, data))


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://127.0.0.1:0/mcp",
        "http://127.0.0.1:1023/mcp",
        "http://127.0.0.1:65536/mcp",
        "http://127.0.0.1:08765/mcp",
        "http://127.0.0.1:+8765/mcp",
        "http://127.0.0.1:8765.0/mcp",
        "http://127.0.0.1:８７６５/mcp",
        "http://127.0.0.1/mcp",
        "http://localhost:8765/mcp",
        "http://127.0.0.2:8765/mcp",
        "http://2130706433:8765/mcp",
        "http://[::1]:8765/mcp",
        "http://0.0.0.0:8765/mcp",
        "http://127.0.0.1.example.invalid:8765/mcp",
        "http://user@127.0.0.1:8765/mcp",
        "https://127.0.0.1:8765/mcp",
        "HTTP://127.0.0.1:8765/mcp",
        "http://127.0.0.1:8765/",
        "http://127.0.0.1:8765/mcp/",
        "http://127.0.0.1:8765/mcp?x=1",
        "http://127.0.0.1:8765/mcp#fragment",
        "http://127.0.0.1:8765/%6dcp",
        "http://127.0.0.1:8765/../mcp",
        "http://127.0.0.1:8765/mcp\\other",
        " http://127.0.0.1:8765/mcp",
    ],
)
def test_rejects_unsupported_urls(private_file, url):
    assert_safe_failure(write_private(private_file, ASSIGNMENT + "\nCARDPUTER_BROKER_URL=" + url))


def test_missing_file_is_safe(private_file):
    assert_safe_failure(private_file)


def test_relative_file_is_rejected(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    monkeypatch.chdir(private_file.parent)
    assert_safe_failure(Path(private_file.name))


def test_parent_traversal_is_rejected(private_file):
    write_private(private_file, ASSIGNMENT)
    assert_safe_failure(private_file.parent / ".." / "private" / private_file.name)


def test_directory_is_rejected(private_file):
    private_file.mkdir(parents=True)
    assert_safe_failure(private_file)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO")
def test_fifo_is_rejected_before_open(private_file, monkeypatch):
    private_file.parent.mkdir()
    os.mkfifo(private_file, 0o600)
    monkeypatch.setattr(environment, "_open_file", lambda _: pytest.fail("opened FIFO"))
    assert_safe_failure(private_file)


@pytest.mark.parametrize("extra", [0, 1])
def test_bounded_file_size(private_file, extra):
    data = ASSIGNMENT + "\n#" + "x" * (8192 - len(ASSIGNMENT) - 2 + extra)
    write_private(private_file, data)
    if extra:
        assert_safe_failure(private_file)
    else:
        assert environment.load_client_environment(private_file).token == KEY


@pytest.mark.parametrize("link_parent", [False, True])
def test_symlink_paths_rejected(private_file, link_parent):
    target = write_private(private_file, ASSIGNMENT)
    link = private_file.parent.parent / "linked"
    try:
        link.symlink_to(target.parent if link_parent else target, target_is_directory=link_parent)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert_safe_failure(link / target.name if link_parent else link)


def test_hard_link_rejected(private_file):
    write_private(private_file, ASSIGNMENT)
    try:
        os.link(private_file, private_file.with_name("second.env"))
    except (OSError, NotImplementedError):
        pytest.skip("hard links unavailable")
    assert_safe_failure(private_file)


@pytest.mark.parametrize("marker", ["directory", "worktree", "link"])
def test_git_checkout_files_rejected(private_file, marker):
    write_private(private_file, ASSIGNMENT)
    git = private_file.parent.parent / ".git"
    if marker == "directory":
        git.mkdir()
    elif marker == "worktree":
        git.write_text("gitdir: elsewhere", encoding="utf-8")
    else:
        try:
            git.symlink_to(git.with_name("missing-git-directory"))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
    assert_safe_failure(private_file)


def test_adapter_source_tree_rejected(layout):
    assert_safe_failure(write_private(layout[0] / "private" / "client.env", ASSIGNMENT))


def test_installed_adapter_tree_rejected(layout, monkeypatch):
    installed = layout[0] / "site-packages"
    module = installed / "cardputer_mcp" / "client_environment.py"
    module.parent.mkdir(parents=True)
    module.touch()
    monkeypatch.setattr(environment, "__file__", str(module))
    assert_safe_failure(write_private(installed / "client.env", ASSIGNMENT))


def test_enclosing_monorepo_rejected(layout):
    project, account = layout
    (project.parent / ".git").mkdir()
    assert_safe_failure(write_private(account / "client.env", ASSIGNMENT))


def test_external_file_allowed_when_cwd_is_inside_git(private_file, layout, monkeypatch):
    project, _ = layout
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    assert environment.load_client_environment(write_private(private_file, ASSIGNMENT)).token == KEY


def test_git_marker_added_during_read_rejected(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    real_read = environment._read_file

    def changed(path):
        result = real_read(path)
        (path.parent / ".git").mkdir()
        return result

    monkeypatch.setattr(environment, "_read_file", changed)
    assert_safe_failure(private_file)


def test_file_replacement_between_check_and_open_rejected(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    replacement = write_private(private_file.with_name("replacement.env"), ASSIGNMENT)
    real_open = environment._open_file

    def changed(path):
        os.replace(replacement, path)
        return real_open(path)

    monkeypatch.setattr(environment, "_open_file", changed)
    assert_safe_failure(private_file)


def test_file_replaced_after_read_rejected(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    replacement = write_private(private_file.with_name("replacement.env"), ASSIGNMENT)
    real_read = os.read

    def changed(descriptor, size):
        result = real_read(descriptor, size)
        if replacement.exists():
            os.replace(replacement, private_file)
        return result

    monkeypatch.setattr(os, "read", changed)
    assert_safe_failure(private_file)


def test_read_stays_bounded_when_file_size_changes(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    requested = []

    def growing(descriptor, size):
        requested.append(size)
        return b"x" * size

    monkeypatch.setattr(os, "read", growing)
    assert_safe_failure(private_file)
    assert requested == [8193]


def test_non_dir_fd_fallback_checks_regular_file(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    monkeypatch.setattr(os, "supports_dir_fd", set())
    assert environment.load_client_environment(private_file).token == KEY


def test_permission_error_does_not_leak_os_message(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)

    def denied(path):
        raise PermissionError(f"secret path: {path} with token {KEY}")

    monkeypatch.setattr(environment, "_read_file", denied)
    assert_safe_failure(private_file)


def test_windows_reparse_attribute_rejected():
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0x400)
    assert environment._is_link(info)


def test_parent_reparse_directory_rejected(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    real_lstat = Path.lstat

    def reparse(path):
        if path == private_file.parent:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        return real_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse)
    assert_safe_failure(private_file)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and permission checks")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o700, 0o4600, 0o000])
def test_posix_permissions_must_be_private(private_file, mode, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    actual = private_file.stat()
    info = SimpleNamespace(
        st_mode=stat.S_IFREG | mode,
        st_nlink=1,
        st_size=actual.st_size,
        st_uid=os.geteuid(),
    )
    with pytest.raises(environment.ClientEnvironmentError, match="^invalid_client_environment$"):
        environment._check_file(info)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership check")
def test_posix_wrong_owner_rejected():
    info = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_size=10, st_uid=os.geteuid() + 1
    )
    with pytest.raises(environment.ClientEnvironmentError, match="^invalid_client_environment$"):
        environment._check_file(info)


@pytest.mark.skipif(os.name != "posix", reason="POSIX read-only file permissions")
def test_posix_private_read_only_mode_accepted():
    info = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o400, st_nlink=1, st_size=10, st_uid=os.geteuid()
    )
    environment._check_file(info)
