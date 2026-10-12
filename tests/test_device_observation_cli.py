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


def test_cli_one_exact_approval_discloses_recipient_data_and_bounds(tmp_path):
    digest, port = write_plan(tmp_path)
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        return f"APPROVE {digest}"

    assert approve_plan(tmp_path, digest, confirm) == {"approved": True, "plan_hash": digest}
    assert len(prompts) == 1
    for phrase in (
        "30 seconds / 64 KiB",
        "ephemeral:remote_mcp",
        "private data",
        "redaction is not guaranteed complete",
        "five minutes",
        "No commands, reset command, flash",
        "ELF prefix is not the firmware binary SHA256",
    ):
        assert phrase in prompts[0]
    assert port.opens == 0 and port.writes == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_lines", 257),
        ("retention_seconds", True),
        ("origin", "other"),
        ("principal_id", "unsafe\ntext"),
    ],
)
def test_cli_rejects_invalid_delivery_schema_before_prompt(tmp_path, field, value):
    digest, port = write_plan(tmp_path, lambda plan: plan["log_delivery"].update({field: value}))
    prompts = []
    with pytest.raises(ValueError, match="^plan_not_found_or_changed$"):
        approve_plan(tmp_path, digest, lambda prompt: prompts.append(prompt) or "")
    assert prompts == [] and port.opens == 0


def test_cli_rejected_approval_does_not_persist_permission(tmp_path):
    digest, _port = write_plan(tmp_path)
    with pytest.raises(ValueError, match="^approval_not_granted$"):
        approve_plan(tmp_path, digest, lambda _prompt: "NO")
    store = Store(tmp_path / "history.sqlite3")
    try:
        assert store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    finally:
        store.close()


def test_device_log_cli_uses_job_without_session_or_device_argument():
    args = parser().parse_args(["device-logs", "synthetic-log-observation"])
    assert args.command == "device-logs" and args.job_id == "synthetic-log-observation"
    assert not hasattr(args, "session") and not hasattr(args, "port")
