"""One bounded MCP client for registered apps, including host benchmark recipes.

Only status/event reads are repeated. A failed or uncertain mutation is never
replayed, and consumed input is not an application quality assertion.
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from mcp import Client

from .app_models import Action, AppSessionOptions
from .client import call

_RESULTS = {
    0: "ok",
    1: "accepted",
    2: "consumed",
    14: "outcome_unknown",
}


class AppClientError(ValueError):
    """Fixed errors without transport exceptions, request bodies or credentials."""


class AppSessionClient:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.session: str | None = None
        self.job_id: str | None = None
        self.identity: dict[str, int] = {}
        self.cursor: dict[str, Any] | None = None
        self.next_seq = 2
        self.next_command = 2
        self.uncertain = False
        self.recent_logs: deque[dict[str, Any]] = deque(maxlen=256)

    async def _request(
        self, tool: str, request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            result = await call(self.client, tool, request)
        except Exception:
            # The remote mutation may have executed; never automatically retry.
            raise AppClientError("outcome_unknown") from None
        if result.get("ok") is False:
            raise AppClientError("app_request_rejected")
        return result

    async def list_apps(self) -> dict[str, Any]:
        """Read the cached catalog, including freshness; never opens a device."""
        return await self._request("list_apps")

    async def events(self) -> dict[str, Any]:
        if self.job_id is None:
            raise AppClientError("app_session_not_started")
        request: dict[str, Any] = {"job_id": self.job_id, "limit": 100}
        if self.cursor is not None:
            request["cursor"] = self.cursor
        result = await self._request("read_app_events", request)
        if result.get("gap") or result.get("expired"):
            raise AppClientError("outcome_unknown")
        self.cursor = result.get("next_cursor")
        self.recent_logs.extend(result.get("logs", []))
        return result

    async def _wait(
        self, predicate: Callable[[dict[str, Any]], bool], timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch = await self.events()
            for event in batch.get("entries", []):
                if predicate(event):
                    return dict(event)
            await asyncio.sleep(0.05)
        raise AppClientError("outcome_unknown")

    async def start(
        self,
        session: str,
        job_id: str,
        options: AppSessionOptions,
        *,
        timeout: float = 5,
    ) -> dict[str, Any]:
        if self.job_id is not None or not 0 < timeout <= 90:
            raise AppClientError("invalid_client_state")
        self.session, self.job_id = session, job_id
        plan = await self._request(
            "prepare_test",
            {
                "session": session,
                "job_id": job_id,
                "test": "app_session",
                "app": options.model_dump(mode="json", exclude_none=True),
            },
        )
        await self._request("execute_test", {"session": session, "plan_hash": plan["plan_hash"]})
        started = await self._wait(lambda e: e.get("kind") == "started", timeout)
        self.identity = {
            name: started[name] for name in ("boot_id", "run_id", "control_session")
        }
        self.next_seq = started.get("next_seq", 2)
        return started

    @staticmethod
    def _result(event: dict[str, Any]) -> str:
        result = event.get("result", event.get("state", "outcome_unknown"))
        return _RESULTS.get(result, str(result)) if isinstance(result, int) else str(result)

    async def command(
        self, operation: str, action: Action | None = None, *, timeout: float = 3
    ) -> dict[str, Any]:
        if (
            not self.identity
            or self.session is None
            or self.job_id is None
            or self.uncertain
            or not 0 < timeout <= 90
        ):
            raise AppClientError("invalid_client_state")
        command_id, seq = self.next_command, self.next_seq
        request: dict[str, Any] = {
            "session": self.session,
            "job_id": self.job_id,
            **self.identity,
            "command_id": command_id,
            "seq": seq,
            "operation": operation,
        }
        if action is not None:
            request["action"] = action
        try:
            await self._request("app_command", request)
        except AppClientError:
            self.uncertain = True
            raise
        # Advance identities once, even when the ACK is lost. Reusing this client
        # cannot silently create a new command for that same attempted input.
        self.next_command += 1
        try:
            event = await self._wait(
                lambda e: e.get("kind") == "ack"
                and e.get("command_id") == command_id
                and self._result(e) not in ("sent", "accepted", "queued"),
                timeout,
            )
        except AppClientError:
            # One read-only status query can establish a lost ACK. It cannot
            # reapply the attempted command, even after device/broker restart.
            status = {**request, "operation": "status"}
            status.pop("action", None)
            try:
                event = await self._request("app_command", status)
            except AppClientError:
                self.uncertain = True
                raise
            if self._result(event) not in ("ok", "consumed"):
                self.uncertain = True
                raise AppClientError("outcome_unknown") from None
        self.next_seq = event.get("next_seq", seq + 1)
        if self._result(event) == "outcome_unknown":
            self.uncertain = True
            raise AppClientError("outcome_unknown")
        if self._result(event) not in ("ok", "consumed"):
            raise AppClientError("app_command_rejected")
        if "run_id" in event:
            self.identity["run_id"] = event["run_id"]
        return event

    async def tap(self, action: Action, *, timeout: float = 3) -> dict[str, Any]:
        return await self.command("action.tap", action, timeout=timeout)

    async def suspend(self, *, timeout: float = 3) -> dict[str, Any]:
        return await self.command("suspend", timeout=timeout)

    async def resume(self, *, timeout: float = 3) -> dict[str, Any]:
        return await self.command("resume", timeout=timeout)

    async def stop(self, *, timeout: float = 5) -> dict[str, Any]:
        await self.command("stop", timeout=timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = await self._request("job_status", {"job_id": self.job_id})
            if job.get("state") in ("succeeded", "failed", "cancelled", "needs_review"):
                return job
            await asyncio.sleep(0.05)
        raise AppClientError("outcome_unknown")
