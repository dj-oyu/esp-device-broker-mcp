"""Headless GUI and local API tests: synthetic identities, no hardware or secrets."""

import copy
from typing import Any

import httpx2
import pytest

from cardputer_mcp.gui import (
    Operation,
    Outcome,
    QueueController,
    QueueSnapshot,
    perform,
)
from cardputer_mcp.operator_client import OperatorClient, OperatorClientError


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


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:9000",
        "http://example.com:9000",
        "http://127.0.0.1:9000/mcp?token=synthetic",
        "http://user:secret@127.0.0.1:9000",
    ],
)
def test_client_rejects_nonlocal_or_credential_bearing_urls(url: str) -> None:
    with pytest.raises(OperatorClientError) as error:
        OperatorClient(url, "synthetic-local-token")
    assert str(error.value) == "Use a local http://127.0.0.1:PORT broker URL."


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
        (500, {"detail": "synthetic-secret"}),
        (302, {"detail": "synthetic-secret"}),
        (200, {"ok": False, "error": "synthetic-secret"}),
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


@pytest.mark.parametrize("token", ["", "synthetic\nsecret"])
def test_empty_or_header_unsafe_token_is_rejected(token: str) -> None:
    with pytest.raises(OperatorClientError) as error:
        OperatorClient("http://127.0.0.1:9000", token)
    assert str(error.value) == "CARDPUTER_LOCAL_TOKEN must contain the local broker token."


@pytest.mark.parametrize("uncertain", [False, True])
def test_current_execution_uncertainty_is_separate_from_historical_acknowledgement(uncertain):
    data = state()
    data.update(execution_uncertain=uncertain, ownership_uncertain=False, needs_review=True)
    snapshot = QueueSnapshot.parse(data)
    assert snapshot.execution_uncertain is uncertain
    assert snapshot.needs_review is True and snapshot.ownership_uncertain is False
    assert QueueSnapshot.parse(state()).execution_uncertain is None
