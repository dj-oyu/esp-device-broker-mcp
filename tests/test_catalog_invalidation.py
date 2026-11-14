"""A job that can change the device's firmware or boot drops the observed app catalog."""

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.models import TestName


def _broker(root: Path) -> Broker:
    return Broker(
        Settings(state_dir=root / "state"),
        lock_path=root / "device.lockfile",
        principal_valid=lambda *args: True,
    )


def _run(test: str) -> SimpleNamespace:
    return SimpleNamespace(
        job_id="job-1",
        plan={"test": test},
        job_log=None,
        device_tap=None,
        lease=SimpleNamespace(principal_id="alice", owner="local"),
    )


@pytest.mark.parametrize(
    ("test", "dropped"),
    [
        (TestName.FLASH, True),
        (TestName.RESTORE, True),
        (TestName.BACKUP, True),
        (TestName.RESTART, True),
        (TestName.APP_SESSION, False),
        (TestName.HELLO, False),
        (TestName.BUILD, False),
    ],
)
def test_catalog_cache_is_dropped_only_after_firmware_changing_jobs(test, dropped):
    with tempfile.TemporaryDirectory() as temp:
        broker = _broker(Path(temp))
        marker = {"observed_at": 0.0, "catalog_sha256": "x"}
        broker.app_sessions.catalog_cache = dict(marker)
        broker._close_admitted(_run(test.value))
        assert (broker.app_sessions.catalog_cache is None) is dropped
