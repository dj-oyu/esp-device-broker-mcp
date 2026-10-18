"""Local approval and report-read CLI tests with invented plans, never USB."""

import pytest
from test_serial_observation import device

from cardputer_mcp.broker import Broker, Session, digest_plan
from cardputer_mcp.cli import parser
from cardputer_mcp.operator import approve_plan
from cardputer_mcp.store import Store


def write_plan(tmp_path, change=None):
    _backend, port, details = device()
    lease = Session("f" * 64, "b" * 32, "remote_mcp", 1000, "ephemeral:remote_mcp")
    plan = {
        "version": 1,
        "broker_epoch": "a" * 32,
        "session_id": lease.id,
        "device": "configured-cardputer",
        "backend": "serial",
        "job_id": "synthetic-log-observation",
        **details,
        "log_delivery": Broker._log_delivery(lease),
    }
    if change:
        change(plan)
    digest = digest_plan(plan)
    with_store = Store(tmp_path / "history.sqlite3")
    with_store.put_plan(digest, plan)
    with_store.close()
    return digest, port


@pytest.mark.parametrize("answer", ["", "APPROVE", "yes"])
def test_cli_approval_is_retired_without_prompt_or_database_mutation(tmp_path, answer):
    digest, port = write_plan(tmp_path)
    prompts = []
    with pytest.raises(ValueError, match="^approval_interface_retired$"):
        approve_plan(tmp_path, digest, lambda prompt: prompts.append(prompt) or answer)
    assert prompts == [] and port.opens == 0 and port.writes == []
    store = Store(tmp_path / "history.sqlite3")
    try:
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        store.close()


def test_device_log_cli_uses_job_without_session_or_device_argument():
    args = parser().parse_args(["device-logs", "synthetic-log-observation"])
    assert args.command == "device-logs" and args.job_id == "synthetic-log-observation"
    assert not hasattr(args, "session") and not hasattr(args, "port")
