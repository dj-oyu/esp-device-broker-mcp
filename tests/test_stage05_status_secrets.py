"""Every `status` view, driven over MCP with live secrets in broker state, never shows them.

Secrets present while reading: bearer keys, a work-session resume token, a live device
lease capability bound to that work session, and a continuation ticket. The only
current exception is pinned explicitly: view=session returns the lease capability to
the owning control principal (it is how that principal learns its lease).
"""

import asyncio
import json
from typing import Any

from test_registry_http import LOCAL, REMOTE
from test_registry_http import registry_service as registry_service

from cardputer_mcp.client import connect

JOB = "secret-job"


def _views(resume_token: str) -> list[dict[str, Any]]:
    return [
        {"view": "identity"},
        {"view": "targets"},
        {"view": "source", "project_id": "synthetic"},
        {"view": "device"},
        {"view": "session", "resume_token": resume_token},
        {"view": "job", "job_id": JOB},
        {"view": "logs", "stream": "broker"},
        {"view": "logs", "stream": "broker", "limit": 100},
        {"view": "logs", "stream": "build", "job_id": JOB},
        {"view": "logs", "stream": "device", "job_id": JOB},
        {"view": "logs", "stream": "app", "job_id": JOB},
        {"view": "capture", "job_id": JOB},
        {"view": "apps"},
    ]


def _rendered(result) -> str:
    return json.dumps(
        {
            "content": [c.model_dump() for c in result.content],
            "structured": result.structured_content,
        }
    )


async def test_status_views_never_disclose_tokens_or_capabilities(registry_service):
    service = registry_service
    owner_key = service.keys["first"].token
    async with connect(service.url, owner_key) as owner:
        opened = await owner.call_tool(
            "session", {"request": {"action": "open", "owner": "remote_mcp"}}
        )
        resume_token = opened.structured_content["resume_token"]
        requested = await owner.call_tool(
            "session",
            {
                "request": {
                    "action": "request",
                    "resume_token": resume_token,
                    "lease_seconds": 300,
                    "wait_seconds": 1,
                }
            },
        )
        lease = requested.structured_content["lease"]["session"]
        plan = await owner.call_tool(
            "submit", {"request": {"kind": "diagnostics", "session": lease, "job_id": JOB}}
        )
        executed = await owner.call_tool(
            "execute",
            {
                "request": {
                    "op": "run",
                    "session": lease,
                    "plan_hash": plan.structured_content["plan_hash"],
                    "continues": True,
                }
            },
        )
        ticket = executed.structured_content["continuation_ticket"]
        await asyncio.wait_for(service.resident.queue.join(), timeout=10)
        # The finished device job ends its lease and holds the device for its chain;
        # spend the ticket to take a fresh lease, so a live capability is in broker
        # state (and the ticket in the store) while every view is read.
        again = await owner.call_tool(
            "session",
            {
                "request": {
                    "action": "request",
                    "resume_token": resume_token,
                    "lease_seconds": 300,
                    "wait_seconds": 1,
                    "after": ticket,
                }
            },
        )
        first_lease, lease = lease, again.structured_content["lease"]["session"]
        assert service.resident.session is not None
        assert service.resident.session.capability == lease != first_lease

    secrets = {
        "resume_token": resume_token,
        "lease": lease,
        "spent_lease": first_lease,
        "ticket": ticket,
        **{f"key:{name}": issued.token for name, issued in service.keys.items()},
        "local_bearer": LOCAL,
        "remote_bearer": REMOTE,
    }
    seen: list[tuple[str, str, str]] = []
    for caller in ("first", "second", "reader"):
        async with connect(service.url, service.keys[caller].token) as client:
            for request in _views(resume_token):
                result = await client.call_tool("status", {"request": request})
                text = _rendered(result)
                if caller == "first" and request["view"] in ("identity", "device", "job"):
                    assert result.structured_content["ok"] is True, request
                view = request["view"] + "/" + request.get("stream", "")
                seen.extend(
                    (caller, view, name) for name, value in secrets.items() if value in text
                )
    # Pinned current exception: the owning control principal reads its own lease
    # capability back through view=session. Nothing else, for nobody else.
    assert seen == [("first", "session/", "lease")]


async def test_session_view_hides_lease_from_read_only_and_other_principals(registry_service):
    service = registry_service
    async with connect(service.url, service.keys["first"].token) as owner:
        opened = await owner.call_tool(
            "session", {"request": {"action": "open", "owner": "remote_mcp"}}
        )
        resume_token = opened.structured_content["resume_token"]
        await owner.call_tool(
            "session",
            {
                "request": {
                    "action": "request",
                    "resume_token": resume_token,
                    "lease_seconds": 30,
                    "wait_seconds": 1,
                }
            },
        )
        mine = await owner.call_tool(
            "status", {"request": {"view": "session", "resume_token": resume_token}}
        )
    assert mine.structured_content["ok"] is True
    assert "resume_token" not in mine.structured_content
    assert isinstance(mine.structured_content["lease"]["session"], str)
    for caller in ("second", "reader"):
        async with connect(service.url, service.keys[caller].token) as other:
            result = await other.call_tool(
                "status", {"request": {"view": "session", "resume_token": resume_token}}
            )
        assert result.structured_content["ok"] is False, caller
