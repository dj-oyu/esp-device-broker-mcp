"""Headless permission UI checks; all identities and requests are synthetic."""

import copy
import json
from dataclasses import replace
from typing import Any

import pytest

from cardputer_mcp.gui import _Window
from cardputer_mcp.gui_policies import (
    ARM_NOTICE,
    CAPTURE_EFFECTS,
    DELIVERY_EFFECTS,
    HOME_ATTESTATION,
    PENDING_CLOSE,
    WRITE_UNCERTAIN,
    PermissionsPanel,
    PolicyController,
    PolicyOperation,
    PolicyOptions,
    PolicyOutcome,
    PolicySnapshot,
    PolicyWorker,
    perform_policy,
)

PRINCIPAL = "key:cpk_" + "a" * 32
OTHER = "key:cpk_" + "b" * 32
RULE = "capture_policy_" + "c" * 32
TARGET = "d" * 64
EPOCH = "e" * 32


def options(**changes: Any) -> dict[str, Any]:
    result = {
        "ok": True,
        "target": TARGET,
        "broker_epoch": EPOCH,
        "server_now": 1000.0,
        "backend": "mock",
        "device": "mock-cardputer",
        "expected_firmware_sha256": None,
        "selector": None,
        "principals": [
            {
                "principal_id": PRINCIPAL,
                "label": "Test client",
                "origin": "remote_mcp",
                "scopes": ["read", "control"],
                "kind": "issued",
                "expires_at": 2000.0,
            }
        ],
        "active_lease": {
            "lease_id": "lease-1",
            "principal_id": PRINCIPAL,
            "origin": "remote_mcp",
            "remaining_seconds": 200,
        },
        "max_ttl_seconds": 604800,
        "max_arm_seconds": 300,
        "effects": ["Capture effects"],
        "arming": None,
        "attestation": HOME_ATTESTATION,
    }
    result.update(changes)
    return result


def rule(**changes: Any) -> dict[str, Any]:
    result = {
        "policy_id": RULE,
        "revision": 1,
        "principal_id": PRINCIPAL,
        "origin": "remote_mcp",
        "target": TARGET,
        "mode": "persistent",
        "capture_home": True,
        "image_delivery": False,
        "created_at": 900.0,
        "expires_at": 1300.0,
        "revoked": False,
        "active": True,
        "broker_epoch": None,
        "lease_id": None,
    }
    result.update(changes)
    return result


def listing(*rows: dict[str, Any], **changes: Any) -> dict[str, Any]:
    result = {
        "ok": True,
        "target": TARGET,
        "broker_epoch": EPOCH,
        "server_now": 1000.0,
        "policies": list(rows),
        "arming": None,
        "audit": [{"event": "policy_created", "at": 900.0, "policy_id": RULE}],
    }
    result.update(changes)
    return result


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


class FakeAPI:
    def __init__(self) -> None:
        self.options = options()
        self.listing = listing(rule())
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.override: dict[str, Any] | None = None

    def policy_options(self) -> dict[str, Any]:
        self.calls.append(("options",))
        return copy.deepcopy(self.options)

    def policies(self) -> dict[str, Any]:
        self.calls.append(("list",))
        return copy.deepcopy(self.listing)

    def _write(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, copy.deepcopy(payload)))
        if self.error:
            raise self.error
        if self.override is not None:
            return self.override
        if action == "arm":
            return {
                "ok": True,
                "arming": {
                    "lease_id": payload["lease_id"],
                    "target": payload["expected_target"],
                    "principal_id": PRINCIPAL,
                    "remaining_seconds": payload["ttl_seconds"],
                },
            }
        changes = {
            "revision": 1 if action == "create" else payload["expected_revision"] + 1,
            "revoked": action == "revoke",
        }
        for key in ("principal_id", "mode", "capture_home", "image_delivery"):
            if key in payload:
                changes[key] = payload[key]
        if payload.get("mode") == "session":
            changes.update(lease_id=payload["expected_lease_id"], broker_epoch=EPOCH)
        return {"ok": True, "policy": rule(**changes)}

    def create_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._write("create", data)

    def update_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._write("update", data)

    def revoke_policy(self, policy_id: str, expected_revision: int) -> dict[str, Any]:
        return self._write(
            "revoke", {"policy_id": policy_id, "expected_revision": expected_revision}
        )

    def arm_capture(self, data: dict[str, Any]) -> dict[str, Any]:
        return self._write("arm", data)


def loaded(api: FakeAPI | None = None) -> tuple[PolicyController, FakeAPI, Clock]:
    api = api or FakeAPI()
    clock = Clock()
    model = PolicyController(clock)
    operation = model.refresh()
    assert operation is not None
    assert model.accept(perform_policy(api, operation))
    assert model.ready()
    return model, api, clock


def create_review(model: PolicyController, **changes: Any) -> Any:
    args = {"mode": "persistent", "capture_home": True, "ttl_text": "100"}
    args.update(changes)
    return model.review("create", **args)


def serial_api() -> FakeAPI:
    api = FakeAPI()
    api.options.update(
        backend="serial",
        expected_firmware_sha256="f" * 64,
        selector={"vid": 1, "pid": 2, "serial_number": "test-board", "location": None},
    )
    return api


def test_open_refresh_select_and_review_never_create_a_permission() -> None:
    model, api, _ = loaded()
    assert model.principal_id == "" and model.selected == ""
    assert create_review(model) is None
    model.select_principal(PRINCIPAL)
    assert create_review(model) is not None
    assert model.review("create", mode="persistent", ttl_text="100") is None
    assert api.calls == [("options",), ("list",)]


@pytest.mark.parametrize(
    "field,value",
    [
        ("target", "bad"),
        ("broker_epoch", "bad"),
        ("server_now", float("nan")),
        ("max_ttl_seconds", True),
        ("max_ttl_seconds", 604801),
        ("max_arm_seconds", 0),
        ("backend", "unknown"),
        ("attestation", "trust me"),
        ("effects", []),
        ("principals", "ALL"),
        ("principals", [{}] * 1001),
        ("device", "x" * 257),
    ],
)
def test_malformed_options_fail_closed(field: str, value: Any) -> None:
    api = FakeAPI()
    api.options[field] = value
    model = PolicyController(Clock())
    operation = model.refresh()
    assert operation is not None
    outcome = perform_policy(api, operation)
    assert outcome.failed and model.accept(outcome)
    assert not model.ready() and model.snapshot is None


@pytest.mark.parametrize(
    "change",
    [
        {"scopes": ["read", "admin"]},
        {"scopes": ["control"]},
        {"scopes": ["read", "read"]},
        {"scopes": "read"},
        {"kind": "issued", "principal_id": "ephemeral:remote_mcp"},
        {"kind": "ephemeral", "expires_at": 1200},
        {"expires_at": float("inf")},
    ],
)
def test_invalid_principal_metadata_is_not_authority(change: dict[str, Any]) -> None:
    data = options()
    data["principals"][0].update(change)
    with pytest.raises((ValueError, TypeError)):
        PolicyOptions.parse(data, 10)


def test_options_render_only_bounded_known_safe_fields() -> None:
    data = options(device="board\nFAKE\u202etarget")
    data["token"] = "secret"
    parsed = PolicyOptions.parse(data, 10)
    assert "\\n" in parsed.device and "\\u202e" in parsed.device
    assert "secret" not in repr(parsed)


@pytest.mark.parametrize(
    "change",
    [
        {"target": "a" * 64},
        {"broker_epoch": "a" * 32},
        {"policies": [rule(), rule()]},
        {"policies": [rule(revision=True)]},
        {"policies": [rule(mode="forever")]},
        {"policies": [rule(active="true")]},
        {"policies": [rule(created_at=1400)]},
        {"audit": [{"event": "server supplied text", "at": 1000, "policy_id": None}]},
    ],
)
def test_malformed_or_inconsistent_listing_blocks_changes(change: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError, KeyError)):
        PolicySnapshot.parse(options(), listing(**change), 10)


def test_scope_ceiling_allows_only_delivery_for_read_only_key() -> None:
    api = FakeAPI()
    api.options["principals"][0]["scopes"] = ["read"]
    model, _, _ = loaded(api)
    model.select_principal(PRINCIPAL)
    assert create_review(model) is None
    assert create_review(model, capture_home=False, image_delivery=True) is not None


def test_ephemeral_is_current_lease_only_and_never_persistent() -> None:
    api = FakeAPI()
    principal = "ephemeral:remote_mcp"
    api.options["principals"][0].update(principal_id=principal, kind="ephemeral", expires_at=None)
    api.options["active_lease"]["principal_id"] = principal
    model, _, _ = loaded(api)
    model.select_principal(principal)
    assert create_review(model) is None
    review = create_review(model, mode="session")
    assert review is not None
    assert json.loads(review.payload)["expected_lease_id"] == "lease-1"
    assert "group of clients" in review.explanation


@pytest.mark.parametrize("ttl", ["", "0", "-1", "604801", "1.5", "1e3", " 100", "99999999999"])
def test_finite_explicit_duration_required(ttl: str) -> None:
    model, _, _ = loaded()
    model.select_principal(PRINCIPAL)
    assert create_review(model, ttl_text=ttl) is None


def test_key_scope_device_firmware_effects_and_expiry_are_reviewed() -> None:
    model, _, _ = loaded(serial_api())
    model.select_principal(PRINCIPAL)
    review = create_review(model, image_delivery=True)
    assert review is not None
    for text in (
        PRINCIPAL,
        TARGET,
        "f" * 64,
        "read",
        "control",
        "100 seconds",
        "test-board",
        CAPTURE_EFFECTS,
        DELIVERY_EFFECTS,
        ARM_NOTICE,
    ):
        assert text in review.explanation
    assert "acknowledge_effects" not in json.loads(review.payload)
    operation = model.commit(review)
    assert operation is not None
    assert json.loads(operation.payload)["acknowledge_effects"] is True


@pytest.mark.parametrize(
    "race", ["selection", "principal", "form", "refresh", "expiry", "key_expiry", "lease_expiry"]
)
def test_modal_review_races_never_write(race: str) -> None:
    api = FakeAPI()
    if race == "key_expiry":
        api.options["principals"][0]["expires_at"] = 1001
    if race == "lease_expiry":
        api.options["active_lease"]["remaining_seconds"] = 1
    model, api, clock = loaded(api)
    model.select_principal(PRINCIPAL)
    review = create_review(model, mode="session")
    assert review is not None
    if race == "selection":
        model.select(RULE)
    elif race == "principal":
        model.select_principal(OTHER)
    elif race == "form":
        model.changed()
    elif race == "refresh":
        operation = model.refresh()
        assert operation and model.accept(perform_policy(api, operation))
    else:
        clock.now += 31 if race == "expiry" else 2
    assert model.commit(review) is None
    assert all(call[0] in ("options", "list") for call in api.calls)


def test_double_click_and_duplicate_result_do_not_replay_write() -> None:
    model, api, _ = loaded()
    model.select_principal(PRINCIPAL)
    review = create_review(model)
    assert review is not None
    operation = model.commit(review)
    assert operation is not None and model.commit(review) is None
    outcome = perform_policy(api, operation)
    assert not outcome.failed and model.accept(outcome)
    assert not model.accept(outcome)
    assert not model.ready() and model.commit(review) is None
    assert [call[0] for call in api.calls].count("create") == 1


@pytest.mark.parametrize(
    "change", [{"expires_at": 999}, {"revoked": True}, {"active": False}, {"target": "a" * 64}]
)
def test_inactive_expired_revoked_or_wrong_target_rule_cannot_be_edited(
    change: dict[str, Any],
) -> None:
    api = FakeAPI()
    api.listing = listing(rule(**change))
    model, _, _ = loaded(api)
    model.select(RULE)
    model.select_principal(PRINCIPAL)
    assert model.review("update", mode="persistent", capture_home=True, ttl_text="100") is None


def test_edit_and_revoke_bind_exact_selected_revision() -> None:
    model, api, _ = loaded()
    model.select(RULE)
    model.select_principal(PRINCIPAL)
    review = model.review("update", mode="persistent", image_delivery=True, ttl_text="50")
    assert review is not None
    payload = json.loads(review.payload)
    assert payload["policy_id"] == RULE and payload["expected_revision"] == 1
    assert "principal_id" not in payload and "mode" not in payload
    operation = model.commit(review)
    assert operation is not None and model.accept(perform_policy(api, operation))
    refresh = model.refresh()
    assert refresh is not None and model.accept(perform_policy(api, refresh))
    review = model.review("revoke")
    assert review is not None
    assert json.loads(review.payload) == {"policy_id": RULE, "expected_revision": 1}


def test_stale_revision_or_any_write_failure_requires_refresh_without_replay() -> None:
    model, api, _ = loaded()
    model.select(RULE)
    review = model.review("revoke")
    assert review is not None
    operation = model.commit(review)
    assert operation is not None
    api.error = ValueError("stale_capture_policy_revision TOKEN=secret https://untrusted/")
    assert model.accept(perform_policy(api, operation))
    assert model.message == WRITE_UNCERTAIN and not model.ready()
    assert "secret" not in model.message and model.commit(review) is None
    assert [call[0] for call in api.calls].count("revoke") == 1


@pytest.mark.parametrize(
    "attestation,ack", [("", False), (HOME_ATTESTATION, False), ("DEVICE VERIFIED", True)]
)
def test_arm_requires_exact_phrase_and_separate_effects_ack(attestation: str, ack: bool) -> None:
    model, _, _ = loaded(serial_api())
    model.select_principal(PRINCIPAL)
    assert (
        model.review("arm", ttl_text="100", attestation=attestation, acknowledge_effects=ack)
        is None
    )


def test_arm_requires_current_selected_principal_target_and_firmware() -> None:
    model, api, _ = loaded(serial_api())
    model.select_principal(PRINCIPAL)
    review = model.review(
        "arm", ttl_text="100", attestation=HOME_ATTESTATION, acknowledge_effects=True
    )
    assert review is not None
    operation = model.commit(review)
    assert operation is not None and model.accept(perform_policy(api, operation))
    payload = api.calls[-1][1]
    assert payload["lease_id"] == "lease-1" and payload["expected_target"] == TARGET
    assert payload["acknowledge_effects"] is True
    for option_changes in (
        {"backend": "mock"},
        {"expected_firmware_sha256": None},
        {"active_lease": None},
    ):
        api = serial_api()
        api.options.update(option_changes)
        model, _, _ = loaded(api)
        model.select_principal(PRINCIPAL)
        assert (
            model.review(
                "arm", ttl_text="100", attestation=HOME_ATTESTATION, acknowledge_effects=True
            )
            is None
        )


@pytest.mark.parametrize(
    "response",
    [
        {"ok": True},
        {"ok": True, "policy": rule(revision=99)},
        {"ok": True, "policy": rule(principal_id=OTHER)},
        {"ok": True, "policy": rule(target="a" * 64)},
    ],
)
def test_malformed_write_success_is_uncertain_not_trusted(response: dict[str, Any]) -> None:
    model, api, _ = loaded()
    model.select_principal(PRINCIPAL)
    review = create_review(model)
    assert review is not None
    operation = model.commit(review)
    assert operation is not None
    api.override = response
    assert model.accept(perform_policy(api, operation)) and model.message == WRITE_UNCERTAIN


def test_pending_close_and_stale_results_preserve_inflight_request() -> None:
    model, api, _ = loaded()
    operation = model.refresh()
    assert operation is not None and not model.close()
    assert model.message == PENDING_CLOSE and not model.closed
    assert not model.accept(PolicyOutcome(replace(operation, sequence=999)))
    assert model.busy
    assert model.accept(perform_policy(api, operation)) and model.close()
    assert model.closed and model.refresh() is None


def test_worker_has_one_queue_and_never_calls_tk() -> None:
    api = FakeAPI()
    worker = PolicyWorker(api)
    try:
        operation = PolicyOperation(1, "refresh", 10)
        worker.commands.put(operation)
        outcome = worker.results.get(timeout=2)
        assert outcome.snapshot is not None and not outcome.failed
        assert api.calls == [("options",), ("list",)]
        assert worker.results.empty()
    finally:
        worker.stop()
        worker.thread.join(timeout=2)
    assert not worker.thread.is_alive()


def test_permissions_panel_close_guard_blocks_main_window_shutdown() -> None:
    calls: list[str] = []
    model = PolicyController(Clock())
    assert model.refresh()
    panel = object.__new__(PermissionsPanel)
    panel.controller = model
    panel.show = lambda: calls.append("show")
    panel._render = lambda: calls.append("render")
    assert not panel.can_close_window() and model.message == PENDING_CLOSE
    window = object.__new__(_Window)
    window.permissions_panel = panel
    window._finish_close = lambda: calls.append("finish")
    window._close()
    assert "finish" not in calls


def test_panel_cancelled_confirmation_and_modal_race_enqueue_nothing(monkeypatch: Any) -> None:
    model, _, _ = loaded()
    model.select_principal(PRINCIPAL)
    review = create_review(model)
    assert review is not None
    panel = object.__new__(PermissionsPanel)
    panel.controller = model
    panel.dialog, panel.root = None, object()
    panel._review = lambda _action: review
    submitted: list[Any] = []
    panel._submit = submitted.append
    monkeypatch.setattr("tkinter.messagebox.askyesno", lambda *a, **kw: False)
    panel._act("create")
    assert not submitted and not model.busy

    def changed(*_args: Any, **_kwargs: Any) -> bool:
        model.changed()
        return True

    monkeypatch.setattr("tkinter.messagebox.askyesno", changed)
    panel._act("create")
    assert submitted == [None] and not model.busy


def test_stop_during_pending_request_does_not_stop_worker() -> None:
    model = PolicyController(Clock())
    model.refresh()
    panel = object.__new__(PermissionsPanel)
    panel.controller = model
    panel._render = lambda: None
    panel.worker = type("Worker", (), {"stop": lambda _self: pytest.fail("worker stopped")})()
    assert not panel.stop() and not model.closed


def test_real_engine_option_and_list_schemas_are_accepted(tmp_path: Any) -> None:
    """Read only synthetic engine metadata; issue no key, grant, or device command."""
    from types import SimpleNamespace
    from typing import cast

    from cardputer_mcp.broker import Broker, Session
    from cardputer_mcp.capture_policy import CapturePolicies
    from cardputer_mcp.config import Settings
    from cardputer_mcp.store import Store

    store = Store(tmp_path / "synthetic.sqlite3")
    try:
        broker = cast(
            Broker,
            SimpleNamespace(
                settings=Settings(state_dir=tmp_path),
                backend=SimpleNamespace(device="mock-cardputer"),
                session=Session("synthetic-capability", "lease-1", "remote_mcp", 200.0, PRINCIPAL),
                epoch=EPOCH,
                clock=lambda: 10.0,
                closing=False,
                principal_valid=lambda *args: True,
                _store=lambda: store,
            ),
        )
        engine = CapturePolicies(
            broker,
            lambda: [
                {
                    "principal_id": PRINCIPAL,
                    "origin": "remote_mcp",
                    "scopes": ["read", "control"],
                    "expires_at": 2000.0,
                    "issued_at": 900.0,
                    "revoked_at": None,
                    "label": "Synthetic fixture",
                    "kind": "issued",
                }
            ],
            wall_clock=lambda: 1000.0,
        )
        snapshot = PolicySnapshot.parse(
            {"ok": True, **engine.options()},
            {"ok": True, **engine.list()},
            10.0,
        )
        assert snapshot.options.principals[0].principal_id == PRINCIPAL
        assert snapshot.policies == ()
        assert snapshot.options.lease is not None
        assert snapshot.options.lease.lease_id == "lease-1"
    finally:
        store.close()
