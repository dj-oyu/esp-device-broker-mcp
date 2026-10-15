"""Real broker/store/source provenance and synthetic ROM only; never USB or SDK."""

import hashlib
import json
import shutil
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_flash_manifest_contract as manifest_tests
import test_source_snapshot as source_tests
from test_flash_lifecycle import FakeDriver
from test_flash_lkg_geometry import table as partition_table

from cardputer_mcp.broker import Broker, BrokerError, digest_plan
from cardputer_mcp.build_locations import build_location, claim_build_directory
from cardputer_mcp.flash_models import FlashDevice, UnattendedFlash
from cardputer_mcp.gui_approvals import ATTESTATIONS
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action
from cardputer_mcp.plan_approval import PlanApprovals


@pytest.fixture
async def env(tmp_path):
    source = source_tests.SourceTests()
    source.setUp()
    fixture = manifest_tests.FlashManifestTests("test_verified_layout_is_inert_and_manifest_bound")
    fixture.setUp()
    broker = None
    try:
        fetched = source.fetch(principal="alice")
        receipt = fetched["source_receipt"]
        elf = b"synthetic ELF contract"
        elf_hash = hashlib.sha256(elf).hexdigest()
        (fixture.root / "app.elf").write_bytes(elf)
        fixture.manifest["artifacts"].append(fixture.receipt("app.elf", elf))
        table = partition_table()
        (fixture.root / "partition_table.bin").write_bytes(table)
        fixture.manifest["artifacts"].append(fixture.receipt("partition_table.bin", table))
        fixture.layout["bootloader"] = {"offset": "0x0", "file": "boot.bin"}
        fixture.layout["partition-table"] = {"offset": "0x8000", "file": "partition_table.bin"}
        fixture.layout["flash_files"]["0x8000"] = "partition_table.bin"
        fixture.save_layout()
        registration = FlashDevice(
            selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
            flash_bytes=8388608,
            project_id="fixture",
            python_tool={
                "executable": tmp_path / "python.exe",
                "sha256": "a" * 64,
                "version": "Python 3.13.16",
            },
            esptool_directory=tmp_path / "tools",
            esptool_sha256="b" * 64,
            backup_directory=tmp_path / "backups",
            private_backup_acl_confirmed=True,
            original_elf_sha256=elf_hash,
            allow_backup=True,
            allow_flash=True,
            allow_restore=True,
        )
        broker = Broker(
            source.settings.model_copy(
                update={
                    "flash_devices": {"fixture": registration},
                    "allow_mock_execution": True,
                }
            ),
            lock_path=tmp_path / "device.lockfile",
            principal_valid=lambda *args: True,
        )
        driver = FakeDriver()
        driver.data = bytearray(b"x" * registration.flash_bytes)
        driver.data[0x8000 : 0x8000 + len(table)] = table
        driver.security = replace(driver.security, flash_bytes=registration.flash_bytes)
        driver.summary["elf_sha256_prefix"] = elf_hash[:16]
        broker.firmware_flash.driver_factory = lambda *args: driver
        await broker.start()
        session = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="alice")[
            "session"
        ]
        store = broker._store()
        fetch_plan = source.snapshots.prepare("fixture", source_tests.COMMIT, "alice") | {
            "job_id": "fetch-one",
            "test": Action.SOURCE.value,
        }
        fetch_hash = digest_plan(fetch_plan)
        store.put_plan(fetch_hash, fetch_plan)
        store.add_job("fetch-one", fetch_hash)
        store.set_job("fetch-one", "succeeded", diagnostic={"source": fetched})
        _, metadata = source.snapshots.resolve("fixture", receipt, "alice")
        provenance = {
            key: metadata[key] for key in ("repository", "commit", "tree_sha1", "source_sha256")
        } | {"source_receipt": receipt, "dependency_policy": "vendored_components_no_fetch"}
        project = broker.settings.build_projects["fixture"]
        output = claim_build_directory(
            broker.settings.state_dir,
            project.build_root,
            fixture.manifest["fingerprint"],
            fixture.manifest["environment_fingerprint"],
        )
        shutil.copytree(fixture.root, output, dirs_exist_ok=True)
        fixture.manifest["build_location"] = build_location(
            fixture.manifest["fingerprint"],
            fixture.manifest["environment_fingerprint"],
            project.build_root,
        )
        build_plan = {
            "job_id": "build-one",
            "test": Action.BUILD.value,
            "build_principal": "alice",
            "project_id": "fixture",
            "fingerprint": fixture.manifest["fingerprint"],
            "source_receipt": receipt,
            "source_provenance": provenance,
        }
        build_hash = digest_plan(build_plan)
        store.put_plan(build_hash, build_plan)
        store.add_job("build-one", build_hash)
        store.set_job("build-one", "running")
        fixture.manifest.update(
            build_job_id="build-one",
            build_plan_hash=build_hash,
            source_provenance=provenance,
        )
        (output / "broker-manifest.json").write_text(json.dumps(fixture.manifest))
        broker.firmware_build.artifacts.publish("build-one", "alice", output, fixture.manifest)
        store.set_job("build-one", "succeeded")

        def prepare(job, action=Action.FLASH, **references):
            return broker.prepare(
                PrepareRequest(
                    session=session,
                    job_id=job,
                    test=action,
                    device_id="fixture",
                    **references,
                )
            )

        def execute(prepared):
            return broker.execute(ExecuteRequest(session=session, plan_hash=prepared["plan_hash"]))

        def approve(prepared):
            service = PlanApprovals(broker)
            detail = service.detail(prepared["plan_hash"])
            service.approve(
                prepared["plan_hash"],
                detail["review_revision"],
                ATTESTATIONS[detail["attestation"]],
            )

        # Existing explicit manual bootstrap; unattended never authorizes this full backup.
        initial = prepare("backup-one", Action.BACKUP)
        approve(initial)
        execute(initial)
        await broker.queue.join()
        assert broker.job("backup-one")["state"] == "succeeded"
        initial = prepare("flash-one", build_job_id="build-one", backup_job_id="backup-one")
        approve(initial)
        execute(initial)
        await broker.queue.join()
        assert broker.job("flash-one")["state"] == "succeeded"
        selected = store.firmware_current("fixture", "alice")["receipt"]
        policy = UnattendedFlash(
            enabled=True,
            principal_id="alice",
            device_id="fixture",
            project_id="fixture",
            operation="firmware_flash",
            source_receipt=receipt,
            source_repository=source_tests.REPOSITORY,
            source_commit=source_tests.COMMIT,
        )

        def configure(value=policy, **changes):
            updated = registration.model_copy(update={"unattended_flash": value, **changes})
            broker.settings = broker.settings.model_copy(
                update={"flash_devices": {"fixture": updated}}
            )
            broker.firmware_flash.settings = broker.settings

        yield SimpleNamespace(
            broker=broker,
            driver=driver,
            store=store,
            policy=policy,
            receipt=receipt,
            configure=configure,
            prepare=prepare,
            execute=execute,
            approve=approve,
            selected=selected,
            output=output,
            source=source,
        )
    finally:
        if broker is not None:
            await broker.close()
        fixture.doCleanups()
        source.tearDown()


async def test_default_manual_and_allow_flash_does_not_grant_unattended(env):
    prepared = env.prepare("manual", build_job_id="build-one")
    assert prepared["approval_required"]
    with pytest.raises(BrokerError, match="operator_approval_required"):
        env.execute(prepared)
    env.configure(env.policy.model_copy(update={"enabled": False}))
    assert env.prepare("disabled", build_job_id="build-one")["approval_required"]
    assert env.driver.writes == 1  # Initial manual bundle only.


async def test_exact_owned_source_receipt_auto_flash_commits_lkg_once(env):
    env.configure()
    prepared = env.prepare("automatic", build_job_id="build-one")
    assert not prepared["approval_required"]
    assert not env.execute(prepared)["deduplicated"]
    await env.broker.queue.join()
    assert env.broker.job("automatic")["state"] == "succeeded"
    assert env.store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert env.store.firmware_current("fixture", "alice")["receipt"] == env.selected
    assert env.execute(prepared)["deduplicated"]
    assert not env.broker.flash_policy_jobs and env.driver.closed


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal_id", "bob"),
        ("device_id", "other"),
        ("project_id", "other"),
        ("source_receipt", "b" * 64),
        ("source_repository", "fixture/other"),
        ("source_commit", "b" * 40),
    ],
)
async def test_policy_scope_mismatch_requires_manual_without_writes(env, field, value):
    env.configure(env.policy.model_copy(update={field: value}))
    before = env.driver.writes
    prepared = env.prepare("mismatch", build_job_id="build-one")
    assert prepared["approval_required"]
    with pytest.raises(BrokerError, match="operator_approval_required"):
        env.execute(prepared)
    assert env.driver.writes == before


@pytest.mark.parametrize(
    "action,references",
    [
        (Action.BACKUP, {}),
        (Action.RESTORE, {}),
        (Action.FLASH, {"build_job_id": "build-one", "backup_job_id": "backup-one"}),
    ],
)
async def test_backup_restore_and_full_backup_inputs_remain_manual(env, action, references):
    env.configure()
    prepared = env.prepare("excluded", action, **references)
    assert prepared["approval_required"]
    with pytest.raises(BrokerError, match="operator_approval_required"):
        env.execute(prepared)


@pytest.mark.parametrize(
    "which", ["source_failed", "source_other_owner", "worktree_build", "source_bytes"]
)
async def test_successful_owned_source_provenance_is_required(env, which):
    env.configure()
    if which == "source_failed":
        env.store.set_job("fetch-one", "failed")
    elif which in ("source_other_owner", "worktree_build"):
        name = "fetch-one" if which == "source_other_owner" else "build-one"
        job = env.store.job(name)
        plan = env.store.plan(job["plan_hash"])
        if which == "source_other_owner":
            plan["source_principal"] = "bob"
        else:
            plan.pop("source_receipt")
        env.store.db.execute(
            "UPDATE plans SET payload=? WHERE hash=?", (json.dumps(plan), job["plan_hash"])
        )
        env.store.db.commit()
    else:
        root, _ = env.source.snapshots.resolve("fixture", env.receipt, "alice")
        (root / "CMakeLists.txt").write_bytes(b"tampered")
    prepared = env.prepare("untrusted", build_job_id="build-one")
    assert prepared["approval_required"]
    with pytest.raises(BrokerError, match="operator_approval_required"):
        env.execute(prepared)


@pytest.mark.parametrize(
    "change", ["policy", "project", "principal", "lease", "artifact", "source"]
)
async def test_queued_authorization_is_rechecked_before_device_writes(env, change):
    env.configure()
    prepared = env.prepare("queued", build_job_id="build-one")
    env.execute(prepared)
    before = env.driver.writes
    if change == "policy":
        env.configure(env.policy.model_copy(update={"enabled": False}))
    elif change == "project":
        project = env.broker.settings.build_projects["fixture"]
        env.broker.settings = env.broker.settings.model_copy(
            update={
                "build_projects": {
                    "fixture": project.model_copy(
                        update={"profile_execution_policy": "RemoteSigned"}
                    )
                }
            }
        )
    elif change == "principal":
        env.broker.principal_valid = lambda *args: False
    elif change == "lease":
        env.broker.session.deadline = 0
    elif change == "artifact":
        (env.output / "app.bin").write_bytes(b"tampered")
    else:
        env.store.set_job("fetch-one", "failed")
    await env.broker.queue.join()
    assert env.broker.job("queued")["state"] == "cancelled"
    assert env.driver.writes == before
    assert env.store.firmware_current("fixture", "alice")["receipt"] == env.selected
    assert not env.broker.flash_policy_jobs


async def test_revocation_in_last_auth_callback_cannot_start_device(env):
    env.configure()
    prepared = env.prepare("reentrant", build_job_id="build-one")
    env.execute(prepared)
    calls = 0

    def current(*args):
        nonlocal calls
        calls += 1
        # Worker's final callback after full policy provenance validation.
        if calls == 3:
            env.configure(env.policy.model_copy(update={"enabled": False}))
        return True

    env.broker.principal_valid = current
    before = env.driver.writes
    await env.broker.queue.join()
    assert env.broker.job("reentrant")["state"] == "cancelled"
    assert env.driver.writes == before


@pytest.mark.parametrize("late_revoke", [False, True])
async def test_failed_or_late_revoked_flash_preserves_lkg_and_halts(env, late_revoke):
    env.configure()
    prepared = env.prepare("uncertain", build_job_id="build-one")
    original = env.broker.firmware_flash.commit_success
    if late_revoke:

        def commit(plan, result, live):
            env.configure(env.policy.model_copy(update={"enabled": False}))
            return original(plan, result, live)

        env.broker.firmware_flash.commit_success = commit
    else:

        def failed_write(segments):
            raise ValueError("synthetic write failure")

        env.driver.write = failed_write
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("uncertain")["state"] == "needs_review"
    assert env.store.firmware_current("fixture", "alice")["receipt"] == env.selected
    assert env.driver.closed and env.store.needs_review()
    with pytest.raises(BrokerError, match="recovery_review_required"):
        env.prepare("blocked", build_job_id="build-one")


def test_registration_is_explicit_strict_and_flash_only():
    data = dict(
        principal_id="alice",
        device_id="fixture",
        project_id="fixture",
        operation="firmware_flash",
        source_receipt="a" * 64,
        source_repository="fixture/project",
        source_commit="a" * 40,
    )
    assert UnattendedFlash(**data).enabled is False
    for update in (
        {"enabled": "true"},
        {"operation": "firmware_restore"},
        {"operation": "firmware_backup"},
        {"principal_id": "*"},
    ):
        with pytest.raises(ValueError):
            UnattendedFlash(**(data | update))


async def test_allow_flash_still_gates_operation(env):
    env.configure(allow_flash=False)
    with pytest.raises(BrokerError, match="registered_flash_operation_not_enabled"):
        env.prepare("disabled-operation", build_job_id="build-one")


async def test_missing_lkg_never_becomes_unattended(env):
    env.configure()
    env.store.db.execute("DELETE FROM firmware_lkg")
    env.store.db.commit()
    with pytest.raises(BrokerError, match="verified_lkg_or_initial_backup_required"):
        env.prepare("missing-recovery", build_job_id="build-one")


async def test_draining_cancelled_queue_discards_transient_permission(env):
    env.configure()
    prepared = env.prepare("drained", build_job_id="build-one")
    env.execute(prepared)
    before = env.driver.writes
    env.broker._cancel_queued("session_released")
    assert env.broker.job("drained")["state"] == "cancelled"
    assert not env.broker.flash_policy_jobs and env.broker.queue.empty()
    assert env.driver.writes == before
    assert env.store.firmware_current("fixture", "alice")["receipt"] == env.selected
