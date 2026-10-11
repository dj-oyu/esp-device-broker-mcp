"""Manual approval never extends a client's revoked authorization to queued work."""

import pytest

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest


@pytest.mark.parametrize("change", ["revoke", "expire_in_callback"])
async def test_legacy_queued_job_rechecks_current_principal_before_start(tmp_path, change):
    now = [100.0]
    valid = [True]
    crossing = [False]

    def principal(identifier, origin, scopes):
        if crossing[0]:
            now[0] = 500.0
        return valid[0]

    broker = Broker(
        Settings(state_dir=tmp_path),
        clock=lambda: now[0],
        lock_path=tmp_path / "device.lock",
        principal_valid=principal,
    )
    await broker.start()
    try:
        lease = broker.acquire(AcquireRequest(owner=Origin.REMOTE), principal_id="synthetic-client")
        plan = broker.prepare(PrepareRequest(session=lease["session"], job_id="queued"))
        broker._store().approve(plan["plan_hash"])
        broker.execute(ExecuteRequest(session=lease["session"], plan_hash=plan["plan_hash"]))
        if change == "revoke":
            valid[0] = False
        else:
            crossing[0] = True
        await broker.queue.join()
        assert broker.backend.calls == 0
        assert broker.job("queued")["state"] == "cancelled"
    finally:
        await broker.close()
