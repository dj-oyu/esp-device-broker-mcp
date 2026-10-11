"""Local GUI approvals use fake time/USB inventory and never touch hardware."""

import json
from contextlib import asynccontextmanager

import pytest

from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.capture_policy import HOME_ATTESTATION, CapturePolicies
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.models import (
    AcquireRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
    RenewRequest,
)
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.plan_approval import PlanApprovals
from cardputer_mcp.ports import DeviceSelector, PortInfo
from cardputer_mcp.serial_backend import SerialBackend


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@asynccontextmanager
async def running(tmp_path, *, backend=None, valid=None):
    clock = Clock()
    settings = {"backend": "serial", "serial": backend.config} if backend else {}
    broker = Broker(
        Settings(state_dir=tmp_path / "state", mock_stage_seconds=0.001, **settings),
        backend=backend,
        clock=clock,
        lock_path=tmp_path / "device.lockfile",
        serial_enabled=backend is not None,
        principal_valid=valid,
    )
    await broker.start()
    try:
        yield broker, PlanApprovals(broker), clock
    finally:
        broker.closing = False
        broker.active_job = None
        broker.backend.active = 0
        broker.backend.hardware_connected = False
        if hasattr(broker.backend, "ownership_uncertain"):
            broker.backend.ownership_uncertain = False
        await broker.close()


def prepare(broker, *, job="job-one", test=Action.DIAGNOSTICS, principal=None):
    if broker.session is None:
        broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id=principal)
    return broker.prepare(PrepareRequest(session=broker.session.capability, job_id=job, test=test))[
        "plan_hash"
    ]


def approvals(broker):
    return [row[0] for row in broker._store().db.execute("SELECT hash FROM approvals")]


async def test_exact_review_duplicate_and_existing_execution_consumption(tmp_path):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        detail = service.detail(digest)
        assert detail["plan"] == broker._store().plan(digest)
        assert len(detail["review_revision"]) == 64
        assert detail["attestation"] == "none"
        assert detail["lease_remaining_seconds"] == 30
        revision = detail["review_revision"]
        assert service.list_pending() == {
            "broker_epoch": broker.epoch,
            "plans": [
                {
                    "plan_hash": digest,
                    "job_id": "job-one",
                    "test": "diagnostics",
                    "backend": "mock",
                    "lease_remaining_seconds": 30,
                }
            ],
            "has_more": False,
        }
        assert service.approve(digest, revision, "") == {
            "approved": True,
            "plan_hash": digest,
            "already_approved": False,
            "execution_submitted": False,
        }
        clock.now += 1
        assert service.approve(digest, revision, "")["already_approved"] is True
        assert approvals(broker) == [digest]
        assert service.list_pending()["plans"] == []
        assert broker._store().record_count("jobs") == 0 and broker.backend.calls == 0
        request = ExecuteRequest(session=broker.session.capability, plan_hash=digest)
        broker.execute(request)
        assert approvals(broker) == []
        await broker.queue.join()
        assert broker.backend.calls == 1
        with pytest.raises(BrokerError, match="plan_job_already_exists"):
            service.approve(digest, revision, "")
        assert broker.execute(request)["deduplicated"] is True
        assert broker.backend.calls == 1


async def test_existing_cli_approval_is_preserved_and_idempotent(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        detail = service.detail(digest)
        broker._store().approve(digest)
        assert service.approve(digest, detail["review_revision"], "")["already_approved"]
        assert approvals(broker) == [digest]


async def test_restart_never_replays_an_old_approval(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        revision = service.detail(digest)["review_revision"]
        service.approve(digest, revision, "")
        broker.release(LeaseRequest(session=broker.session.capability))
    async with running(tmp_path) as (broker, service, _):
        assert service.list_pending()["plans"] == []
        prepare(broker, job="new-epoch")
        with pytest.raises(BrokerError, match="plan_session_mismatch"):
            service.approve(digest, revision, "")
        assert approvals(broker) == [digest]
        assert broker._store().record_count("jobs") == 0
        assert broker.backend.calls == 0


async def test_renewal_with_identical_deadline_still_invalidates_review(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        revision = service.detail(digest)["review_revision"]
        deadline = broker.session.deadline
        broker.renew(RenewRequest(session=broker.session.capability, lease_seconds=30))
        assert broker.session.deadline == deadline
        with pytest.raises(BrokerError, match="stale_plan_review"):
            service.approve(digest, revision, "")


@pytest.mark.parametrize("transition", ["expire", "release", "reacquire", "restart"])
async def test_old_lease_and_epoch_reviews_fail_closed(tmp_path, transition):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        revision = service.detail(digest)["review_revision"]
        if transition == "expire":
            clock.now += 30
        elif transition == "restart":
            broker.epoch = "b" * 32
        else:
            broker.release(LeaseRequest(session=broker.session.capability))
            if transition == "reacquire":
                broker.acquire(AcquireRequest(owner=Origin.LOCAL))
        with pytest.raises(BrokerError, match="plan_session_mismatch"):
            service.approve(digest, revision, "")
        assert service.list_pending()["plans"] == []
        assert approvals(broker) == []


@pytest.mark.parametrize("transition", ["renew", "new-plan", "finished-job", "queue-revision"])
async def test_review_revision_changes_with_history_and_lease(tmp_path, transition):
    async with running(tmp_path) as (broker, service, clock):
        digest = prepare(broker)
        old = service.detail(digest)["review_revision"]
        if transition == "renew":
            clock.now += 1
            broker.renew(RenewRequest(session=broker.session.capability, lease_seconds=50))
        elif transition == "new-plan":
            prepare(broker, job="another")
        elif transition == "finished-job":
            broker._store().add_job("finished", "f" * 64)
            broker._store().set_job("finished", "succeeded")
        else:
            broker._queue_sequence += 1
        with pytest.raises(BrokerError, match="stale_plan_review"):
            service.approve(digest, old, "")
        fresh = service.detail(digest)["review_revision"]
        assert old != fresh
        assert service.approve(digest, fresh, "")["approved"]


@pytest.mark.parametrize(
    "state",
    ["closing", "review", "active", "physical", "connected", "uncertain", "queue", "durable-queue"],
)
async def test_broker_state_blocks_approval(tmp_path, state):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        revision = service.detail(digest)["review_revision"]
        if state == "closing":
            broker.closing = True
        elif state in ("review", "durable-queue"):
            broker._store().add_job("other", "a" * 64)
            if state == "review":
                broker._store().set_job("other", "needs_review")
        elif state == "active":
            broker.active_job = "other"
        elif state == "physical":
            broker.backend.active = 1
        elif state == "connected":
            broker.backend.hardware_connected = True
        elif state == "uncertain":
            broker.backend.ownership_uncertain = True
        else:
            broker.queue.put_nowait(None)
        with pytest.raises(BrokerError):
            service.approve(digest, revision, "")
        assert approvals(broker) == []
        if state == "queue":
            assert broker.queue.get_nowait() is None
            broker.queue.task_done()


@pytest.mark.parametrize("valid", [None, lambda *args: False, lambda *args: "yes"])
async def test_principal_must_have_current_control_scope(tmp_path, valid):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, _):
        digest = prepare(broker, principal="requesting-client")
        broker.principal_valid = valid
        with pytest.raises(BrokerError, match="principal_not_authorized|plan_session_mismatch"):
            service.detail(digest)
        assert approvals(broker) == []


async def test_principal_revocation_and_authentication_exception(tmp_path):
    seen = []
    live = True

    def valid(principal, owner, scopes):
        seen.append((principal, owner, scopes))
        return live

    async with running(tmp_path, valid=valid) as (broker, service, _):
        digest = prepare(broker, principal="requesting-client")
        revision = service.detail(digest)["review_revision"]
        assert seen[-1] == ("requesting-client", "local_cli", frozenset({"control"}))
        live = False
        with pytest.raises(BrokerError, match="principal_not_authorized|plan_session_mismatch"):
            service.approve(digest, revision, "")
        live = True
        digest = prepare(broker, job="after-revocation", principal="requesting-client")
        revision = service.detail(digest)["review_revision"]

        def broken(*args):
            raise RuntimeError("SYNTHETIC_SECRET")

        broker.principal_valid = broken
        with pytest.raises(BrokerError, match="^(principal_not_authorized|plan_session_mismatch)$"):
            service.approve(digest, revision, "")


@pytest.mark.parametrize(
    "payload",
    [
        "deleted",
        "tampered",
        "null",
        "[]",
        "{",
        "duplicate",
        "oversized",
        "unknown-field",
        "wrong-schema",
    ],
)
async def test_malformed_or_replaced_plan_is_not_displayed_or_approved(tmp_path, payload):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        revision = service.detail(digest)["review_revision"]
        db = broker._store().db
        original = broker._store().plan(digest)
        if payload == "deleted":
            db.execute("DELETE FROM plans WHERE hash=?", (digest,))
        else:
            if payload == "tampered":
                original["job_id"] = "changed"
                raw = json.dumps(original)
            elif payload == "duplicate":
                raw = json.dumps(original)[:-1] + ',"job_id":"SYNTHETIC_SECRET"}'
            elif payload in ("oversized", "unknown-field", "wrong-schema"):
                if payload == "oversized":
                    original["extra"] = "SYNTHETIC_SECRET" * 2000
                elif payload == "unknown-field":
                    original["capability"] = "SYNTHETIC_SECRET"
                else:
                    original["version"] = True
                new_digest = digest_plan(original)
                db.execute("UPDATE plans SET hash=? WHERE hash=?", (new_digest, digest))
                digest = new_digest
                raw = json.dumps(original)
            else:
                raw = payload
            db.execute("UPDATE plans SET payload=? WHERE hash=?", (raw, digest))
        db.commit()
        with pytest.raises(BrokerError, match="^plan_not_found_or_changed$"):
            service.detail(digest)
        with pytest.raises(BrokerError, match="^plan_not_found_or_changed$"):
            service.approve(digest, revision, "")
        assert service.list_pending()["plans"] == []
        assert approvals(broker) == []


async def test_pending_is_current_bounded_and_contains_no_capability(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        old = prepare(broker, job="old")
        broker.release(LeaseRequest(session=broker.session.capability))
        for index in range(65):
            prepare(broker, job=f"job-{index}")
        pending = service.list_pending()
        assert len(pending["plans"]) == 64 and pending["has_more"]
        assert old not in json.dumps(pending)
        assert broker.session.capability not in json.dumps(pending)
        detail = service.detail(pending["plans"][0]["plan_hash"])
        assert broker.session.capability not in json.dumps(detail)
        assert broker.backend.calls == 0


def serial_backend():
    config = SerialSettings(
        selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number="SYNTHETIC-ONLY"),
        expected_firmware_sha256="a" * 64,
    )
    port = PortInfo(port="COM42", vid=0xCAFE, pid=1, serial_number="SYNTHETIC-ONLY")
    inventory = [port]
    calls = []

    def enumerate_only():
        calls.append("enumerate")
        return inventory

    def no_open():
        pytest.fail("approval must never construct or open a serial port")

    return SerialBackend(config, inventory=enumerate_only, factory=no_open), inventory, calls


@pytest.mark.parametrize(
    ("test", "kind", "phrase"),
    [
        (Action.OBSERVE, "none", ""),
        (Action.HELLO, "device_free_firmware", "DEVICE FREE AND FIRMWARE VERIFIED"),
        (Action.CAPTURE, "device_home_firmware", "DEVICE HOME AND FIRMWARE VERIFIED"),
    ],
)
async def test_serial_attestation_and_metadata_enumeration_only(tmp_path, test, kind, phrase):
    backend, inventory, calls = serial_backend()
    async with running(tmp_path, backend=backend, valid=lambda *args: True) as (broker, service, _):
        digest = prepare(broker, test=test, principal="synthetic-client")
        detail = service.detail(digest)
        assert detail["attestation"] == kind
        assert calls == ["enumerate"]
        wrong = "DEVICE HOME AND FIRMWARE VERIFIED" if test == Action.HELLO else "yes"
        for value in (wrong, phrase + " "):
            with pytest.raises(BrokerError, match="device_attestation_required"):
                service.approve(digest, detail["review_revision"], value)
        assert service.approve(digest, detail["review_revision"], phrase)["approved"]
        assert calls == ["enumerate", "enumerate"]
        assert backend.calls == 0 and not backend.hardware_connected
        assert broker._store().record_count("jobs") == 0


@pytest.mark.parametrize("change", ["port", "firmware", "inventory", "exception", "expired"])
async def test_serial_preflight_rejects_current_metadata_changes(tmp_path, change):
    backend, inventory, _ = serial_backend()
    async with running(tmp_path, backend=backend) as (broker, service, clock):
        digest = prepare(broker, test=Action.HELLO)
        detail = service.detail(digest)
        if change == "port":
            inventory[0] = inventory[0].model_copy(update={"port": "COM99"})
        elif change == "firmware":
            updated = backend.config.model_copy(update={"expected_firmware_sha256": "b" * 64})
            backend.config = updated
            broker.settings = broker.settings.model_copy(update={"serial": updated})
        elif change == "inventory":
            inventory.clear()
        elif change == "exception":

            def fail():
                raise RuntimeError("SYNTHETIC_SECRET")

            backend.inventory = fail
        else:

            def expires():
                clock.now += 30
                return inventory

            backend.inventory = expires
        with pytest.raises(BrokerError) as caught:
            service.approve(digest, detail["review_revision"], "DEVICE FREE AND FIRMWARE VERIFIED")
        assert "SYNTHETIC_SECRET" not in str(caught.value)
        assert approvals(broker) == []
        assert backend.calls == 0


async def test_wrong_review_hash_and_wrong_mock_attestation_fail(tmp_path):
    async with running(tmp_path) as (broker, service, _):
        digest = prepare(broker)
        detail = service.detail(digest)
        for revision in (None, "", "A" * 64, "a" * 64):
            with pytest.raises(BrokerError, match="stale_plan_review"):
                service.approve(digest, revision, "")
        with pytest.raises(BrokerError, match="device_attestation_required"):
            service.approve(digest, detail["review_revision"], "yes")
        assert approvals(broker) == []


@pytest.mark.parametrize("test", [Action.DIAGNOSTICS, Action.WORKFLOW, Action.CAPTURE])
async def test_all_mock_plans_and_validation_only_helper(tmp_path, test):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, _):
        digest = prepare(broker, test=test, principal="mock-client")
        plan, lease = service.validate_current(digest)
        assert plan == broker._store().plan(digest)
        assert lease is broker.session
        assert approvals(broker) == []
        detail = service.detail(digest)
        assert detail["attestation"] == "none"
        assert service.approve(digest, detail["review_revision"], "")["approved"]
        assert broker.backend.calls == 0


async def test_changed_backend_configuration_is_rejected_before_metadata_enumeration(tmp_path):
    backend, _, calls = serial_backend()
    async with running(tmp_path, backend=backend) as (broker, service, _):
        digest = prepare(broker, test=Action.OBSERVE)
        revision = service.detail(digest)["review_revision"]
        backend.config = backend.config.model_copy(update={"observe_seconds": 1})
        with pytest.raises(BrokerError, match="plan_backend_mismatch"):
            service.approve(digest, revision, "")
        assert calls == ["enumerate"]
        assert approvals(broker) == []


async def test_changed_serial_fingerprint_is_rejected_at_approval(tmp_path):
    backend, inventory, _ = serial_backend()
    async with running(tmp_path, backend=backend) as (broker, service, _):
        digest = prepare(broker, test=Action.OBSERVE)
        revision = service.detail(digest)["review_revision"]
        inventory[0] = inventory[0].model_copy(update={"location": "different-usb-location"})
        with pytest.raises(BrokerError, match="plan_backend_mismatch"):
            service.approve(digest, revision, "")
        assert approvals(broker) == []


def capture_policies(broker):
    wall = Clock()
    principal = broker.session.principal_id
    registry = [
        {
            "principal_id": principal,
            "origin": "local_cli",
            "scopes": ["read", "control"],
            "expires_at": 10000.0,
            "revoked_at": None,
            "kind": "issued",
        }
    ]
    policies = CapturePolicies(broker, lambda: registry, wall_clock=wall)
    broker.capture_policies = policies
    policy = policies.create(
        principal_id=principal,
        mode="persistent",
        capture_home=True,
        image_delivery=False,
        ttl_seconds=100,
        expected_target=policies.target(),
    )
    return policies, policy, wall


async def test_pending_excludes_mock_auto_execution_without_approving(tmp_path):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, _):
        for index, test in enumerate((Action.DIAGNOSTICS, Action.WORKFLOW, Action.CAPTURE)):
            prepare(broker, job=f"automatic-{index}", test=test, principal="mock-client")
        assert len(service.list_pending()["plans"]) == 3
        broker.settings = broker.settings.model_copy(update={"allow_mock_execution": True})
        assert service.list_pending() == {
            "broker_epoch": broker.epoch,
            "plans": [],
            "has_more": False,
        }
        assert approvals(broker) == []
        assert broker._store().record_count("jobs") == 0
        assert broker.backend.calls == 0
        broker.settings = broker.settings.model_copy(update={"allow_mock_execution": False})
        assert len(service.list_pending()["plans"]) == 3


@pytest.mark.parametrize("transition", ["revoke", "expire", "image-only"])
async def test_pending_capture_reappears_when_permission_is_unavailable(tmp_path, transition):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, clock):
        capture = prepare(broker, test=Action.CAPTURE, principal="key:cpk_" + "1" * 32)
        manual = prepare(broker, job="manual-diagnostic")
        policies, policy, wall = capture_policies(broker)
        audit_before = policies.list()["audit"]
        assert [item["plan_hash"] for item in service.list_pending()["plans"]] == [manual]
        assert policies.list()["audit"] == audit_before
        if transition == "revoke":
            policies.revoke(policy["policy_id"], expected_revision=1)
        elif transition == "expire":
            wall.now += 100
        else:
            policies.update(
                policy["policy_id"],
                expected_revision=1,
                capture_home=False,
                image_delivery=True,
                ttl_seconds=100,
                expected_target=policies.target(),
            )
        audit_before = policies.list()["audit"]
        assert [item["plan_hash"] for item in service.list_pending()["plans"]] == [capture, manual]
        assert policies.list()["audit"] == audit_before
        broker._store().approve(capture)
        assert [item["plan_hash"] for item in service.list_pending()["plans"]] == [manual]
        clock.now += 30
        assert service.list_pending()["plans"] == []
        assert broker.backend.calls == 0


async def test_serial_pending_capture_requires_current_arming_and_never_enumerates(tmp_path):
    backend, _, calls = serial_backend()
    async with running(tmp_path, backend=backend, valid=lambda *args: True) as (
        broker,
        service,
        clock,
    ):
        capture = prepare(broker, test=Action.CAPTURE, principal="key:cpk_" + "1" * 32)
        hello = prepare(broker, job="manual-hello", test=Action.HELLO)
        policies, _, _ = capture_policies(broker)
        assert [item["plan_hash"] for item in service.list_pending()["plans"]] == [capture, hello]
        policies.arm(
            lease_id=broker.session.id,
            expected_target=policies.target(),
            ttl_seconds=10,
            attestation=HOME_ATTESTATION,
            acknowledge_effects=True,
        )
        audit_before = policies.list()["audit"]
        assert [item["plan_hash"] for item in service.list_pending()["plans"]] == [hello]
        assert policies.list()["audit"] == audit_before
        clock.now += 10
        assert [item["plan_hash"] for item in service.list_pending()["plans"]] == [capture, hello]
        assert calls == ["enumerate", "enumerate"]
        assert approvals(broker) == []
        assert broker.backend.calls == 0 and not broker.backend.hardware_connected


async def test_pending_limit_applies_after_policy_and_invalid_plan_filtering(tmp_path):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, _):
        for index in range(65):
            prepare(
                broker,
                job=f"automatic-capture-{index}",
                test=Action.CAPTURE,
                principal="key:cpk_" + "1" * 32,
            )
        policies, _, _ = capture_policies(broker)
        assert service.list_pending()["plans"] == []
        assert not service.list_pending()["has_more"]
        invalid = prepare(broker, job="invalid")
        plan = broker._store().plan(invalid)
        plan["job_id"] = "tampered"
        broker._store().db.execute(
            "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(plan), invalid)
        )
        broker._store().db.commit()
        manual = prepare(broker, job="manual-after-automatic")
        audit_before = policies.list()["audit"]
        pending = service.list_pending()
        assert [item["plan_hash"] for item in pending["plans"]] == [manual]
        assert not pending["has_more"]
        for index in range(64):
            prepare(broker, job=f"manual-{index}")
        pending = service.list_pending()
        assert len(pending["plans"]) == 64 and pending["has_more"]
        assert policies.list()["audit"] == audit_before
        assert broker.backend.calls == 0


async def test_pending_scan_limit_reports_unknown_without_unbounded_policy_probes(tmp_path):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, _):
        for index in range(257):
            prepare(
                broker,
                job=f"automatic-capture-{index}",
                test=Action.CAPTURE,
                principal="key:cpk_" + "1" * 32,
            )
        policies, _, _ = capture_policies(broker)
        authorize = policies.authorize_capture
        probes = 0

        def counted(plan, *, record_use):
            nonlocal probes
            probes += 1
            assert record_use is False
            return authorize(plan, record_use=record_use)

        policies.authorize_capture = counted
        audit_before = policies.list()["audit"]
        with pytest.raises(BrokerError, match="^plan_approval_unavailable$"):
            service.list_pending()
        assert probes == 256
        assert policies.list()["audit"] == audit_before
        assert approvals(broker) == []
        assert broker.backend.calls == 0


@pytest.mark.parametrize("transition", ["expire", "replace", "epoch", "job", "approved"])
async def test_pending_rechecks_current_context_after_policy_registry_callback(
    tmp_path, transition
):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, clock):
        manual = prepare(broker, principal="key:cpk_" + "1" * 32)
        prepare(broker, job="capture", test=Action.CAPTURE)
        policies, _, _ = capture_policies(broker)
        registry = policies.list_principals
        changed = False

        def changed_registry():
            nonlocal changed
            if changed:
                return registry()
            changed = True
            if transition == "expire":
                clock.now += 30
            elif transition == "replace":
                broker.session = None
            elif transition == "epoch":
                broker.epoch = "c" * 32
            elif transition == "job":
                broker._store().add_job("job-one", manual)
                broker._store().set_job("job-one", "succeeded")
            else:
                broker._store().approve(manual)
            return registry()

        policies.list_principals = changed_registry
        if transition in ("expire", "replace", "epoch"):
            with pytest.raises(BrokerError, match="^plan_session_mismatch$"):
                service.list_pending()
        else:
            with pytest.raises(BrokerError, match="^plan_approval_unavailable$"):
                service.list_pending()
        assert broker.backend.calls == 0


async def test_pending_policy_callback_error_is_fixed_and_does_not_leak(tmp_path):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, _):
        prepare(broker, test=Action.CAPTURE, principal="key:cpk_" + "1" * 32)
        policies, _, _ = capture_policies(broker)

        def broken(plan, *, record_use):
            assert record_use is False
            raise RuntimeError("SYNTHETIC_SECRET")

        policies.authorize_capture = broken
        with pytest.raises(BrokerError, match="^plan_approval_unavailable$"):
            service.list_pending()
        assert approvals(broker) == []
        assert broker.backend.calls == 0


@pytest.mark.parametrize("transition", ["expire", "closing", "active", "queued", "review"])
async def test_pending_checks_state_after_final_principal_callback(tmp_path, transition):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, clock):
        prepare(broker, principal="mock-client")
        checks = 0

        def valid(principal, owner, scopes):
            nonlocal checks
            if scopes == frozenset({"control"}):
                checks += 1
                if checks == 2:
                    if transition == "expire":
                        clock.now += 30
                    elif transition == "closing":
                        broker.closing = True
                    elif transition == "active":
                        broker.active_job = "other"
                    else:
                        broker._store().add_job("other", "a" * 64)
                        if transition == "review":
                            broker._store().set_job("other", "needs_review")
            return True

        broker.principal_valid = valid
        with pytest.raises(BrokerError):
            service.list_pending()
        assert broker.backend.calls == 0


@pytest.mark.parametrize("transition", ["policy-expiry", "policy-revoke", "arm-expiry"])
async def test_pending_rejects_capture_permission_changes_in_final_context(tmp_path, transition):
    backend = serial_backend()[0] if transition == "arm-expiry" else None
    async with running(tmp_path, backend=backend, valid=lambda *args: True) as (
        broker,
        service,
        clock,
    ):
        digest = prepare(broker, test=Action.CAPTURE, principal="key:cpk_" + "1" * 32)
        policies, policy, wall = capture_policies(broker)
        if backend:
            policies.arm(
                lease_id=broker.session.id,
                expected_target=policies.target(),
                ttl_seconds=10,
                attestation=HOME_ATTESTATION,
                acknowledge_effects=True,
            )
        context = service._context
        checks = 0

        def changed_context():
            nonlocal checks
            lease = context()
            checks += 1
            if checks == 2:
                if transition == "policy-expiry":
                    wall.now += 100
                elif transition == "policy-revoke":
                    policies.revoke(policy["policy_id"], expected_revision=1)
                else:
                    clock.now += 10
            return lease

        service._context = changed_context
        with pytest.raises(BrokerError, match="^plan_approval_unavailable$"):
            service.list_pending()
        service._context = context
        assert [item["plan_hash"] for item in service.list_pending()["plans"]] == [digest]
        assert broker.backend.calls == 0


async def test_pending_terminal_policy_validation_catches_late_callback_expiry(tmp_path):
    async with running(tmp_path, valid=lambda *args: True) as (broker, service, _):
        for index in range(2):
            prepare(
                broker,
                job=f"capture-{index}",
                test=Action.CAPTURE,
                principal="key:cpk_" + "1" * 32,
            )
        policies, _, wall = capture_policies(broker)
        context = service._context
        registry = policies.list_principals
        context_checks = 0
        terminal_checks = 0

        def current_context():
            nonlocal context_checks
            context_checks += 1
            return context()

        def expiring_registry():
            nonlocal terminal_checks
            if context_checks == 2:
                terminal_checks += 1
                if terminal_checks == 2:
                    wall.now += 100
            return registry()

        service._context = current_context
        policies.list_principals = expiring_registry
        audit_before = policies.list()["audit"]
        with pytest.raises(BrokerError, match="^plan_approval_unavailable$"):
            service.list_pending()
        assert terminal_checks == 2
        service._context = context
        policies.list_principals = registry
        assert len(service.list_pending()["plans"]) == 2
        assert policies.list()["audit"] == audit_before
        assert broker.backend.calls == 0
