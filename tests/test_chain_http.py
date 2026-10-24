"""A two-job chain driven only through the six MCP tools over localhost HTTP (mock backend)."""

import asyncio

from test_authenticated_mock import authenticated_mock as authenticated_mock

from cardputer_mcp.broker import Broker
from cardputer_mcp.client import call, connect

LOCAL = "a" * 64
REMOTE = "b" * 64


async def finished(client, job_id: str) -> dict:
    for _ in range(400):
        job = await call(client, "status", {"view": "job", "job_id": job_id})
        if job.get("state") in ("succeeded", "failed", "cancelled", "needs_review"):
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


async def test_continuation_through_the_six_tools(authenticated_mock: tuple[str, Broker]) -> None:
    url, resident = authenticated_mock
    async with connect(url, REMOTE) as client:
        work = await call(client, "session", {"action": "open", "owner": "remote_mcp"})
        granted = await call(
            client, "session", {"action": "request", "resume_token": work["resume_token"]}
        )
        lease = granted["lease"]["session"]
        plan = await call(
            client, "submit", {"kind": "diagnostics", "session": lease, "job_id": "first"}
        )
        assert plan["ok"] and plan["execute_tool"] == "execute"
        queued = await call(
            client,
            "execute",
            {"op": "run", "plan_hash": plan["plan_hash"], "session": lease, "continues": True},
        )
        assert queued["ok"]
        ticket = queued["continuation_ticket"]
        assert len(ticket) == 8
        # A hint without continues is a shape error, not a silent ignore.
        bad = await call(
            client,
            "execute",
            {"op": "run", "plan_hash": plan["plan_hash"], "session": lease, "chain_total_hint": 3},
        )
        assert bad == {"ok": False, "error": "argument_required", "field": "continues"}
        first = await finished(client, "first")
        assert first["state"] == "succeeded"
        assert first["chain"]["chain_position"] == 1 and first["chain"]["chain_open"] is True
        # The idle slot is now a reservation for this chain only.
        for _ in range(20):
            device = await call(client, "status", {"view": "device"})
            if device["device_hold"]["active"]:
                break
            await asyncio.sleep(0.05)
        assert device["device_hold"]["active"] is True
        # Ask for the next device job ahead of time: no lease, only the ticket.
        later = await call(
            client,
            "submit",
            {"kind": "diagnostics", "job_id": "second", "after": ticket},
        )
        assert later["ok"], later
        waiting = await call(client, "execute", {"op": "run", "plan_hash": later["plan_hash"]})
        assert waiting["ok"] and waiting["state"] in ("waiting_parent", "waiting_for_device")
        second = await finished(client, "second")
        assert second["state"] == "succeeded"
        assert second["chain"]["chain_position"] == 2
        assert second["chain"]["chain_open"] is False
        # The ticket is spent; the lease of the finished job ended with it.
        again = await call(
            client, "submit", {"kind": "diagnostics", "job_id": "third", "after": ticket}
        )
        assert again == {"ok": False, "error": "ticket_consumed"}
        await asyncio.sleep(0.1)
        resident.maintenance_tick()
        assert resident.session is None


async def test_rules_for_after_and_wrong_tool(authenticated_mock: tuple[str, Broker]) -> None:
    url, _ = authenticated_mock
    async with connect(url, REMOTE) as client:
        refused = await call(
            client,
            "submit",
            {"kind": "backup", "job_id": "b", "device_id": "dev", "after": "ABCDEFGH"},
        )
        assert refused == {"ok": False, "error": "argument_not_allowed", "field": "after"}
        with_session = await call(
            client,
            "submit",
            {"kind": "diagnostics", "job_id": "d", "session": "a" * 64, "after": "ABCDEFGH"},
        )
        assert with_session["ok"] is False and with_session["error"] == "argument_not_allowed"
        missing = await call(client, "submit", {"kind": "diagnostics", "job_id": "d"})
        assert missing == {"ok": False, "error": "argument_required", "field": "session"}
        unknown_ticket = await call(
            client, "submit", {"kind": "diagnostics", "job_id": "d", "after": "ABCDEFGH"}
        )
        assert unknown_ticket == {"ok": False, "error": "ticket_invalid"}
        gone = await call(client, "cancel", {"target": "ticket", "ticket": "ABCDEFGH"})
        assert gone == {"ok": False, "error": "ticket_invalid"}
