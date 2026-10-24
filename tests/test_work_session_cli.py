"""Typed CLI parsing and offline recovery; synthetic local files only."""

from pathlib import Path

import pytest
from filelock import FileLock

from cardputer_mcp import cli
from cardputer_mcp.operator import acknowledge_device_request_recovery
from cardputer_mcp.store import Store


def test_session_cli_never_accepts_raw_resume_argument():
    parser = cli.parser()
    args = parser.parse_args(["work-request", "--wait-seconds", "17"])
    assert args.resume_env == "CARDPUTER_WORK_RESUME_TOKEN"
    assert args.wait_seconds == 17
    with pytest.raises(SystemExit):
        parser.parse_args(["work-resume", "--resume-token", "a" * 64])


def test_cli_keys_have_no_token_argument():
    parser = cli.parser()
    args = parser.parse_args(["keys", "issue", "--registry", "synthetic.sqlite3"])
    assert not args.control and args.ttl_seconds == 3600
    assert args.origin == "remote_mcp"
    with pytest.raises(SystemExit):
        parser.parse_args(["keys", "verify", "--registry", "fake.sqlite3", "--token", "secret"])


@pytest.fixture
def recovery(tmp_path: Path):
    directory = tmp_path / "state"
    directory.mkdir()
    store = Store(directory / "history.sqlite3")
    store.db.execute(
        "INSERT INTO device_requests(id,state,lease_seconds,reason) VALUES (?,?,?,?)",
        ("synthetic-request", "needs_review", 30, "broker_restart_no_replay"),
    )
    store.db.commit()
    store.close()
    return directory


def test_explicit_offline_recovery_needs_no_keyword_and_never_replays(recovery):
    prompts = []
    result = acknowledge_device_request_recovery(
        recovery, "synthetic-request", lambda prompt: prompts.append(prompt) or ""
    )
    assert result == {"reviewed": True, "request_id": "synthetic-request", "replayed": False}
    assert not prompts
    store = Store(recovery / "history.sqlite3")
    assert not store.needs_review()
    assert store.request("synthetic-request")["state"] == "needs_review"
    store.close()


def test_review_request_refuses_running_broker(recovery):
    with FileLock(str(recovery / "broker.lockfile"), timeout=0):
        with pytest.raises(ValueError, match="stop_broker_before_recovery_review"):
            acknowledge_device_request_recovery(recovery, "synthetic-request", lambda _: "yes")
