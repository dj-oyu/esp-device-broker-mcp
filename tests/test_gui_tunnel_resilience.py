"""Tunnel panel liveness when a status poll, worker, or Tk callback misbehaves (#126).

No Tk display, Tunnel client, cloud account, or USB device is used: a fake root
records `after` callbacks, and fake managers park or fail on purpose.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from cardputer_mcp import gui_tunnel as gt
from cardputer_mcp.gui_tunnel import (
    POLL_RESULT_SECONDS,
    POLL_STALLED,
    REVIEW_WARNING,
    WORKER_STOPPED,
    TunnelController,
    TunnelOperation,
    TunnelOutcome,
    TunnelPanel,
    TunnelView,
    TunnelWorker,
)

URL = "http://127.0.0.1:8765/mcp"
PRIVATE = "SYNTHETIC_PANEL_SECRET_TEXT"


def snapshot(**changes: Any) -> dict[str, Any]:
    return {
        "state": "running",
        "status_code": "tunnel_started",
        "configured": True,
        "owned": True,
        "process_alive": True,
        "local_readiness": False,
        "cloud_poll_observed": False,
        "app_verified": False,
        "ownership_uncertain": False,
        "exit_code": None,
        **changes,
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ParkedManager:
    """poll() parks until released; control actions stay independent of it."""

    def __init__(self) -> None:
        self.data = snapshot()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []
        self.starts = 1

    def configure(self, settings: Any, broker_url: str, *, enable_serial: bool = False) -> Any:
        raise AssertionError("not used")

    def connect(self, api_key: str, bearer: str) -> dict[str, Any]:
        self.calls.append("connect")
        self.starts += 1
        self.data = snapshot()
        return dict(self.data)

    def poll(self) -> dict[str, Any]:
        self.calls.append("poll")
        self.entered.set()
        assert self.release.wait(10), "test never released the parked poll"
        return snapshot(status_code="tunnel_cloud_poll_observed", cloud_poll_observed=True)

    def disconnect(self) -> dict[str, Any]:
        self.calls.append("disconnect")
        self.data = snapshot(
            state="disconnected", status_code="tunnel_disconnected", owned=False,
            process_alive=False,
        )  # fmt: skip
        return dict(self.data)

    def close(self) -> bool:
        self.calls.append("close")
        self.data = snapshot(
            state="closed", status_code="tunnel_closed", owned=False, process_alive=False
        )
        return True

    def snapshot(self) -> dict[str, Any]:
        return dict(self.data)


class FakeRoot:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, Any]] = []

    def after(self, delay: int, callback: Any) -> None:
        self.scheduled.append((delay, callback))

    def pending(self, callback: Any) -> int:
        return sum(1 for _, scheduled in self.scheduled if scheduled == callback)


def running_controller(clock: Clock) -> TunnelController:
    model = TunnelController(monotonic=clock)
    model.view = TunnelView.parse(snapshot())
    return model


def wait_result(worker: TunnelWorker, timeout: float = 5.0) -> TunnelOutcome:
    return worker.results.get(timeout=timeout)


@pytest.fixture
def panel():
    clock = Clock()
    manager = ParkedManager()
    root = FakeRoot()
    created = TunnelPanel(root, URL, manager=manager)  # type: ignore[arg-type]
    created.controller = running_controller(clock)
    # No Tk display in tests: the dialog/prompt openers are recorded, not built.
    created.shown = []  # type: ignore[attr-defined]
    created.show = lambda: created.shown.append(True)  # type: ignore[attr-defined, method-assign]
    created._credentials = lambda: None  # type: ignore[method-assign]
    yield created, manager, root, clock
    manager.release.set()
    created.worker.stop()


def test_controller_deadline_clears_only_the_poll_and_ignores_its_late_result():
    clock = Clock()
    model = running_controller(clock)
    operation = model.poll()
    assert operation is not None and model.busy and model.message == "Checking Tunnel status..."
    assert not model.can_disconnect()
    clock.now += POLL_RESULT_SECONDS - 0.01
    assert not model.expire() and model.busy
    clock.now += 0.02
    assert model.expire()
    assert not model.busy and model.failed and model.message == POLL_STALLED
    assert model.view.owned and model.view.process_alive  # Ownership is never faked away.
    assert model.can_disconnect()
    assert model.poll() is None  # No second probe is queued behind the stalled one.
    late = TunnelOutcome(
        operation.sequence, "poll", TunnelView.parse(snapshot(owned=False)), "late", False
    )
    assert model.accept(late) is False
    assert model.view.owned and model.message == POLL_STALLED
    assert model.stalled_poll is None and model.poll() is not None


@pytest.mark.parametrize("action", ["connect", "disconnect", "close"])
def test_deadline_never_applies_to_control_actions(action):
    clock = Clock()
    model = running_controller(clock)
    model.view = TunnelView.parse(snapshot(owned=action != "connect", process_alive=False))
    if action == "connect":
        model.view = TunnelView.parse(
            snapshot(state="disconnected", owned=False, process_alive=False)
        )
        operation = model.connect("k", "r" * 64)
    elif action == "disconnect":
        operation = model.disconnect()
    else:
        operation = model.request_close()
    assert operation is not None
    clock.now += POLL_RESULT_SECONDS * 10
    assert not model.expire() and model.busy and model.action == action


def test_stalled_poll_on_its_own_thread_never_blocks_disconnect():
    manager = ParkedManager()
    worker = TunnelWorker(manager, URL, Path("."))  # type: ignore[arg-type]
    clock = Clock()
    model = running_controller(clock)
    try:
        poll = model.poll()
        assert poll is not None
        worker.submit(poll)
        assert manager.entered.wait(5)
        clock.now += POLL_RESULT_SECONDS
        assert model.expire()
        disconnect = model.disconnect()
        assert disconnect is not None
        worker.submit(disconnect)
        result = wait_result(worker)
        assert result.action == "disconnect" and model.accept(result)
        assert not model.view.owned and not model.stop_unconfirmed and not model.failed
        manager.release.set()
        late = wait_result(worker)
        assert late.action == "poll" and late.sequence == poll.sequence
        assert model.accept(late) is False and not model.view.owned
        assert manager.starts == 1  # A late poll never (re)starts a client.
    finally:
        manager.release.set()
        worker.stop()


class Escape(BaseException):
    pass


def test_base_exception_in_an_operation_yields_a_failed_result_and_worker_survives():
    manager = ParkedManager()

    def explode() -> dict[str, Any]:
        raise Escape(PRIVATE)

    manager.poll = explode  # type: ignore[method-assign]
    worker = TunnelWorker(manager, URL, Path("."))  # type: ignore[arg-type]
    try:
        worker.submit(TunnelOperation(1, "poll"))
        result = wait_result(worker)
        assert result.failed and result.sequence == 1 and PRIVATE not in repr(result)
        assert worker.poll_thread.is_alive()
        worker.submit(TunnelOperation(2, "disconnect"))
        result = wait_result(worker)
        assert result.action == "disconnect" and not result.failed
    finally:
        worker.stop()


def test_lost_control_result_keeps_credentials_out_and_ownership_uncertain():
    manager = ParkedManager()

    def explode(api_key: str, bearer: str) -> dict[str, Any]:
        raise Escape(PRIVATE + api_key)

    manager.connect = explode  # type: ignore[method-assign]
    worker = TunnelWorker(manager, URL, Path("."))  # type: ignore[arg-type]
    model = TunnelController()
    model.view = TunnelView.parse(snapshot(state="disconnected", owned=False, process_alive=False))
    model.path = Path("broker.toml")
    try:
        operation = model.connect("api-" + PRIVATE, "r" * 64)
        assert operation is not None
        worker.submit(operation)
        result = wait_result(worker)
        assert operation.api_key == operation.bearer == ""
        assert result.view is None and PRIVATE not in repr(result)
        assert model.accept(result) and model.stop_unconfirmed and model.failed
    finally:
        worker.stop()


def dead_thread() -> threading.Thread:
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    return thread


def test_dead_poll_thread_is_detected_and_replaced_on_next_poll(panel):
    created, manager, root, clock = panel
    model = created.controller
    operation = model.poll()
    assert operation is not None
    created.worker.poll_thread = dead_thread()  # The result can now never arrive.
    created._drain()
    assert not model.busy and model.failed and model.message == WORKER_STOPPED
    assert model.view.owned and model.can_disconnect()
    assert created.diagnostics[-1] == "tunnel_worker_stopped: poll"
    manager.release.set()
    created._poll()  # A fresh poll thread serves the next status check.
    assert created.worker.poll_thread.is_alive()
    result = wait_result(created.worker)
    assert result.action == "poll" and model.accept(result)


def test_dead_control_thread_during_disconnect_fails_closed(panel):
    created, manager, root, clock = panel
    model = created.controller
    operation = model.disconnect()
    assert operation is not None
    stale = queue.Queue()
    created.worker.commands = stale
    created.worker.thread = dead_thread()
    created._drain()
    assert not model.busy and model.stop_unconfirmed
    assert model.message == REVIEW_WARNING and model.view.owned
    retry = model.disconnect()  # Disconnect remains available and gets a live worker.
    assert retry is not None
    created._submit(retry)
    assert created.worker.thread.is_alive() and created.worker.commands is not stale
    result = wait_result(created.worker)
    assert result.action == "disconnect" and model.accept(result) and not model.view.owned


def test_panel_deadline_records_a_path_free_worker_stack_and_ignores_late_result(panel):
    created, manager, root, clock = panel
    model = created.controller
    created._poll()
    assert model.busy and model.action == "poll" and manager.entered.wait(5)
    created._drain()
    assert model.busy  # Within the deadline nothing changes.
    clock.now += POLL_RESULT_SECONDS
    created._drain()
    assert not model.busy and model.message == POLL_STALLED and model.can_disconnect()
    record = created.diagnostics[-1]
    assert record.startswith("tunnel_poll_deadline: ")
    assert "ParkedManager" not in record and ":poll:" in record  # Frame names only.
    assert "\\" not in record and "/" not in record and PRIVATE not in record
    assert len(record) <= gt.DIAGNOSTIC_CHARS
    sequence = model.stalled_poll
    created._poll()  # Stalled: no new poll is queued behind the parked one.
    assert manager.calls.count("poll") == 1 and model.stalled_poll == sequence
    manager.release.set()
    deadline = time.monotonic() + 5
    while model.stalled_poll is not None and time.monotonic() < deadline:
        created._drain()
        time.sleep(0.01)
    assert model.stalled_poll is None and model.message == POLL_STALLED
    assert model.view.cloud_poll_success is None  # Late evidence was not applied.


@pytest.mark.parametrize("loop", ["_poll", "_drain"])
def test_tk_callback_exception_keeps_the_loop_scheduled_without_echo(panel, loop):
    created, manager, root, clock = panel
    manager.release.set()
    calls = []

    def broken_render() -> None:
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError(PRIVATE)

    created._render = broken_render  # type: ignore[method-assign]
    if loop == "_drain":
        created.worker.results.put(
            TunnelOutcome(model_sequence(created), "poll", TunnelView.parse(snapshot()), "ok")
        )
    callback = getattr(created, loop)
    before = root.pending(callback)
    callback()
    assert root.pending(callback) == before + 1
    record = created.diagnostics[-1]
    assert record.startswith("RuntimeError: ") and PRIVATE not in record
    assert "\\" not in record and "/" not in record
    assert created.controller.failed and created.controller.message == gt.PANEL_FAILED


def model_sequence(created: TunnelPanel) -> int:
    operation = created.controller.poll()
    assert operation is not None
    return operation.sequence


def test_reschedule_failure_is_contained_and_stopped_worker_ends_loops(panel):
    created, manager, root, clock = panel
    manager.release.set()

    def broken_after(delay: int, callback: Any) -> None:
        raise RuntimeError(PRIVATE)

    root.after = broken_after  # type: ignore[method-assign]
    created._drain()  # Must not raise into Tk.
    assert PRIVATE not in created.diagnostics[-1]
    root.after = FakeRoot.after.__get__(root)  # type: ignore[method-assign]
    created.worker.stop()
    count = len(root.scheduled)
    created._drain()
    created._poll()
    assert len(root.scheduled) == count
