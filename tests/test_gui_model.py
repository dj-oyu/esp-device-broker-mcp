"""Headless GUI and local API tests: synthetic identities, no hardware or secrets."""

import copy
import queue
import threading
from typing import Any

import httpx2
import pytest

from cardputer_mcp.gui import (
    Operation,
    Outcome,
    QueueController,
    QueueSnapshot,
    _Window,
    perform,
    ttl,
)
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError, operator_base_url


def state() -> dict[str, Any]:
    return {
        "ok": True,
        "backend": "mock",
        "queue_revision": "revision-1",
        "sessions": [
            {
                "session_id": "session-a",
                "owner": "Test operator",
                "principal_id": "principal-a",
                "scopes": ["read", "control"],
                "state": "ACTIVE",
                "expires_in_seconds": 42.4,
                "request_id": "request-a",
            }
        ],
        "waiters": [
            {
                "request_id": "request-b",
                "session_id": "session-b",
                "position": 2,
                "state": "WAITING",
                "lease_id": None,
                "wait_remaining_seconds": 80.2,
                "lease_remaining_seconds": None,
                "active_job": None,
            },
            {
                "request_id": "request-a",
                "session_id": "session-a",
                "position": 1,
                "state": "WAITING",
                "lease_id": None,
                "wait_remaining_seconds": 90,
                "lease_remaining_seconds": None,
                "active_job": None,
            },
        ],
        "owner": {
            "lease_id": "lease-owner",
            "request_id": "request-owner",
            "session_id": "session-owner",
            "owner": "Other operator",
            "principal_id": "principal-owner",
            "lease_remaining_seconds": 12,
            "release_blocked": False,
        },
        "active_job": None,
        "needs_review": False,
        "ownership_uncertain": False,
    }


class FakeAPI:
    def __init__(self) -> None:
        self.data = state()
        self.calls: list[tuple[Any, ...]] = []
        self.action_error: Exception | None = None
        self.state_error: Exception | None = None

    def state(self) -> dict[str, Any]:
        self.calls.append(("state",))
        if self.state_error:
            raise self.state_error
        return copy.deepcopy(self.data)

    def _action(self, *args: Any) -> dict[str, Any]:
        self.calls.append(args)
        if self.action_error:
            raise self.action_error
        return {"ok": True}

    def cancel(self, request_id: str) -> dict[str, Any]:
        return self._action("cancel", request_id)

    def reorder(self, request_id: str, position: int, expected_revision: str) -> dict[str, Any]:
        return self._action("reorder", request_id, position, expected_revision)

    def release(self, lease_id: str) -> dict[str, Any]:
        return self._action("release", lease_id)


def ready(data: dict[str, Any] | None = None) -> QueueController:
    controller = QueueController()
    operation = controller.refresh()
    assert operation is not None
    assert controller.accept(
        Outcome(operation.sequence, QueueSnapshot.parse(data or state()), "OK")
    )
    return controller


def test_snapshot_only_keeps_public_fields_and_sorts_fifo() -> None:
    data = state()
    data["resume_token"] = "synthetic-secret"
    data["sessions"][0]["resume_token"] = "synthetic-secret"
    data["owner"]["token"] = "synthetic-secret"
    snapshot = QueueSnapshot.parse(data)
    assert snapshot.backend == "MOCK"
    assert [row.request_id for row in snapshot.waiters] == ["request-a", "request-b"]
    assert snapshot.sessions[0].scopes == "read, control"
    assert "synthetic-secret" not in repr(snapshot)
    assert ttl(snapshot.sessions[0].expires_in_seconds) == "43 s"
    assert ttl(0) == "0 s"
    assert ttl(None) == "--"


@pytest.mark.parametrize(
    "field,value",
    [
        ("backend", "unknown"),
        ("needs_review", "false"),
        ("ownership_uncertain", None),
        ("sessions", {}),
        ("waiters", {}),
    ],
)
def test_invalid_snapshot_rejected_with_fixed_message(field: str, value: Any) -> None:
    data = state()
    data[field] = value
    with pytest.raises(
        OperatorClientError, match="^The broker returned an invalid queue snapshot.$"
    ):
        QueueSnapshot.parse(data)


@pytest.mark.parametrize("position", [0, -1, True, 1.5, None, "1"])
def test_waiting_positions_must_be_positive_integers(position: Any) -> None:
    data = state()
    data["waiters"][0]["position"] = position
    with pytest.raises(OperatorClientError):
        QueueSnapshot.parse(data)


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "five"])
def test_invalid_ttls_rejected(value: Any) -> None:
    data = state()
    data["sessions"][0]["expires_in_seconds"] = value
    with pytest.raises(OperatorClientError):
        QueueSnapshot.parse(data)


def test_duplicate_ids_or_positions_rejected() -> None:
    data = state()
    data["waiters"][0]["request_id"] = "request-a"
    with pytest.raises(OperatorClientError):
        QueueSnapshot.parse(data)
    data = state()
    data["waiters"][0]["position"] = 1
    with pytest.raises(OperatorClientError):
        QueueSnapshot.parse(data)


def test_no_actions_before_authoritative_refresh() -> None:
    controller = QueueController()
    controller.selected_request = "request-a"
    controller.selected_lease = "lease-owner"
    assert controller.cancel() is None
    assert controller.move(1) is None
    assert controller.release("lease-owner") is None


def test_busy_guards_duplicate_clicks_and_operation_contains_public_id_only() -> None:
    controller = ready()
    controller.selected_request = "request-a"
    operation = controller.cancel()
    assert operation is not None and operation.target == "request-a"
    assert operation.action == "cancel"
    assert controller.busy
    assert controller.cancel() is None
    assert controller.refresh() is None
    assert controller.move(1) is None
    assert not controller.can_release()


def test_queue_moves_are_one_based_and_bounded() -> None:
    controller = ready()
    controller.selected_request = "request-a"
    assert not controller.can_move(-1)
    assert controller.can_move(1)
    assert controller.move(1) == Operation(2, "reorder", "request-a", 2, "revision-1")
    controller = ready()
    controller.selected_request = "request-b"
    assert not controller.can_move(1)
    assert controller.can_move(-1)
    assert controller.move(-1) == Operation(2, "reorder", "request-b", 1, "revision-1")
    assert not controller.can_move(0)


@pytest.mark.parametrize("status", ["GRANTED", "ACTIVE", "CANCELLED", "EXPIRED", "FAILED"])
def test_only_waiting_request_can_be_cancelled_or_reordered(status: str) -> None:
    data = state()
    data["waiters"][0]["state"] = status
    data["waiters"][0]["position"] = None
    controller = ready(data)
    controller.selected_request = "request-b"
    assert controller.cancel() is None
    assert controller.move(-1) is None


def test_release_requires_matching_selected_generation_and_idle_owner() -> None:
    controller = ready()
    assert controller.release("lease-owner") is None
    controller.selected_lease = "lease-owner"
    assert controller.can_release()
    assert controller.release("old-lease") is None
    assert controller.release("lease-owner") == Operation(2, "release", "lease-owner")


@pytest.mark.parametrize(
    "field,value",
    [
        ("active_job", "job-running"),
        ("needs_review", True),
        ("ownership_uncertain", True),
    ],
)
def test_unsafe_ownership_release_disabled(field: str, value: Any) -> None:
    data = state()
    data[field] = value
    controller = ready(data)
    controller.selected_lease = "lease-owner"
    assert not controller.can_release()
    assert controller.release("lease-owner") is None


def test_backend_busy_blocks_release_and_expired_active_owner_remains_visible() -> None:
    data = state()
    data["owner"]["release_blocked"] = True
    data["owner"]["lease_remaining_seconds"] = 0
    controller = ready(data)
    controller.selected_lease = "lease-owner"
    assert controller.snapshot and controller.snapshot.owner
    assert controller.snapshot.owner.lease_remaining_seconds == 0
    assert not controller.can_release()


def test_stale_refresh_cannot_overwrite_current_action_result() -> None:
    controller = ready()
    controller.selected_request = "request-a"
    operation = controller.cancel()
    assert operation is not None
    assert not controller.accept(Outcome(1, QueueSnapshot.parse(state()), "Old state"))
    assert controller.busy
    changed = state()
    changed["waiters"] = []
    assert controller.accept(Outcome(operation.sequence, QueueSnapshot.parse(changed), "Cancelled"))
    assert controller.selected_request == ""
    assert not controller.accept(
        Outcome(operation.sequence, QueueSnapshot.parse(state()), "Repeat")
    )
    assert controller.snapshot and not controller.snapshot.waiters


def test_owner_change_during_confirmation_invalidates_release() -> None:
    controller = ready()
    controller.selected_lease = "lease-owner"
    operation = controller.refresh()
    assert operation is not None
    changed = state()
    changed["owner"]["lease_id"] = "lease-new"
    controller.accept(Outcome(operation.sequence, QueueSnapshot.parse(changed), "Refreshed"))
    assert controller.selected_lease == ""
    assert controller.release("lease-owner") is None


def test_error_preserves_visible_snapshot_but_disables_actions() -> None:
    controller = ready()
    controller.selected_request = "request-a"
    operation = controller.refresh()
    assert operation is not None
    controller.accept(Outcome(operation.sequence, None, "Cannot connect", True))
    assert controller.snapshot is not None
    assert not controller.fresh
    assert controller.failed and controller.message == "Cannot connect"
    assert not controller.can_cancel()
    assert controller.refresh() is not None


@pytest.mark.parametrize(
    "operation,expected",
    [
        (Operation(9, "cancel", "request-a"), ("cancel", "request-a")),
        (
            Operation(9, "reorder", "request-b", 1, "revision-1"),
            ("reorder", "request-b", 1, "revision-1"),
        ),
        (Operation(9, "release", "lease-owner"), ("release", "lease-owner")),
    ],
)
def test_worker_refreshes_after_each_mutation(
    operation: Operation, expected: tuple[Any, ...]
) -> None:
    api = FakeAPI()
    outcome = perform(api, operation)
    assert api.calls == [expected, ("state",)]
    assert outcome.sequence == 9 and outcome.snapshot and not outcome.failed


def test_refused_mutation_refreshes_and_does_not_retry() -> None:
    api = FakeAPI()
    api.action_error = OperatorClientError("Operation refused")
    api.data["active_job"] = "job-new"
    outcome = perform(api, Operation(3, "release", "lease-owner"))
    assert api.calls == [("release", "lease-owner"), ("state",)]
    assert outcome.failed and outcome.message == "Operation refused"
    assert outcome.snapshot and outcome.snapshot.active_job == "job-new"


def test_failed_refresh_after_action_does_not_claim_certain_completion() -> None:
    api = FakeAPI()
    api.state_error = OperatorClientError("Cannot reach broker")
    outcome = perform(api, Operation(3, "cancel", "request-a"))
    assert outcome.failed and outcome.snapshot is None
    assert outcome.message == "Action outcome may be uncertain. Cannot reach broker"


def test_unexpected_exception_text_is_not_exposed() -> None:
    api = FakeAPI()
    api.action_error = RuntimeError("synthetic-private-data")
    outcome = perform(api, Operation(3, "cancel", "request-a"))
    assert outcome.failed and "synthetic-private-data" not in outcome.message
    api.state_error = RuntimeError("synthetic-private-data")
    outcome = perform(api, Operation(4, "refresh"))
    assert outcome.failed and "synthetic-private-data" not in outcome.message


def test_close_invalidates_inflight_result() -> None:
    controller = ready()
    operation = controller.refresh()
    assert operation is not None
    controller.close()
    assert not controller.accept(perform(FakeAPI(), operation))
    assert controller.refresh() is None
    assert not controller.fresh


def test_background_worker_does_not_call_tk_and_close_does_not_touch_broker() -> None:
    api = FakeAPI()
    window = object.__new__(_Window)
    window.api = api
    window.stopped = threading.Event()
    window.commands = queue.Queue()
    window.results = queue.Queue()
    worker = threading.Thread(target=window._worker, daemon=True)
    worker.start()
    window.commands.put(Operation(1, "refresh"))
    assert window.results.get(timeout=2).snapshot is not None
    window.stopped.set()
    window.commands.put(None)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert api.calls == [("state",)]


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:9000",
        "http://localhost:9000",
        "http://example.com:9000",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:9000/other",
        "http://127.0.0.1:9000/mcp?token=synthetic",
        "http://127.0.0.1:9000/#secret",
        "http://user:secret@127.0.0.1:9000",
        "http://127.0.0.1:bad",
        "http://[::1]:9000",
        " http://127.0.0.1:9000",
        "http://127.0.0.1:9000\n",
    ],
)
def test_client_rejects_nonlocal_or_credential_bearing_urls(url: str) -> None:
    with pytest.raises(OperatorClientError) as error:
        OperatorClient(url, "synthetic-local-token")
    assert str(error.value) == "Use a local http://127.0.0.1:PORT broker URL."


@pytest.mark.parametrize("suffix", ["", "/", "/mcp"])
def test_client_accepts_only_loopback_base_or_mcp_url(suffix: str) -> None:
    assert operator_base_url("http://127.0.0.1:9000" + suffix) == "http://127.0.0.1:9000"


def install_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[dict[str, Any]]:
    original = httpx2.Client
    options: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> httpx2.Client:
        options.append(kwargs)
        return original(transport=httpx2.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx2, "Client", factory)
    return options


def test_client_auth_is_header_only_no_proxy_redirects_or_cookie_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        received.append(request)
        return httpx2.Response(200, json=state(), headers={"Set-Cookie": "tracking=secret"})

    options = install_transport(monkeypatch, handler)
    client = OperatorClient("http://127.0.0.1:9000/mcp", "synthetic-local-token")
    client.state()
    client.cancel("request-a")
    client.reorder("request-b", 1, "revision-1")
    client.release("lease-owner")
    assert len(options) == 4
    for config, request in zip(options, received, strict=True):
        assert config["trust_env"] is False and config["follow_redirects"] is False
        assert config["timeout"].connect == 2
        assert config["timeout"].read == 5
        assert request.headers["Authorization"] == "Bearer synthetic-local-token"
        assert "cookie" not in request.headers
        assert "synthetic-local-token" not in str(request.url)
        assert b"synthetic-local-token" not in request.content
    assert [request.url.path for request in received] == [
        "/operator/state",
        "/operator/cancel",
        "/operator/reorder",
        "/operator/release",
    ]
    assert received[1].content == b'{"request_id":"request-a"}'
    assert (
        received[2].content
        == b'{"request_id":"request-b","position":1,"expected_revision":"revision-1"}'
    )
    assert received[3].content == b'{"lease_id":"lease-owner"}'
    assert "synthetic-local-token" not in repr(client)


@pytest.mark.parametrize(
    "status,body",
    [
        (401, {"detail": "synthetic-secret"}),
        (403, {"detail": "synthetic-secret"}),
        (500, {"detail": "synthetic-secret"}),
        (302, {"detail": "synthetic-secret"}),
        (200, {"ok": False, "error": "synthetic-secret"}),
        (200, {"detail": "synthetic-secret"}),
        (200, ["synthetic-secret"]),
    ],
)
def test_server_errors_are_fixed_and_never_echoed(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: Any,
) -> None:
    install_transport(monkeypatch, lambda _: httpx2.Response(status, json=body))
    with pytest.raises(OperatorClientError) as error:
        OperatorClient("http://127.0.0.1:9000", "synthetic-local-token").state()
    assert "synthetic-secret" not in str(error.value)
    assert "synthetic-local-token" not in str(error.value)


def test_transport_exception_and_invalid_json_are_not_echoed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("synthetic-secret")

    install_transport(monkeypatch, broken)
    client = OperatorClient("http://127.0.0.1:9000", "synthetic-local-token")
    with pytest.raises(OperatorClientError) as error:
        client.state()
    assert "synthetic-secret" not in str(error.value)
    assert error.value.__suppress_context__


def test_stale_reorder_is_rejected_then_refresh_uses_new_revision() -> None:
    class RevisionAPI(FakeAPI):
        def reorder(self, request_id: str, position: int, expected_revision: str) -> dict[str, Any]:
            self.calls.append(("reorder", request_id, position, expected_revision))
            if expected_revision != self.data["queue_revision"]:
                raise OperatorClientError("Operation refused: the selection changed.")
            return {"ok": True}

    api = RevisionAPI()
    controller = ready()
    controller.selected_request = "request-b"
    operation = controller.move(-1)
    assert operation is not None and operation.expected_revision == "revision-1"
    # Another operator reorders the same two entries before the pending request.
    api.data["queue_revision"] = "revision-2"
    api.data["waiters"][0]["position"] = 1
    api.data["waiters"][1]["position"] = 2
    outcome = perform(api, operation)
    assert outcome.failed
    assert controller.accept(outcome)
    assert controller.fresh and not controller.busy
    assert controller.snapshot and controller.snapshot.queue_revision == "revision-2"
    assert not controller.can_move(-1)
    next_operation = controller.move(1)
    assert next_operation is not None and next_operation.expected_revision == "revision-2"
    assert api.calls == [("reorder", "request-b", 1, "revision-1"), ("state",)]


def test_invalid_json_uses_fixed_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    install_transport(monkeypatch, lambda _: httpx2.Response(200, text="synthetic-secret-not-json"))
    client = OperatorClient("http://127.0.0.1:9000", "synthetic-local-token")
    with pytest.raises(
        OperatorClientError, match="^The broker returned an invalid operator response.$"
    ):
        client.state()


@pytest.mark.parametrize(
    "token", ["", "synthetic token", "synthetic\nsecret", "synthetic\x00secret"]
)
def test_empty_or_header_unsafe_token_is_rejected(token: str) -> None:
    with pytest.raises(OperatorClientError) as error:
        OperatorClient("http://127.0.0.1:9000", token)
    assert str(error.value) == "CARDPUTER_LOCAL_TOKEN must contain the local broker token."


def test_empty_snapshot_clears_selections_and_disables_mutations() -> None:
    controller = ready()
    controller.selected_request = "request-a"
    controller.selected_lease = "lease-owner"
    operation = controller.refresh()
    assert operation is not None
    empty = state()
    empty.update(sessions=[], waiters=[], owner=None)
    assert controller.accept(
        Outcome(operation.sequence, QueueSnapshot.parse(empty), "Queue refreshed")
    )
    assert controller.snapshot and not controller.snapshot.sessions
    assert controller.selected_request == "" and controller.selected_lease == ""
    assert not controller.can_cancel() and not controller.can_release()
    assert not controller.can_move(-1) and not controller.can_move(1)


def test_empty_table_removes_old_rows_without_selectable_placeholder() -> None:
    class FakeTree:
        def __init__(self) -> None:
            self.rows = ["lease-owner"]
            self.calls: list[tuple[Any, ...]] = []

        def xview(self) -> tuple[float, float]:
            return (0.0, 1.0)

        def yview(self) -> tuple[float, float]:
            return (0.0, 1.0)

        def get_children(self) -> tuple[str, ...]:
            return tuple(self.rows)

        def delete(self, *items: str) -> None:
            self.calls.append(("delete", *items))
            self.rows = []

        def exists(self, item: str) -> bool:
            return item in self.rows

        def xview_moveto(self, value: float) -> None:
            self.calls.append(("xview", value))

        def yview_moveto(self, value: float) -> None:
            self.calls.append(("yview", value))

    tree: Any = FakeTree()
    _Window._replace(tree, [], "lease-owner")
    assert tree.rows == []
    assert tree.calls == [("delete", "lease-owner"), ("xview", 0.0), ("yview", 0.0)]
    # Repeated empty refreshes must not issue a Tcl delete with no item IDs.
    _Window._replace(tree, [], "")
    assert sum(call[0] == "delete" for call in tree.calls) == 1


def test_window_close_callback_only_stops_gui_worker_and_destroys_window() -> None:
    class FakeRoot:
        destroyed = False

        def destroy(self) -> None:
            self.destroyed = True

    window = object.__new__(_Window)
    api = FakeAPI()
    window.api = api
    window.root = root = FakeRoot()  # type: ignore[assignment]
    window.controller = ready()
    window.stopped = threading.Event()
    window.commands = queue.Queue()
    # The explicit button and window-manager close both invoke this callback.
    window._close()
    assert root.destroyed and window.stopped.is_set()
    assert window.controller.closed and not window.controller.fresh
    assert window.commands.get_nowait() is None
    assert api.calls == []
