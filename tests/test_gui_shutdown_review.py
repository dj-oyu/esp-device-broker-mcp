"""Closing the read-only GUI must preserve an in-flight cancellation guard."""

from unittest.mock import Mock

from cardputer_mcp.gui import _Window


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
