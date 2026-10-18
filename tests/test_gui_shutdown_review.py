"""Closing the read-only GUI must preserve an in-flight cancellation guard."""

import queue
import threading
from unittest.mock import Mock

from cardputer_mcp.gui import QueueController, _Window


def test_main_close_waits_for_pending_timeline_request_before_touching_tunnel():
    window = object.__new__(_Window)
    window.timeline_panel = Mock()
    window.timeline_panel.can_close_window.return_value = False
    window.tunnel_panel, window.root, window.api = Mock(), Mock(), Mock()
    window._close()
    window.timeline_panel.can_close_window.assert_called_once()
    window.tunnel_panel.request_close.assert_not_called()
    window.root.destroy.assert_not_called()
    assert window.api.mock_calls == []


def test_main_close_rechecks_pending_timeline_after_tunnel_shutdown_delay():
    window = object.__new__(_Window)
    window.timeline_panel = Mock()
    window.timeline_panel.can_close_window.return_value = False
    window.controller = QueueController()
    window.root = Mock()
    window.stopped, window.commands = threading.Event(), queue.Queue()
    window._finish_close()
    window.timeline_panel.stop.assert_not_called()
    window.root.destroy.assert_not_called()
    assert not window.stopped.is_set() and not window.controller.closed
    assert window.commands.empty()
