"""Host/mock app sessions preserve identity, bounds and explicit teardown."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

import pytest

from cardputer_mcp.app_events import AppEvents
from cardputer_mcp.app_models import AppCommandRequest, AppSessionOptions
from cardputer_mcp.app_sessions import AppFailure, AppSessions, MockLink, RunningApp
from cardputer_mcp.app_wire import Op, Reply, Result, State
from cardputer_mcp.backend import MockBackend


def request(run, *, command=2, seq=2, operation="action.tap", action="accept", **overrides):
    fields = dict(
        session="c" * 64,
        job_id="job",
        boot_id=run.boot,
        run_id=run.run,
        control_session=run.session,
        command_id=command,
        seq=seq,
        operation=operation,
    )
    if operation == "action.tap":
        fields["action"] = action
    return AppCommandRequest(**{**fields, **overrides})


def running(**options):
    value = RunningApp(
        "owner", AppSessionOptions(app_id="local.hello", **options), AppEvents("owner", 262144)
    )
    value.boot, value.run, value.session, value.state = 11, 7, 9, "running"
    return value


async def started(manager, backend, app="local.hello", *, job="job", live=lambda: True, **options):
    config = AppSessionOptions(app_id=app, duration_ms=1000, **options)
    plan = {"job_id": job, **manager.prepare(backend, config)}
    task = asyncio.create_task(manager.execute(backend, plan, "owner", lambda _: None, live))
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        run = manager.jobs.get(job)
        if run is not None and run.state == "running":
            return run, task
        if task.done():
            await task
            raise AssertionError("worker finished without started")
        await asyncio.sleep(0.005)
    raise AssertionError("app start deadline")


async def await_ack(run, command):
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        entry = run.commands.get(command)
        if entry is not None and entry[1]["state"] not in ("queued", "sent", "accepted"):
            return entry[1]
        await asyncio.sleep(0.005)
    raise AssertionError("command ACK deadline")


def test_order_duplicate_conflict_stale_identity_and_no_queue_reapplication():
    run = running()
    first = request(run)
    run.command(first)
    duplicate = run.command(first)
    assert duplicate["duplicate"] and run.pending.qsize() == 1
    assert run.submitted_seq == 3
    with pytest.raises(ValueError, match="conflict"):
        run.command(request(run, action="back"))
    with pytest.raises(ValueError, match="out_of_order"):
        run.command(request(run, command=3, seq=5))
    for field, value, code in (
        ("boot_id", 12, "stale_boot"),
        ("run_id", 8, "stale_run"),
        ("control_session", 10, "stale_session"),
    ):
        with pytest.raises(ValueError, match=code):
            run.command(request(run, command=3, seq=3, **{field: value}))
    unknown = run.command(request(run, command=999, seq=0, operation="status"))
    assert unknown["state"] == "outcome_unknown"
    assert run.pending.qsize() == 1 and run.submitted_seq == 3


def test_queue_full_rate_budget_plan_action_and_cancel_rejections_are_explicit():
    run = running()
    with patch("time.monotonic", side_effect=[100.0, 100.0]):
        run.command(request(run))
    with patch("time.monotonic", return_value=100.001):
        with pytest.raises(ValueError, match="rate_limit"):
            run.command(request(run, command=3, seq=3))
    with pytest.raises(ValueError, match="action_not_in_plan"):
        run.command(request(run, command=3, seq=3, action="up"))
    for command in range(3, 6):
        with patch("time.monotonic", return_value=100.0 + command):
            run.command(request(run, command=command, seq=command))
    with pytest.raises(ValueError, match="queue_full"):
        run.command(request(run, command=6, seq=6))
    assert run.pending.qsize() == 4
    assert [run.pending.get_nowait().seq for _ in range(4)] == [2, 3, 4, 5]
    limited = running(max_commands=1)
    limited.command(request(limited))
    with pytest.raises(ValueError, match="limit"):
        limited.command(request(limited, command=3, seq=3))
    limited.cancelled.set()
    with pytest.raises(ValueError, match="app_input_stopped"):
        limited.command(request(limited, command=3, seq=3))


def test_stop_has_a_reserved_queue_slot_and_survives_rate_and_command_budget():
    limited = running(max_commands=1)
    with patch("time.monotonic", return_value=100):
        limited.command(request(limited))
        stop = limited.command(request(limited, command=3, seq=3, operation="stop"))
    assert stop["state"] == "queued" and limited.pending.qsize() == 2
    full = running()
    for command in range(2, 6):
        with patch("time.monotonic", return_value=100.0 + command):
            full.command(request(full, command=command, seq=command))
    stop = full.command(request(full, command=6, seq=6, operation="stop"))
    assert stop["state"] == "queued" and full.pending.qsize() == 5
    with pytest.raises(ValueError, match="queue_full"):
        full.command(request(full, command=7, seq=7, operation="stop"))


def test_suspend_resume_preserves_instance_stop_tears_down_and_status_is_read_only():
    async def scenario():
        manager, backend = AppSessions(), MockBackend()
        run, task = await started(manager, backend)
        original = run.run
        run.command(request(run, operation="suspend"))
        assert (await await_ack(run, 2))["state"] == "ok"
        assert run.state == "suspended" and run.run == original and not task.done()
        await asyncio.sleep(0.11)
        run.command(request(run, command=3, seq=3, operation="action.tap"))
        assert (await await_ack(run, 3))["state"] == "blocked"
        await asyncio.sleep(0.11)
        run.command(request(run, command=4, seq=4, operation="resume"))
        assert (await await_ack(run, 4))["state"] == "ok"
        assert run.run == original and run.state == "running"
        await asyncio.sleep(0.11)
        run.command(request(run, command=5, seq=5))
        assert (await await_ack(run, 5))["state"] == "consumed"
        count = len(run.commands)
        status = run.command(request(run, command=5, seq=5, operation="status"))
        assert status["state"] == "consumed"
        assert len(run.commands) == count
        await asyncio.sleep(0.11)
        run.command(request(run, command=6, seq=6, operation="stop"))
        assert await asyncio.wait_for(task, 2) == "app_session_stopped"
        assert run.state == "stopped" and not run.running
        assert backend.active == 0 and backend.max_active == 1
        assert any(e["kind"] == "finished" and e["teardown_confirmed"] for e in run.events.control)
        with pytest.raises(ValueError, match="app_input_stopped"):
            run.command(request(run, command=7, seq=7))

    asyncio.run(scenario())


def test_unknown_app_and_unavailable_vmprobe_never_launch():
    async def scenario(app_id, code):
        manager, backend = AppSessions(), MockBackend()
        plan = {"job_id": "job", **manager.prepare(backend, AppSessionOptions(app_id=app_id))}
        with pytest.raises(AppFailure) as failure:
            await manager.execute(backend, plan, "owner", lambda _: None, lambda: True)
        assert failure.value.code == code
        run = manager.jobs["job"]
        assert run.run == 0 and not run.running
        assert backend.active == 0
        assert not any(e["kind"] == "started" for e in run.events.control)

    asyncio.run(scenario("unregistered.app", "unknown_app"))
    asyncio.run(scenario("local.vmprobe", "unsupported"))


def test_cancel_timeout_and_authority_loss_stop_input_and_confirm_cleanup():
    async def scenario(reason):
        manager, backend = AppSessions(), MockBackend()
        authorized = [True]
        run, task = await started(manager, backend, live=lambda: authorized[0])
        if reason == "cancelled":
            run.cancelled.set()
        elif reason == "authority":
            authorized[0] = False
        if reason == "authority":
            with pytest.raises(AppFailure) as failure:
                await asyncio.wait_for(task, 2)
            assert failure.value.code == "app_authority_lost"
        else:
            assert await asyncio.wait_for(task, 2) == "app_session_" + reason
        assert not run.running and run.state == "stopped"
        assert backend.active == 0
        assert any(e["kind"] == "finished" and e["teardown_confirmed"] for e in run.events.control)

    for reason in ("cancelled", "timeout", "authority"):
        asyncio.run(scenario(reason))


def test_disconnect_marks_unknown_without_input_replay_or_successful_teardown():
    class DisconnectLink(MockLink):
        taps = 0
        disconnected = False

        def send(self, op, session, command_id, **fields):
            if op == Op.TAP:
                type(self).taps += 1
                self.disconnected = True
            if self.disconnected:
                raise OSError("synthetic private serial exception")
            return super().send(op, session, command_id, **fields)

    async def scenario():
        manager, backend = AppSessions(), MockBackend()
        with patch("cardputer_mcp.app_sessions.MockLink", DisconnectLink):
            run, task = await started(manager, backend)
            run.command(request(run))
            with pytest.raises(AppFailure) as failure:
                await asyncio.wait_for(task, 2)
        assert failure.value.code == "outcome_unknown"
        assert DisconnectLink.taps == 1 and backend.calls == 1
        assert not run.running
        assert any(e["kind"] == "needs_review" for e in run.events.control)
        assert not any(
            e["kind"] == "finished" and e["teardown_confirmed"] for e in run.events.control
        )
        restarted = AppSessions()
        assert not restarted.jobs
        assert backend.calls == 1

    asyncio.run(scenario())


def test_sequential_fresh_jobs_use_same_device_boot_and_new_instance():
    async def scenario():
        manager, backend = AppSessions(), MockBackend()
        first, task = await started(manager, backend, job="hello")
        first_boot, first_run = first.boot, first.run
        first.command(request(first, operation="stop"))
        await asyncio.wait_for(task, 2)
        second, second_task = await started(manager, backend, "local.stress", job="stress")
        assert second.boot == first_boot and second.run != first_run
        second.command(request(second, operation="stop"))
        await asyncio.wait_for(second_task, 2)

    asyncio.run(scenario())


def test_startup_rejection_queries_state_stops_and_closes_before_next_job():
    class RejectFirstStart(MockLink):
        rejected = False

        def __init__(self, control):
            super().__init__(control)
            self.operations = []

        def send(self, op, session, command_id, **fields):
            self.operations.append(op)
            if op == Op.START and not self.rejected:
                self.rejected = True
                self.seq += 1
                reply = Reply(
                    session,
                    command_id,
                    self.boot,
                    self.run,
                    fields["seq"],
                    self.fence,
                    Result.UNSUPPORTED,
                    State.READY,
                    Op.START,
                    next_seq=self.seq,
                )
                self.ledger[command_id] = reply
                self.control(reply)
                return reply
            return super().send(op, session, command_id, **fields)

    async def scenario():
        manager, backend = AppSessions(), MockBackend()
        plan = {
            "job_id": "rejected",
            **manager.prepare(backend, AppSessionOptions(app_id="local.hello")),
        }
        with patch("cardputer_mcp.app_sessions.MockLink", RejectFirstStart):
            with pytest.raises(AppFailure) as failure:
                await manager.execute(backend, plan, "owner", lambda _: None, lambda: True)
            assert failure.value.code == "unsupported"
            device = manager.mock_link
            assert device.operations == [
                Op.CATALOG,
                Op.OPEN,
                Op.START,
                Op.STATUS,
                Op.STOP,
                Op.CLOSE,
            ]
            assert device.offer == 2 and device.state == State.NO_SESSION
            failed = manager.jobs["rejected"]
            assert not any(e["kind"] == "started" for e in failed.events.control)
            assert any(
                e["kind"] == "finished" and e["teardown_confirmed"] for e in failed.events.control
            )
            assert backend.active == 0
            recovered, task = await started(manager, backend, job="next-explicit-job")
            assert recovered.session == 2 and recovered.boot == device.boot
            recovered.command(request(recovered, operation="stop"))
            await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


def test_changed_catalog_fails_old_plan_without_launch_then_allows_new_explicit_plan():
    class RecordingLink(MockLink):
        def __init__(self, control):
            super().__init__(control)
            self.operations = []
            self.changed = False

        def catalog(self):
            catalog = super().catalog()
            if self.changed:
                return [replace(item, title=item.title + " v2") for item in catalog]
            return catalog

        def send(self, op, session, command_id, **fields):
            self.operations.append(op)
            return super().send(op, session, command_id, **fields)

    async def scenario():
        manager, backend = AppSessions(), MockBackend()
        with patch("cardputer_mcp.app_sessions.MockLink", RecordingLink):
            first, task = await started(manager, backend, job="initial")
            first.command(request(first, operation="stop"))
            await asyncio.wait_for(task, 2)
            options = AppSessionOptions(app_id="local.hello")
            stale_plan = {"job_id": "stale-plan", **manager.prepare(backend, options)}
            device = manager.mock_link
            device.changed = True
            device.operations.clear()
            with pytest.raises(AppFailure) as failure:
                await manager.execute(backend, stale_plan, "owner", lambda _: None, lambda: True)
            assert failure.value.code == "catalog_changed"
            assert device.operations == [Op.CATALOG]
            assert all(item["title"].endswith(" v2") for item in manager.list_apps()["apps"])
            new_plan = manager.prepare(backend, options)
            assert (
                new_plan["catalog_binding"]["expected"] != stale_plan["catalog_binding"]["expected"]
            )
            second, second_task = await started(manager, backend, job="new-explicit-plan")
            second.command(request(second, operation="stop"))
            await asyncio.wait_for(second_task, 2)
            assert second.run != first.run

    asyncio.run(scenario())


def test_device_pre_admission_refusal_preserves_sequence_for_explicit_new_command():
    from cardputer_mcp.app_wire import Reply, Result

    class FullOnce(MockLink):
        refused = False

        def send(self, op, session, command_id, **fields):
            if op == Op.TAP and not self.refused:
                self.refused = True
                return Reply(
                    session,
                    command_id,
                    self.boot,
                    self.run,
                    fields["seq"],
                    self.fence,
                    Result.QUEUE_FULL,
                    self.state,
                    op,
                    next_seq=self.seq,
                )
            return super().send(op, session, command_id, **fields)

    async def scenario():
        manager, backend = AppSessions(), MockBackend()
        with patch("cardputer_mcp.app_sessions.MockLink", FullOnce):
            run, task = await started(manager, backend)
            run.command(request(run))
            assert (await await_ack(run, 2))["state"] == "queue_full"
            assert run.next_seq == run.submitted_seq == 2
            await asyncio.sleep(0.11)
            run.command(request(run, command=3, seq=2))
            assert (await await_ack(run, 3))["state"] == "consumed"
            run.command(request(run, command=4, seq=3, operation="stop"))
            assert await asyncio.wait_for(task, 2) == "app_session_stopped"

    asyncio.run(scenario())
