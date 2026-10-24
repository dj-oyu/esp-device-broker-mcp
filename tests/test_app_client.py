"""Host recipes share one client; uncertain mutations are never replayed."""

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from cardputer_mcp.app_client import AppClientError, AppSessionClient
from cardputer_mcp.app_models import AppSessionOptions


def route(tool: str, request: dict[str, Any] | None) -> str:
    """Name the six-tool request by the operation it selects."""
    body = request or {}
    if tool == "status":
        if body["view"] == "apps":
            return "apps"
        if body["view"] == "job":
            return "job"
        assert body["view"] == "logs" and body["stream"] == "app"
        return "events"
    if tool == "submit":
        assert body["kind"] == "app_session"
        return "submit"
    assert tool == "execute"
    return "run" if body["op"] == "run" else "app_input"


class FakeBroker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.pending: list[dict[str, Any]] = []
        self.sequence = 0
        self.mutation_failure = False

    async def call(
        self, client: Any, tool: str, request: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        tool = route(tool, request)
        self.calls.append((tool, request))
        if tool == "apps":
            return {"ok": True, "apps": [], "freshness": "unobserved"}
        if tool == "submit":
            return {"ok": True, "plan_hash": "b" * 64}
        if tool == "run":
            self.pending.append(
                dict(kind="started", boot_id=123, run_id=7, control_session=9, next_seq=2)
            )
            return {"ok": True, "state": "queued"}
        if tool == "app_input":
            assert request is not None
            if self.mutation_failure:
                raise RuntimeError("synthetic secret never returned")
            return dict(ok=True, command_id=request["command_id"], state="queued", sent=False)
        if tool == "events":
            self.sequence += 1
            entries, self.pending = self.pending, []
            return dict(
                ok=True,
                entries=entries,
                logs=[dict(sequence=self.sequence, text="redacted output")],
                next_cursor=dict(epoch="a" * 32, control=self.sequence, log=self.sequence),
                gap=False,
                expired=False,
            )
        if tool == "job":
            return dict(ok=True, state="succeeded")
        raise AssertionError(tool)


class AppClientTests(unittest.TestCase):
    def test_client_uses_real_runtime_sent_and_ack_events(self) -> None:
        from cardputer_mcp.app_models import AppCommandRequest, AppEventsRequest
        from cardputer_mcp.app_sessions import AppSessions
        from cardputer_mcp.backend import MockBackend

        async def scenario() -> None:
            manager, backend = AppSessions(), MockBackend()
            plans: dict[str, dict[str, Any]] = {}
            tasks: list[asyncio.Task[str]] = []
            mutations: list[str] = []

            async def adapter(
                connection: Any, tool: str, body: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                tool = route(tool, body)
                if tool == "submit":
                    assert body is not None
                    options = AppSessionOptions.model_validate(body["app"])
                    plans["current"] = {
                        "job_id": body["job_id"],
                        **manager.prepare(backend, options),
                    }
                    return dict(ok=True, plan_hash="b" * 64)
                if tool == "run":
                    tasks.append(
                        asyncio.create_task(
                            manager.execute(
                                backend, plans["current"], "owner", lambda _: None, lambda: True
                            )
                        )
                    )
                    return dict(ok=True, state="queued")
                if tool == "events":
                    assert body is not None
                    body = {k: v for k, v in body.items() if k not in ("view", "stream")}
                    run = manager.jobs.get(body["job_id"])
                    if run is None:
                        return dict(ok=True, entries=[], logs=[], gap=False, expired=False)
                    return {
                        "ok": True,
                        **run.events.read("owner", AppEventsRequest.model_validate(body)),
                    }
                if tool == "app_input":
                    assert body is not None
                    body = {k: v for k, v in body.items() if k != "op"}
                    mutations.append(body["operation"])
                    return {
                        "ok": True,
                        **manager.jobs[body["job_id"]].command(
                            AppCommandRequest.model_validate(body)
                        ),
                    }
                if tool == "job":
                    if tasks[0].done():
                        assert tasks[0].result() == "app_session_stopped"
                        return dict(ok=True, state="succeeded")
                    return dict(ok=True, state="running")
                raise AssertionError(tool)

            with patch("cardputer_mcp.app_client.call", adapter):
                client = AppSessionClient(None)  # type: ignore[arg-type]
                try:
                    await client.start(
                        "c" * 64,
                        "runtime-client",
                        AppSessionOptions(app_id="local.hello", duration_ms=1000),
                    )
                    assert (await client.tap("accept"))["result"] == "consumed"
                    assert (await client.stop())["state"] == "succeeded"
                    assert mutations == ["action.tap", "stop"]
                    events = list(manager.jobs["runtime-client"].events.control)
                    sent = next(e for e in events if e["kind"] == "sent")
                    assert "result" not in sent and "state" not in sent
                    assert not client.uncertain
                finally:
                    for run in manager.jobs.values():
                        run.cancelled.set()
                    for task in tasks:
                        await asyncio.wait_for(task, 2)

        asyncio.run(scenario())

    def test_transport_failure_never_replays_or_leaks_exception(self) -> None:
        async def scenario() -> None:
            broker = FakeBroker()
            with patch("cardputer_mcp.app_client.call", broker.call):
                client = AppSessionClient(None)  # type: ignore[arg-type]
                await client.start("c" * 64, "recipe", AppSessionOptions(app_id="local.hello"))
                broker.mutation_failure = True
                with self.assertRaisesRegex(AppClientError, "^outcome_unknown$"):
                    await client.tap("accept")
                with self.assertRaisesRegex(AppClientError, "invalid_client_state"):
                    await client.tap("accept")
                assert len([t for t, _ in broker.calls if t == "app_input"]) == 1

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
