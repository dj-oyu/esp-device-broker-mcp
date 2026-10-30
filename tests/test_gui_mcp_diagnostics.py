"""Headless MCP info UI checks; no hardware, real credentials, or Tk display."""

from typing import Any
from unittest.mock import Mock

import pytest

from cardputer_mcp.gui import (
    Outcome,
    QueueController,
    QueueSnapshot,
    mcp_catalog_summary,
    perform,
)
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError

PRIVATE = "synthetic-private-token-schema-path"


def summary() -> dict[str, Any]:
    return {
        "gui_version": "0.14.0rc1",
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
        "gui_version: 0.14.0rc1\n"
        "server_version: 0.7.0\n"
        "tools_count: 17\n"
        "read_capture: true\n"
        "capture_home: false\n"
        "broker_epoch: 0123456789abcdef0123456789abcdef"
    )


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
