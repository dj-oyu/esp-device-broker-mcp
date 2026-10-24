"""Local approval and report-read CLI tests with invented plans, never USB."""

from test_serial_observation import device

from cardputer_mcp.broker import Broker, Session, digest_plan
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
