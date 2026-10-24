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
        "CARDPUTER_CLIENT_KEY=" + KEY[:-1],
        "CARDPUTER_CLIENT_KEY='" + KEY + "'",
        "export " + ASSIGNMENT,
        "CARDPUTER_CLIENT_KEY=$(echo token)",
        ASSIGNMENT + "\nUNRECOGNIZED=value",
        ASSIGNMENT + "\n" + ASSIGNMENT,
        ASSIGNMENT + "\x00",
        b"\xff\xfe" + ASSIGNMENT.encode("ascii"),
    ],
)
def test_rejects_nonliteral_or_malformed_files(private_file, data):
    assert_safe_failure(write_private(private_file, data))


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://127.0.0.1:1023/mcp",
        "http://127.0.0.1:08765/mcp",
        "http://localhost:8765/mcp",
        "https://127.0.0.1:8765/mcp",
        "http://user@127.0.0.1:8765/mcp",
        "http://127.0.0.1:8765/mcp?x=1",
    ],
)
def test_rejects_unsupported_urls(private_file, url):
    assert_safe_failure(write_private(private_file, ASSIGNMENT + "\nCARDPUTER_BROKER_URL=" + url))


@pytest.mark.parametrize("shape", ["traversal", "missing", "directory", "relative"])
def test_parent_traversal_is_rejected(private_file, shape, monkeypatch):
    if shape == "directory":
        private_file.mkdir(parents=True)
    elif shape != "missing":
        write_private(private_file, ASSIGNMENT)
    path = private_file
    if shape == "traversal":
        path = private_file.parent / ".." / "private" / private_file.name
    elif shape == "relative":
        monkeypatch.chdir(private_file.parent)
        path = Path(private_file.name)
    assert_safe_failure(path)


@pytest.mark.parametrize("extra", [0, 1])
def test_bounded_file_size(private_file, extra):
    data = ASSIGNMENT + "\n#" + "x" * (8192 - len(ASSIGNMENT) - 2 + extra)
    write_private(private_file, data)
    if extra:
        assert_safe_failure(private_file)
    else:
        assert environment.load_client_environment(private_file).token == KEY


@pytest.mark.parametrize("kind", ["file", "parent", "hard"])
def test_symlink_paths_rejected(private_file, kind):
    target = write_private(private_file, ASSIGNMENT)
    link = private_file.parent.parent / "linked"
    try:
        if kind == "hard":
            os.link(target, target.with_name("second.env"))
            link = target
        else:
            link.symlink_to(
                target.parent if kind == "parent" else target, target_is_directory=kind == "parent"
            )
    except (OSError, NotImplementedError):
        pytest.skip("links unavailable")
    assert_safe_failure(link / target.name if kind == "parent" else link)


@pytest.mark.parametrize("marker", ["directory", "link", "monorepo", "cwd_allowed"])
def test_git_checkout_files_rejected(private_file, layout, monkeypatch, marker):
    project, account = layout
    write_private(private_file, ASSIGNMENT)
    git = private_file.parent.parent / ".git"
    if marker == "directory":
        git.mkdir()
    elif marker == "monorepo":
        (project.parent / ".git").mkdir()
    elif marker == "cwd_allowed":
        (project / ".git").mkdir()
        monkeypatch.chdir(project)
        assert environment.load_client_environment(private_file).token == KEY
        return
    else:
        try:
            git.symlink_to(git.with_name("missing-git-directory"))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
    assert_safe_failure(private_file)


@pytest.mark.parametrize("installed", [False, True])
def test_adapter_source_tree_rejected(layout, monkeypatch, installed):
    root = layout[0]
    if installed:
        root = root / "site-packages"
        module = root / "cardputer_mcp" / "client_environment.py"
        module.parent.mkdir(parents=True)
        module.touch()
        monkeypatch.setattr(environment, "__file__", str(module))
    assert_safe_failure(write_private(root / "private" / "client.env", ASSIGNMENT))


def test_file_replacement_between_check_and_open_rejected(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)
    replacement = write_private(private_file.with_name("replacement.env"), ASSIGNMENT)
    real_open = environment._open_file

    def changed(path):
        os.replace(replacement, path)
        return real_open(path)

    monkeypatch.setattr(environment, "_open_file", changed)
    assert_safe_failure(private_file)


def test_permission_error_does_not_leak_os_message(private_file, monkeypatch):
    write_private(private_file, ASSIGNMENT)

    def denied(path):
        raise PermissionError(f"secret path: {path} with token {KEY}")

    monkeypatch.setattr(environment, "_read_file", denied)
    assert_safe_failure(private_file)


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
@pytest.mark.parametrize(
    ("mode", "uid_delta", "accepted"),
    [(0o644, 0, False), (0o700, 0, False), (0o4600, 0, False), (0o600, 1, False), (0o400, 0, True)],
)
def test_posix_permissions_must_be_private(mode, uid_delta, accepted):
    info = SimpleNamespace(
        st_mode=stat.S_IFREG | mode, st_nlink=1, st_size=10, st_uid=os.geteuid() + uid_delta
    )
    if accepted:
        environment._check_file(info)
        return
    with pytest.raises(environment.ClientEnvironmentError, match="^invalid_client_environment$"):
        environment._check_file(info)
