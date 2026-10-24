"""Headless MCP info UI checks; no hardware, real credentials, or Tk display."""

import queue
import threading
from typing import Any
from unittest.mock import Mock

import pytest

from cardputer_mcp.gui import (
    Operation,
    Outcome,
    QueueController,
    QueueSnapshot,
    _Window,
    mcp_catalog_summary,
    perform,
)
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError

PRIVATE = "synthetic-private-token-schema-path"


def summary() -> dict[str, Any]:
    return {
        "gui_version": "0.13.0",
        "server_version": "0.7.0",
        "tools_count": 17,
        "read_capture": True,
        "capture_home": False,
        "broker_epoch": "0123456789abcdef0123456789abcdef",
    }


def snapshot() -> QueueSnapshot:
    return QueueSnapshot.parse(
        {
            "backend": "mock",
            "queue_revision": "revision-1",
            "sessions": [],
            "waiters": [
                {
                    "request_id": "request-a",
                    "session_id": "session-a",
                    "position": 1,
                    "state": "WAITING",
                    "wait_remaining_seconds": 10,
                }
            ],
            "owner": None,
            "needs_review": False,
            "ownership_uncertain": False,
        }
    )


def ready_controller() -> QueueController:
    controller = QueueController()
    operation = controller.refresh()
    assert operation is not None
    assert controller.accept(Outcome(operation.sequence, snapshot(), "Queue refreshed"))
    controller.selected_request = "request-a"
    assert controller.can_cancel()
    return controller


def catalog_api() -> Mock:
    api = Mock(spec=OperatorClient)
    api.catalog.return_value = summary()
    return api


def test_catalog_formatter_uses_exact_fixed_labels_and_values() -> None:
    assert mcp_catalog_summary(summary()) == (
        "gui_version: 0.13.0\n"
        "server_version: 0.7.0\n"
        "tools_count: 17\n"
        "read_capture: true\n"
        "capture_home: false\n"
        "broker_epoch: 0123456789abcdef0123456789abcdef"
    )


@pytest.mark.parametrize("count", [0, 256])
def test_catalog_formatter_accepts_count_bounds_and_unknown_version(count: int) -> None:
    data = summary()
    data.update(server_version="unknown", tools_count=count)
    text = mcp_catalog_summary(data)
    assert "server_version: unknown" in text
    assert f"tools_count: {count}" in text
    assert len(text) < 256
    assert len(text.splitlines()) == 6


@pytest.mark.parametrize(
    "field,value",
    [
        ("gui_version", "0.7.0"),
        ("gui_version", PRIVATE),
        ("server_version", PRIVATE),
        ("server_version", "0.7.1\n" + PRIVATE),
        ("server_version", "1" * 512),
        ("server_version", None),
        ("tools_count", -1),
        ("tools_count", 257),
        ("tools_count", True),
        ("tools_count", "17"),
        ("read_capture", 1),
        ("read_capture", PRIVATE),
        ("capture_home", None),
        ("capture_home", "false"),
        ("broker_epoch", "A" * 32),
        ("broker_epoch", "a" * 31),
        ("broker_epoch", "a" * 33),
        ("broker_epoch", PRIVATE),
        ("broker_epoch", None),
        ("tool_names", [PRIVATE]),
        ("input_schema", {"private": PRIVATE}),
        ("headers", {"Authorization": PRIVATE}),
        ("token", PRIVATE),
        ("path", PRIVATE),
    ],
)
def test_catalog_formatter_rejects_extra_or_malformed_fields_without_echoing(
    field: str,
    value: Any,
) -> None:
    data = summary()
    data[field] = value
    with pytest.raises(OperatorClientError) as error:
        mcp_catalog_summary(data)
    assert str(error.value) == "The broker returned invalid MCP info."
    assert PRIVATE not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("field", list(summary()))
def test_catalog_formatter_requires_all_fixed_fields(field: str) -> None:
    data = summary()
    del data[field]
    with pytest.raises(OperatorClientError, match="^The broker returned invalid MCP info.$"):
        mcp_catalog_summary(data)


def test_catalog_uses_serialized_operation_without_extending_probe_with_state_call() -> None:
    controller = ready_controller()
    previous = controller.snapshot
    operation = controller.catalog()
    assert operation == Operation(2, "catalog")
    assert controller.message == "Reading MCP info..."
    assert controller.busy
    assert controller.catalog() is None
    assert controller.diagnostics() is None
    assert controller.refresh() is None
    assert controller.cancel() is None
    api = catalog_api()
    outcome = perform(api, operation)
    assert [call[0] for call in api.mock_calls] == ["catalog"]
    assert outcome.sequence == operation.sequence
    assert outcome.message == "MCP info loaded"
    assert outcome.catalog == mcp_catalog_summary(summary())
    assert outcome.diagnostics is None and outcome.snapshot is None
    assert not outcome.failed
    assert controller.accept(outcome)
    assert controller.snapshot is previous
    assert controller.selected_request == "request-a"
    assert not controller.fresh and not controller.busy
    assert not controller.can_cancel()
    assert not controller.accept(outcome)
    refresh = controller.refresh()
    assert refresh is not None
    assert controller.accept(Outcome(refresh.sequence, snapshot(), "Queue refreshed"))
    assert controller.can_cancel()


@pytest.mark.parametrize("failure", ["malformed", "unexpected", "timeout"])
def test_catalog_failure_retains_visible_queue_without_echoing_payload_or_refreshing(
    failure: str,
) -> None:
    controller = ready_controller()
    previous = controller.snapshot
    api = catalog_api()
    if failure == "malformed":
        api.catalog.return_value["headers"] = {"Authorization": PRIVATE}
        expected = "The broker returned invalid MCP info."
    elif failure == "unexpected":
        api.catalog.side_effect = RuntimeError(PRIVATE)
        expected = "MCP info failed. Check the local broker."
    else:
        expected = "MCP catalog probe timed out. The broker was not restarted."
        api.catalog.side_effect = OperatorClientError(expected)
    operation = controller.catalog()
    assert operation is not None
    outcome = perform(api, operation)
    assert [call[0] for call in api.mock_calls] == ["catalog"]
    assert outcome.failed and outcome.catalog is None and outcome.snapshot is None
    assert outcome.message == expected
    assert PRIVATE not in repr(outcome)
    assert controller.accept(outcome)
    assert controller.snapshot is previous
    assert not controller.fresh and not controller.can_cancel()
    assert controller.catalog() is not None


def test_catalog_runs_on_existing_background_worker_without_tk_or_main_thread_network() -> None:
    window = object.__new__(_Window)
    window.api = api = catalog_api()
    window.controller = QueueController()
    window.stopped = threading.Event()
    window.commands = queue.Queue()
    window.results = queue.Queue()
    window._controls = Mock()
    window._render = Mock(side_effect=AssertionError("Worker must not render Tk"))
    window._show_catalog = Mock(side_effect=AssertionError("Worker must not open Tk dialog"))
    threads = []

    def catalog() -> dict[str, Any]:
        threads.append(threading.get_ident())
        return summary()

    api.catalog.side_effect = catalog
    window._submit(window.controller.catalog())
    api.catalog.assert_not_called()
    worker = threading.Thread(target=window._worker, daemon=True)
    worker.start()
    try:
        outcome = window.results.get(timeout=2)
        assert outcome.catalog == mcp_catalog_summary(summary())
        assert threads == [worker.ident]
        assert threads[0] != threading.get_ident()
        assert [call[0] for call in api.mock_calls] == ["catalog"]
    finally:
        window.stopped.set()
        window.commands.put(None)
        worker.join(timeout=2)
    assert not worker.is_alive()
    window._show_catalog.assert_not_called()
    window._render.assert_not_called()


@pytest.mark.parametrize("disposition", ["accepted", "stale", "closed", "stopped"])
def test_catalog_poll_shows_only_current_result_once_and_never_after_close(
    disposition: str,
) -> None:
    window = object.__new__(_Window)
    window.controller = QueueController()
    window.stopped = threading.Event()
    window.root = Mock()
    window.results = queue.Queue()
    window._render = Mock()
    window._show_catalog = Mock()
    window._show_diagnostics = Mock()
    window._render_plan_badge = Mock()
    operation = window.controller.catalog()
    assert operation is not None
    outcome = perform(catalog_api(), operation)
    window.results.put(outcome)
    window.results.put(outcome)
    if disposition == "stale":
        assert window.controller.accept(outcome)
        assert window.controller.refresh() is not None
    elif disposition == "closed":
        window.controller.close()
        assert window.controller.catalog() is None
    elif disposition == "stopped":
        window.stopped.set()
    window._poll()
    if disposition == "accepted":
        window._render.assert_called_once()
        window._show_catalog.assert_called_once_with(outcome.catalog)
    else:
        window._render.assert_not_called()
        window._show_catalog.assert_not_called()
    window._show_diagnostics.assert_not_called()


@pytest.mark.parametrize("state", ["idle", "busy", "closed"])
def test_catalog_button_is_disabled_while_busy_or_closed(state: str) -> None:
    window = object.__new__(_Window)
    window.controller = QueueController()
    for name in (
        "cancel_button",
        "up_button",
        "down_button",
        "release_button",
        "refresh_button",
        "diagnostics_button",
        "catalog_button",
        "status",
        "status_label",
    ):
        setattr(window, name, Mock())
    if state == "busy":
        assert window.controller.catalog() is not None
    elif state == "closed":
        window.controller.close()
    window._controls()
    window.catalog_button.configure.assert_called_once_with(
        state="normal" if state == "idle" else "disabled"
    )
