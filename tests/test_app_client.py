"""Host recipes share one client; uncertain mutations are never replayed."""

import asyncio
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

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
        self.lose_ack = False
        self.status_known = True
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
            if request["operation"] == "status":
                return dict(
                    ok=True,
                    command_id=request["command_id"],
                    state="consumed" if self.status_known else "outcome_unknown",
                    next_seq=request["seq"] + 1,
                )
            if self.mutation_failure:
                raise RuntimeError("synthetic secret never returned")
            if not self.lose_ack:
                self.pending.extend(
                    [
                        dict(kind="sent", command_id=request["command_id"], seq=request["seq"]),
                        dict(kind="ack", command_id=request["command_id"], result="accepted"),
                    ]
                )
                self.pending.append(
                    dict(
                        kind="ack",
                        command_id=request["command_id"],
                        result="consumed" if request["operation"] == "action.tap" else "ok",
                        next_seq=request["seq"] + 1,
                    )
                )
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

    def test_hello_and_stress_use_same_typed_calls_and_fresh_default(self) -> None:
        async def scenario(app_id: str) -> None:
            broker = FakeBroker()
            with patch("cardputer_mcp.app_client.call", broker.call):
                client = AppSessionClient(None)  # type: ignore[arg-type]
                await client.list_apps()
                await client.start("c" * 64, "recipe", AppSessionOptions(app_id=app_id))
                ack = await client.tap("accept")
                assert ack["result"] == "consumed"
                await client.suspend()
                await client.resume()
                assert (await client.stop())["state"] == "succeeded"
                assert len(client.recent_logs) > 0
                prepared = next(r for t, r in broker.calls if t == "submit")
                assert prepared is not None
                assert prepared["app"]["app_id"] == app_id
                assert prepared["app"]["start"] == "fresh"
                commands = [r for t, r in broker.calls if t == "app_input"]
                assert [r["seq"] for r in commands if r] == [2, 3, 4, 5]
                assert all(r and r["run_id"] == 7 for r in commands)
                assert all(r and r["boot_id"] == 123 for r in commands)

        for app_id in ("local.hello", "local.stress"):
            asyncio.run(scenario(app_id))

    def test_lost_ack_uses_status_only(self) -> None:
        async def scenario() -> None:
            broker = FakeBroker()
            with patch("cardputer_mcp.app_client.call", broker.call):
                client = AppSessionClient(None)  # type: ignore[arg-type]
                await client.start("c" * 64, "recipe", AppSessionOptions(app_id="local.hello"))
                broker.lose_ack = True
                with patch.object(
                    client, "_wait", AsyncMock(side_effect=AppClientError("outcome_unknown"))
                ):
                    ack = await client.tap("accept")
                assert ack["state"] == "consumed"
                requests = [r for t, r in broker.calls if t == "app_input"]
                assert [r["operation"] for r in requests if r] == ["action.tap", "status"]
                assert requests[0] and requests[1]
                assert requests[0]["command_id"] == requests[1]["command_id"]
                assert client.next_seq == 3

        asyncio.run(scenario())

    def test_unknown_status_blocks_further_input(self) -> None:
        async def scenario() -> None:
            broker = FakeBroker()
            with patch("cardputer_mcp.app_client.call", broker.call):
                client = AppSessionClient(None)  # type: ignore[arg-type]
                await client.start("c" * 64, "recipe", AppSessionOptions(app_id="local.hello"))
                broker.status_known = False
                with patch.object(
                    client, "_wait", AsyncMock(side_effect=AppClientError("outcome_unknown"))
                ):
                    with self.assertRaisesRegex(AppClientError, "outcome_unknown"):
                        await client.tap("accept")
                with self.assertRaisesRegex(AppClientError, "invalid_client_state"):
                    await client.tap("accept")
                requests = [r for t, r in broker.calls if t == "app_input"]
                assert len(requests) == 2

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
