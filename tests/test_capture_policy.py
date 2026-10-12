"""Policy decisions use synthetic clocks/metadata only: no keys or USB access."""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cardputer_mcp.broker import Broker, Session, digest_plan
from cardputer_mcp.capture_policy import (
    HOME_ATTESTATION,
    MAX_ARM_SECONDS,
    MAX_AUDIT_EVENTS,
    MAX_TTL_SECONDS,
    CapturePolicies,
    CapturePolicyError,
)
from cardputer_mcp.config import SerialSettings, Settings
from cardputer_mcp.ports import DeviceSelector
from cardputer_mcp.store import Store

PRINCIPAL = "key:cpk_" + "1" * 32
OTHER = "key:cpk_" + "2" * 32


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Environment:
    def __init__(self, path: Path) -> None:
        self.monotonic = Clock(100.0)
        self.wall = Clock(1000.0)
        self.registry: list[dict[str, Any]] = [
            {
                "principal_id": PRINCIPAL,
                "origin": "remote_mcp",
                "scopes": ["read", "control"],
                "expires_at": 1000.0 + MAX_TTL_SECONDS * 2,
                "revoked_at": None,
                "label": "Synthetic test subject",
                "kind": "issued",
            }
        ]
        self.store = Store(path / "history.sqlite3")
        self.valid = True
        self.broker = cast(
            Broker,
            SimpleNamespace(
                settings=Settings(state_dir=path),
                backend=SimpleNamespace(name="mock", device="mock-cardputer"),
                session=Session("not-a-real-capability", "lease-1", "remote_mcp", 400.0, PRINCIPAL),
                epoch="epoch-1",
                closing=False,
                clock=self.monotonic,
                principal_valid=lambda _principal, _origin, _scopes: self.valid,
                _store=lambda: self.store,
            ),
        )
        self.policies = CapturePolicies(self.broker, lambda: self.registry, wall_clock=self.wall)

    def serial(self, firmware: str | None = "a" * 64) -> None:
        self.broker.settings = Settings(
            state_dir=self.broker.settings.state_dir,
            backend="serial",
            serial=SerialSettings(
                selector=DeviceSelector(vid=1, pid=2, serial_number="synthetic-board"),
                expected_firmware_sha256=firmware,
            ),
        )
        self.broker.backend = cast(
            Any,
            SimpleNamespace(name="serial", device="configured-cardputer"),
        )

    def grant(self, **changes: Any) -> dict[str, Any]:
        args = {
            "principal_id": PRINCIPAL,
            "mode": "persistent",
            "capture_home": True,
            "image_delivery": True,
            "ttl_seconds": 200,
            "expected_target": self.policies.target(),
        }
        args.update(changes)
        if args["mode"] == "session" and "expected_lease_id" not in args:
            args["expected_lease_id"] = self.broker.session.id if self.broker.session else None
        return self.policies.create(**args)

    def plan(self, **changes: Any) -> dict[str, Any]:
        serial = self.broker.settings.serial
        plan = {
            "test": "capture_home",
            "job_id": "capture-1",
            "broker_epoch": self.broker.epoch,
            "session_id": self.broker.session.id if self.broker.session else "lease-1",
            "backend": self.broker.settings.backend,
            "device": self.broker.backend.device,
            "expected_firmware_sha256": serial.expected_firmware_sha256 if serial else None,
        }
        plan.update(changes)
        return plan

    def arm(self, **changes: Any) -> dict[str, Any]:
        args = {
            "lease_id": self.broker.session.id if self.broker.session else None,
            "expected_target": self.policies.target(),
            "ttl_seconds": 100,
            "attestation": HOME_ATTESTATION,
            "acknowledge_effects": True,
        }
        args.update(changes)
        return self.policies.arm(**args)

    def image(self) -> dict[str, Any]:
        plan = self.plan()
        digest = digest_plan(plan)
        self.store.put_plan(digest, plan)
        self.store.add_job("capture-1", digest)
        self.store.set_job("capture-1", "succeeded")
        return {
            "principal_id": PRINCIPAL,
            "origin": "remote_mcp",
            "deadline": 400.0,
            "policy_target": self.policies.target(),
            "delivery_state": "pending",
        }


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Environment]:
    environment = Environment(tmp_path)
    try:
        yield environment
    finally:
        environment.store.close()


def test_empty_default_and_explicit_capture_only_operations(env: Environment) -> None:
    assert env.policies.list()["policies"] == []
    assert env.policies.authorize_capture(env.plan()) is None
    image = env.image()
    assert not env.policies.authorize_image("capture-1", image)
    env.grant()
    assert env.policies.authorize_capture(env.plan()) is not None
    for operation in ("hello", "firmware_workflow", "passive_observe", "flash", "status", "logs"):
        assert env.policies.authorize_capture(env.plan(test=operation)) is None


def test_prepare_probe_does_not_record_capture_use(env: Environment) -> None:
    assert env.policies.authorize_capture(env.plan(), record_use=False) is None
    env.grant()
    before = env.policies.list()["audit"]
    record = env.policies.authorize_capture(env.plan(), record_use=False)
    assert record and env.policies.validate_capture(record, env.plan())
    assert env.policies.list()["audit"] == before


def test_capture_and_image_permissions_are_independent(env: Environment) -> None:
    image = env.image()
    policy = env.grant(capture_home=False)
    assert env.policies.authorize_capture(env.plan()) is None
    assert env.policies.authorize_image("capture-1", image)
    env.policies.update(
        policy["policy_id"],
        expected_revision=1,
        capture_home=True,
        image_delivery=False,
        ttl_seconds=200,
        expected_target=env.policies.target(),
    )
    assert env.policies.authorize_capture(env.plan()) is not None
    assert not env.policies.authorize_image("capture-1", image)


def test_scopes_cannot_be_expanded_and_runtime_narrowing_is_rechecked(env: Environment) -> None:
    policy = env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record
    env.registry[0]["scopes"] = ["read"]
    assert not env.policies.validate_capture(record, env.plan())
    assert env.policies.authorize_capture(env.plan()) is None
    with pytest.raises(CapturePolicyError, match="capture_policy_principal_not_authorized"):
        env.grant()
    with pytest.raises(CapturePolicyError, match="capture_policy_principal_not_authorized"):
        env.policies.update(
            policy["policy_id"],
            expected_revision=1,
            capture_home=True,
            image_delivery=False,
            ttl_seconds=20,
            expected_target=env.policies.target(),
        )
    env.grant(capture_home=False)
    image = env.image()
    assert env.policies.authorize_image("capture-1", image)
    env.registry[0]["scopes"] = []
    assert not env.policies.authorize_image("capture-1", image)


@pytest.mark.parametrize("change", ["removed", "revoked", "expired", "wrong_origin", "not_issued"])
def test_registry_metadata_is_authority_even_when_callback_returns_true(
    env: Environment,
    change: str,
) -> None:
    env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record
    if change == "removed":
        env.registry.clear()
    elif change == "revoked":
        env.registry[0]["revoked_at"] = env.wall.now
    elif change == "expired":
        env.registry[0]["expires_at"] = env.wall.now
    elif change == "wrong_origin":
        env.registry[0]["origin"] = "local_cli"
    else:
        env.registry[0]["kind"] = "ephemeral"
    assert not env.policies.validate_capture(record, env.plan())
    assert env.policies.authorize_capture(env.plan()) is None


def test_callback_revocation_and_exceptions_fail_closed(env: Environment) -> None:
    env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record
    env.valid = False
    assert not env.policies.validate_capture(record, env.plan())

    def failed(*_args: object) -> bool:
        raise RuntimeError("untrusted-private-error")

    env.broker.principal_valid = failed
    assert env.policies.authorize_capture(env.plan()) is None
    assert "untrusted-private-error" not in str(env.policies.list())


def test_ephemeral_only_has_current_lease_session_policy(env: Environment) -> None:
    principal = "ephemeral:remote_mcp"
    assert env.broker.session
    env.broker.session.principal_id = principal
    with pytest.raises(CapturePolicyError, match="persistent_policy_issued_key_required"):
        env.grant(principal_id=principal)
    policy = env.grant(principal_id=principal, mode="session")
    assert policy["broker_epoch"] == "epoch-1" and policy["lease_id"] == "lease-1"
    assert env.policies.authorize_capture(env.plan())
    assert env.store.db.execute("SELECT count(*) FROM capture_policies").fetchone()[0] == 0
    env.broker.session = Session("unused", "lease-2", "remote_mcp", 400, principal)
    assert env.policies.authorize_capture(env.plan()) is None


@pytest.mark.parametrize("ttl", [0, -1, MAX_TTL_SECONDS + 1, True, 1.5, float("nan")])
def test_finite_bounded_grant_expiry(env: Environment, ttl: Any) -> None:
    with pytest.raises(CapturePolicyError, match="invalid_capture_policy_expiry"):
        env.grant(ttl_seconds=ttl)


def test_expiry_is_clamped_to_key_and_checked_at_exact_boundary(env: Environment) -> None:
    env.registry[0]["expires_at"] = 1050.0
    policy = env.grant(ttl_seconds=200)
    assert policy["expires_at"] == 1050.0
    record = env.policies.authorize_capture(env.plan())
    assert record
    env.wall.now = 1049.999
    assert env.policies.validate_capture(record, env.plan())
    env.wall.now = 1050
    assert not env.policies.validate_capture(record, env.plan())


def test_revision_edit_and_revoke_invalidate_queued_record(env: Environment) -> None:
    policy = env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record and env.policies.validate_capture(record, env.plan())
    env.store.put_plan(digest_plan(env.plan()), env.plan())
    env.store.add_job("capture-1", digest_plan(env.plan()))
    assert env.policies.validate_capture(record, env.plan())  # Already queued is valid.
    edited = env.policies.update(
        policy["policy_id"],
        expected_revision=1,
        capture_home=True,
        image_delivery=True,
        ttl_seconds=200,
        expected_target=env.policies.target(),
    )
    assert edited["revision"] == 2
    assert not env.policies.validate_capture(record, env.plan())
    with pytest.raises(CapturePolicyError, match="stale_capture_policy_revision"):
        env.policies.revoke(policy["policy_id"], expected_revision=1)
    fresh = env.policies.authorize_capture(env.plan())
    assert fresh
    revoked = env.policies.revoke(policy["policy_id"], expected_revision=2)
    assert revoked["revision"] == 3 and revoked["revoked"]
    assert not env.policies.validate_capture(fresh, env.plan())
    assert env.store.job("capture-1")["state"] == "queued"  # Engine never mutates USB/work.


def test_session_release_reacquire_restart_and_expired_lease_invalidate(env: Environment) -> None:
    env.grant(mode="session")
    record = env.policies.authorize_capture(env.plan())
    assert record
    assert env.broker.session
    env.broker.session.deadline = 99
    assert not env.policies.validate_capture(record, env.plan())
    env.broker.session.deadline = 400
    assert env.policies.authorize_capture(env.plan()) is None
    env.grant(mode="session")
    env.broker.epoch = "epoch-2"
    assert env.policies.authorize_capture(env.plan()) is None
    env.grant(mode="session")
    env.broker.session = None
    env.policies.invalidate_session()
    env.broker.session = Session("unused", "lease-2", "remote_mcp", 400, PRINCIPAL)
    assert env.policies.authorize_capture(env.plan()) is None


def test_session_create_binds_displayed_lease_and_principal(env: Environment) -> None:
    with pytest.raises(CapturePolicyError, match="capture_policy_lease_changed"):
        env.grant(mode="session", expected_lease_id="earlier-lease")
    with pytest.raises(CapturePolicyError, match="capture_policy_principal_mismatch"):
        env.grant(mode="session", principal_id=OTHER)


def test_persistent_survives_engine_restart_but_queue_authorization_does_not(
    env: Environment,
) -> None:
    env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record
    env.broker.epoch = "epoch-2"
    restarted = CapturePolicies(env.broker, lambda: env.registry, wall_clock=env.wall)
    assert len(restarted.list()["policies"]) == 1
    assert not restarted.validate_capture(record, env.plan())
    assert restarted.authorize_capture(env.plan())
    env.wall.now += 200
    assert restarted.authorize_capture(env.plan()) is None


def test_serial_requires_configured_firmware_and_separate_current_lease_arm(
    env: Environment,
) -> None:
    env.serial(None)
    with pytest.raises(CapturePolicyError, match="capture_policy_firmware_required"):
        env.grant()
    env.serial()
    env.grant()
    assert env.policies.authorize_capture(env.plan()) is None
    with pytest.raises(CapturePolicyError, match="capture_arm_attestation_required"):
        env.arm(attestation="HOME")
    with pytest.raises(CapturePolicyError, match="capture_arm_attestation_required"):
        env.arm(acknowledge_effects=False)
    with pytest.raises(CapturePolicyError, match="invalid_capture_arm_expiry"):
        env.arm(ttl_seconds=MAX_ARM_SECONDS + 1)
    env.arm()
    record = env.policies.authorize_capture(env.plan())
    assert record
    assert env.policies.validate_capture(record, env.plan())
    assert not env.policies.validate_capture(record, env.plan(expected_firmware_sha256="b" * 64))
    assert env.policies.authorize_capture(env.plan(expected_firmware_sha256="b" * 64)) is None


def test_serial_arm_allows_same_lease_renewal_but_expires_independently(env: Environment) -> None:
    env.serial()
    env.grant()
    env.arm(ttl_seconds=100)
    record = env.policies.authorize_capture(env.plan())
    assert record and env.broker.session
    env.monotonic.now = 199.9
    env.broker.session.deadline = 499.9
    assert env.policies.validate_capture(record, env.plan())
    env.monotonic.now = 200
    assert not env.policies.validate_capture(record, env.plan())


def test_serial_arm_cannot_cross_release_review_or_target_change(env: Environment) -> None:
    env.serial()
    env.grant()
    env.arm()
    env.broker.session = Session("unused", "lease-2", "remote_mcp", 400, PRINCIPAL)
    assert env.policies.authorize_capture(env.plan()) is None
    env.arm()
    env.store.put_plan(digest_plan(env.plan()), env.plan())
    env.store.add_job("uncertain", digest_plan(env.plan()))
    env.store.set_job("uncertain", "needs_review")
    assert env.policies.authorize_capture(env.plan()) is None
    env.store.db.execute("UPDATE jobs SET reviewed=1 WHERE id='uncertain'")
    env.store.db.commit()
    assert env.policies.authorize_capture(env.plan()) is None
    env.arm()
    env.serial("b" * 64)
    assert env.policies.authorize_capture(env.plan()) is None
    assert env.policies.options()["arming"] is None


def test_target_stable_without_dynamic_com_name_and_changes_with_selector(env: Environment) -> None:
    env.serial()
    initial = env.policies.target()
    env.broker.backend.port = "COM99"  # type: ignore[attr-defined]
    assert env.policies.target() == initial
    assert env.broker.settings.serial
    serial = env.broker.settings.serial.model_copy(
        update={
            "selector": DeviceSelector(vid=1, pid=2, serial_number="another-board"),
        }
    )
    env.broker.settings = env.broker.settings.model_copy(update={"serial": serial})
    assert env.policies.target() != initial
    with pytest.raises(CapturePolicyError, match="capture_policy_target_changed"):
        env.grant(expected_target=initial)


def test_plan_binding_cannot_be_reused_for_another_job_or_principal(env: Environment) -> None:
    env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record
    assert not env.policies.validate_capture(record, env.plan(job_id="other-job"))
    assert env.broker.session
    env.broker.session.principal_id = OTHER
    assert not env.policies.validate_capture(record, env.plan())


@pytest.mark.parametrize("mode", ["persistent", "session"])
def test_image_policy_rechecks_every_read_and_respects_lifecycle(
    env: Environment,
    mode: str,
) -> None:
    policy = env.grant(mode=mode, capture_home=False)
    image = env.image()
    assert env.policies.authorize_image("capture-1", image)
    audit_before = env.store.db.execute("SELECT count(*) FROM capture_policy_audit").fetchone()[0]
    assert env.policies.authorize_image("capture-1", image, record_use=False)
    audit_after = env.store.db.execute("SELECT count(*) FROM capture_policy_audit").fetchone()[0]
    assert audit_after == audit_before
    assert not env.policies.authorize_image("capture-1", {**image, "delivery_state": "rejected"})
    assert not env.policies.authorize_image("capture-1", {**image, "principal_id": OTHER})
    assert not env.policies.authorize_image("capture-1", {**image, "policy_target": "b" * 64})
    if mode == "persistent":
        env.broker.session = None
        assert env.policies.authorize_image("capture-1", image)
        env.policies.revoke(policy["policy_id"], expected_revision=1)
        assert not env.policies.authorize_image("capture-1", image)
    else:
        env.broker.session = Session("unused", "lease-2", "remote_mcp", 400, PRINCIPAL)
        assert not env.policies.authorize_image("capture-1", image)


def test_delivery_requires_live_retention_and_original_capture_plan(env: Environment) -> None:
    env.grant(capture_home=False)
    image = env.image()
    assert not env.policies.authorize_image("missing", image)
    env.monotonic.now = 400
    assert not env.policies.authorize_image("capture-1", image)


def test_policy_metadata_and_fixed_audit_are_bounded_no_credentials(env: Environment) -> None:
    env.grant()
    for _ in range(MAX_AUDIT_EVENTS + 2):
        env.policies.authorize_capture(env.plan(test="forbidden-raw-command"))
    audit = env.policies.list()["audit"]
    assert len(audit) == MAX_AUDIT_EVENTS
    history = "\n".join(env.store.db.iterdump())
    for private in ("not-a-real-capability", "forbidden-raw-command", "png", "bearer"):
        assert private not in history
    assert {entry["event"] for entry in audit} == {"capture_denied"}


def test_options_are_current_metadata_and_finite_operator_values(env: Environment) -> None:
    options = env.policies.options()
    assert options["broker_epoch"] == "epoch-1" and options["server_now"] == 1000
    assert options["principals"][0]["kind"] == "issued"
    assert options["active_lease"]["lease_id"] == "lease-1"
    assert options["max_ttl_seconds"] == MAX_TTL_SECONDS
    assert "Esc" in options["effects"][0]
    env.registry[0]["revoked_at"] = env.wall.now
    assert env.policies.options()["principals"] == []


@pytest.mark.parametrize("operation", ["authorize", "validate", "image"])
@pytest.mark.parametrize("expires", ["grant", "key", "lease", "arm", "retention"])
def test_deadlines_are_rechecked_after_principal_metadata_callback(
    env: Environment,
    operation: str,
    expires: str,
) -> None:
    if expires == "arm":
        env.serial()
    env.grant(ttl_seconds=1 if expires == "grant" else 200)
    if expires == "arm":
        env.arm(ttl_seconds=1)
    record = env.policies.authorize_capture(env.plan())
    assert record
    image = env.image()
    if expires == "key":
        env.registry[0]["expires_at"] = 1001.0

    def delayed(*_args: object) -> bool:
        if expires in ("grant", "key"):
            env.wall.now = 1002.0
        elif expires in ("lease", "retention"):
            env.monotonic.now = 401.0
        else:
            env.monotonic.now = 102.0
        return True

    env.broker.principal_valid = delayed
    if operation == "authorize":
        assert env.policies.authorize_capture(env.plan()) is None
    elif operation == "validate":
        assert not env.policies.validate_capture(record, env.plan())
    elif expires == "arm":
        # Delivery has its own explicit grant and does not send USB commands.
        assert env.policies.authorize_image("capture-1", image)
    else:
        assert not env.policies.authorize_image("capture-1", image)


def test_registry_iteration_can_cross_key_expiry_after_matching_metadata(env: Environment) -> None:
    env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record
    env.registry[0]["expires_at"] = 1001.0

    def delayed_registry() -> Iterator[object]:
        yield env.registry[0]
        env.wall.now = 1002.0

    env.policies.list_principals = delayed_registry
    assert not env.policies.validate_capture(record, env.plan())


def test_callback_revoking_grant_cannot_authorize_stale_snapshot(env: Environment) -> None:
    policy = env.grant()
    record = env.policies.authorize_capture(env.plan())
    assert record

    def revoke(*_args: object) -> bool:
        env.broker.principal_valid = lambda *_: True
        env.policies.revoke(policy["policy_id"], expected_revision=1)
        return True

    env.broker.principal_valid = revoke
    assert not env.policies.validate_capture(record, env.plan())


def fail_audit(env: Environment) -> None:
    env.policies.list()  # Ensure the owned schema exists before adding a synthetic fault.
    env.store.db.executescript("""
        CREATE TEMP TRIGGER fail_policy_audit BEFORE INSERT ON capture_policy_audit
        BEGIN SELECT RAISE(ABORT, 'private-database-failure'); END;
    """)


def restore_audit(env: Environment) -> None:
    assert not env.store.db.in_transaction
    env.store.db.execute("DROP TRIGGER fail_policy_audit")


@pytest.mark.parametrize("mode", ["persistent", "session"])
def test_failed_create_does_not_publish_unaudited_permission(env: Environment, mode: str) -> None:
    fail_audit(env)
    with pytest.raises(CapturePolicyError, match="^capture_policy_storage_failed$"):
        env.grant(mode=mode)
    restore_audit(env)
    assert env.policies.list()["policies"] == []
    assert env.store.db.execute("SELECT count(*) FROM capture_policies").fetchone()[0] == 0
    assert env.policies.authorize_capture(env.plan()) is None


@pytest.mark.parametrize("mode", ["persistent", "session"])
def test_failed_update_preserves_only_previous_permissions_and_revision(
    env: Environment,
    mode: str,
) -> None:
    policy = env.grant(mode=mode, capture_home=False, ttl_seconds=10)
    fail_audit(env)
    with pytest.raises(CapturePolicyError, match="^capture_policy_storage_failed$"):
        env.policies.update(
            policy["policy_id"],
            expected_revision=1,
            capture_home=True,
            image_delivery=True,
            ttl_seconds=200,
            expected_target=env.policies.target(),
        )
    restore_audit(env)
    actual = env.policies.list()["policies"][0]
    assert actual["revision"] == 1 and actual["capture_home"] is False
    assert actual["expires_at"] == policy["expires_at"]
    assert env.policies.authorize_capture(env.plan()) is None
    image = env.image()
    assert env.policies.authorize_image("capture-1", image)
    env.wall.now += 11
    assert not env.policies.authorize_image("capture-1", image)


def test_failed_arm_does_not_publish_permission_or_extend_existing_arm(env: Environment) -> None:
    env.serial()
    env.grant()
    fail_audit(env)
    with pytest.raises(CapturePolicyError, match="^capture_policy_storage_failed$"):
        env.arm()
    restore_audit(env)
    assert env.policies.options()["arming"] is None
    assert env.policies.authorize_capture(env.plan()) is None
    env.arm(ttl_seconds=1)
    fail_audit(env)
    with pytest.raises(CapturePolicyError, match="^capture_policy_storage_failed$"):
        env.arm(ttl_seconds=100)
    restore_audit(env)
    assert env.policies.options()["arming"]["remaining_seconds"] == 1
    env.monotonic.now += 2
    assert env.policies.authorize_capture(env.plan()) is None


@pytest.mark.parametrize("mode", ["persistent", "session"])
def test_audit_failure_does_not_roll_back_completed_revocation(env: Environment, mode: str) -> None:
    policy = env.grant(mode=mode)
    record = env.policies.authorize_capture(env.plan())
    assert record
    fail_audit(env)
    with pytest.raises(CapturePolicyError, match="^capture_policy_revoked_audit_failed$"):
        env.policies.revoke(policy["policy_id"], expected_revision=1)
    restore_audit(env)
    assert not env.policies.validate_capture(record, env.plan())
    actual = env.policies.list()["policies"][0]
    assert actual["revision"] == 2 and actual["revoked"] is True
    if mode == "persistent":
        restarted = CapturePolicies(env.broker, lambda: env.registry, wall_clock=env.wall)
        assert restarted.authorize_capture(env.plan()) is None


@pytest.mark.parametrize("mode", ["persistent", "session"])
@pytest.mark.parametrize("fault", ["prune", "commit"])
def test_audit_prune_and_commit_failures_roll_back_and_never_publish(
    env: Environment,
    mode: str,
    fault: str,
) -> None:
    env.policies.list()
    if fault == "prune":
        env.store.db.executemany(
            "INSERT INTO capture_policy_audit(event,at) VALUES ('capture_denied',1000)",
            [()] * MAX_AUDIT_EVENTS,
        )
        env.store.db.commit()
        env.store.db.executescript("""
            CREATE TEMP TRIGGER fail_policy_audit BEFORE DELETE ON capture_policy_audit
            BEGIN SELECT RAISE(ABORT, 'private-prune-failure'); END;
        """)
    else:
        # The audit INSERT succeeds; the deferred foreign key fails only at COMMIT.
        env.store.db.execute("PRAGMA foreign_keys=ON")
        env.store.db.executescript("""
            CREATE TEMP TABLE synthetic_parent (id INTEGER PRIMARY KEY);
            CREATE TEMP TABLE synthetic_child (
                parent INTEGER REFERENCES synthetic_parent(id) DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TEMP TRIGGER fail_policy_audit AFTER INSERT ON capture_policy_audit
            BEGIN INSERT INTO synthetic_child VALUES (1); END;
        """)
    audit_count = env.store.db.execute("SELECT count(*) FROM capture_policy_audit").fetchone()[0]
    with pytest.raises(CapturePolicyError, match="^capture_policy_storage_failed$"):
        env.grant(mode=mode)
    restore_audit(env)
    assert env.policies.list()["policies"] == []
    assert env.store.db.execute("SELECT count(*) FROM capture_policies").fetchone()[0] == 0
    actual_count = env.store.db.execute("SELECT count(*) FROM capture_policy_audit").fetchone()[0]
    assert actual_count == audit_count
    assert env.policies.authorize_capture(env.plan()) is None
