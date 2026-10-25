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
from cardputer_mcp.firmware_flash import operation_from_plan
from cardputer_mcp.flash_backup import BackupStore
from cardputer_mcp.flash_lifecycle import FlashLifecycle
from cardputer_mcp.flash_models import FlashDevice, UnattendedFlash
from cardputer_mcp.models import (
    AcquireRequest,
    ExecuteRequest,
    LeaseRequest,
    Origin,
    PrepareRequest,
)
from cardputer_mcp.models import TestName as Action


@pytest.fixture
async def env(tmp_path, request):
    # Each case keeps its own broker, SQLite database, files and driver. Only
    # the prerequisite backup avoids repeating broker dispatch/stage commits;
    # the full 8 MiB broker bootstrap remains covered explicitly below.
    bootstrap = getattr(request, "param", None)
    assert bootstrap in (None, "broker-bootstrap"), "unknown bootstrap fixture mode"
    broker_bootstrap = bootstrap == "broker-bootstrap"
    flash_size = "8MB" if broker_bootstrap else "4MB"
    flash_bytes = (8 if broker_bootstrap else 4) * 1024 * 1024
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
        size_index = fixture.layout["write_flash_args"].index("--flash-size") + 1
        fixture.layout["write_flash_args"][size_index] = flash_size
        fixture.layout["flash_settings"]["flash_size"] = flash_size
        fixture.save_layout()
        registration = FlashDevice(
            selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
            flash_bytes=flash_bytes,
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

        # The scenario under test starts with an owned backup and a flashed LKG.
        # Run the prerequisite ROM backup/persistence directly in matrix cases;
        # every initial flash and scenario operation still uses the real broker.
        initial = prepare("backup-one", Action.BACKUP)
        if broker_bootstrap:
            execute(initial)
            await broker.queue.join()
        else:
            operation = operation_from_plan(initial["plan"])
            backup_engine = FlashLifecycle(driver)
            backup_receipt, backup_data, _ = backup_engine.run(
                operation,
                operation.digest,
                live=lambda: True,
                reader_closed=lambda: (
                    not broker.backend.active
                    and not broker.backend.hardware_connected
                    and not getattr(broker.backend, "ownership_uncertain", False)
                ),
                stage=lambda _: None,
            )
            assert backup_receipt is not None and backup_data is not None
            BackupStore(
                registration.backup_directory,
                windows_private_acl_confirmed=registration.private_backup_acl_confirmed,
            ).save("backup-one", "alice", backup_receipt, backup_data)
            assert driver.closed and not backup_engine.active and not backup_engine.uncertain
            store.add_job("backup-one", initial["plan_hash"])
            store.set_job("backup-one", "succeeded")
        assert broker.job("backup-one")["state"] == "succeeded"
        initial = prepare("flash-one", build_job_id="build-one", backup_job_id="backup-one")
        execute(initial)
        await broker.queue.join()
        assert broker.job("flash-one")["state"] == "succeeded"
        selected = store.firmware_current("fixture")["receipt"]
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
            session=session,
            selected=selected,
            output=output,
            source=source,
        )
    finally:
        if broker is not None:
            await broker.close()
        fixture.doCleanups()
        source.tearDown()


@pytest.mark.parametrize("env", ["broker-bootstrap"], indirect=True)
async def test_preparation_never_executes_and_no_policy_grant_is_needed(env):
    before = env.driver.writes
    prepared = env.prepare("explicit", build_job_id="build-one")
    assert prepared["approval_required"] is False
    assert env.store.job("explicit") is None and env.driver.writes == before
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("explicit")["state"] == "succeeded"


async def test_exact_owned_source_receipt_auto_flash_commits_lkg_once(env):
    env.configure()
    prepared = env.prepare("automatic", build_job_id="build-one")
    assert not prepared["approval_required"]
    assert not env.execute(prepared)["deduplicated"]
    await env.broker.queue.join()
    assert env.broker.job("automatic")["state"] == "succeeded", env.broker.job("automatic")
    assert env.store.firmware_current("fixture")["completed_job"] == "automatic"
    assert env.store.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0
    assert env.store.firmware_current("fixture")["receipt"] == env.selected
    assert env.execute(prepared)["deduplicated"]
    assert not env.broker._execution_intents and env.driver.closed


@pytest.mark.parametrize(
    "action,references",
    [
        (Action.BACKUP, {}),
        (Action.RESTORE, {}),
        (Action.FLASH, {"build_job_id": "build-one", "backup_job_id": "backup-one"}),
    ],
)
async def test_backup_restore_and_full_backup_require_separate_explicit_request(
    env, action, references
):
    env.configure()
    prepared = env.prepare("separate", action, **references)
    assert prepared["approval_required"] is False
    assert env.store.job("separate") is None
    env.execute(prepared)
    await env.broker.queue.join()
    if action == Action.FLASH:
        # The original full backup no longer matches the already flashed base. Review
        # classification fix: the pre-write fence stopped it before any write with a
        # confirmed close, so it is `failed` (was needs_review) with the same code.
        assert env.broker.job("separate")["state"] == "failed"
        assert env.broker.job("separate")["result"] == "flash_backup_stale_needs_review"
    else:
        assert env.broker.job("separate")["state"] == "succeeded"


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
    before = env.driver.writes
    with pytest.raises(BrokerError, match="flash_binding_invalid|backend_preflight_failed"):
        prepared = env.prepare("untrusted", build_job_id="build-one")
        env.execute(prepared)
    assert env.driver.writes == before


@pytest.mark.parametrize(
    "change", ["registration", "project", "principal", "lease", "artifact", "source"]
)
async def test_queued_authorization_is_rechecked_before_device_writes(env, change):
    env.configure()
    prepared = env.prepare("queued", build_job_id="build-one")
    env.execute(prepared)
    before = env.driver.writes
    if change == "registration":
        env.configure(allow_flash=False)
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
    assert env.broker.job("queued")["state"] in ("cancelled", "needs_review")
    assert env.driver.writes == before
    assert env.store.firmware_current("fixture")["receipt"] == env.selected
    assert not env.broker._execution_intents


async def test_revocation_in_last_auth_callback_cannot_start_device(env):
    env.configure()
    prepared = env.prepare("reentrant", build_job_id="build-one")
    env.execute(prepared)
    calls = 0

    def current(*args):
        nonlocal calls
        calls += 1
        # Worker's final callback after full policy provenance validation.
        if calls == 2:
            env.configure(allow_flash=False)
        return True

    env.broker.principal_valid = current
    before = env.driver.writes
    await env.broker.queue.join()
    assert env.broker.job("reentrant")["state"] == "cancelled"
    assert env.driver.writes == before


@pytest.mark.parametrize("late_revoke", [False, True])
async def test_failed_or_late_revoked_flash_preserves_lkg_and_current_guards(env, late_revoke):
    env.configure()
    prepared = env.prepare("uncertain", build_job_id="build-one")
    original = env.broker.firmware_flash.commit_success
    if late_revoke:

        def commit(plan, result, live):
            env.configure(allow_flash=False)
            return original(plan, result, live)

        env.broker.firmware_flash.commit_success = commit
    else:

        def failed_write(segments):
            raise ValueError("synthetic write failure")

        env.driver.write = failed_write
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("uncertain")["state"] == "needs_review"
    assert env.store.firmware_current("fixture")["receipt"] == env.selected
    assert env.driver.closed and env.store.needs_review()
    if late_revoke:
        assert env.broker.status()["execution_uncertain"] is True
        with pytest.raises(BrokerError, match="recovery_review_required"):
            env.prepare("blocked", build_job_id="build-one")
    else:
        assert env.broker.status()["execution_uncertain"] is False
        env.prepare("fresh", build_job_id="build-one")


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


async def test_second_atomic_commit_guard_rechecks_registration_and_rolls_back(env):
    env.configure()
    prepared = env.prepare("atomic-revoke", build_job_id="build-one")
    original = env.store.finish_firmware
    previous = env.store.firmware_current("fixture")
    calls = 0

    def finish(job_id, result, promotion, guard, *args):
        def guarded():
            nonlocal calls
            calls += 1
            if calls == 2:
                env.configure(allow_flash=False)
            return guard()

        return original(job_id, result, promotion, guarded, *args)

    env.store.finish_firmware = finish
    env.execute(prepared)
    await env.broker.queue.join()
    assert calls == 2
    assert env.broker.job("atomic-revoke")["state"] == "needs_review"
    assert env.store.firmware_current("fixture") == previous


@pytest.mark.parametrize("phase", ["dispatch", "prewrite", "commit"])
@pytest.mark.parametrize("replacement", ["replace", "delete"])
async def test_last_auth_callback_cannot_replace_owned_fetch_receipt(env, phase, replacement):
    env.configure()
    prepared = env.prepare("receipt-revoke", build_job_id="build-one")
    env.execute(prepared)
    before = env.driver.writes
    previous = env.store.firmware_current("fixture")
    fetch_hash = env.store.job("fetch-one")["plan_hash"]
    calls = 0
    changed = False
    second_guard = False
    original = env.store.finish_firmware
    guard_calls = 0

    def finish(job_id, result, promotion, guard, *args):
        def guarded():
            nonlocal guard_calls, second_guard
            guard_calls += 1
            second_guard = guard_calls == 2
            return guard()

        return original(job_id, result, promotion, guarded, *args)

    def current(*args):
        nonlocal calls, changed
        calls += 1
        trigger = (
            (phase == "dispatch" and calls == 2)
            or (phase == "prewrite" and env.driver.active)
            or (phase == "commit" and second_guard)
        )
        if trigger and not changed:
            changed = True
            diagnostic = (
                json.dumps({"source": {"source_receipt": "b" * 64}})
                if replacement == "replace"
                else None
            )
            env.store.db.execute("UPDATE jobs SET diagnostic=? WHERE id='fetch-one'", (diagnostic,))
            if phase != "commit":
                env.store.db.commit()
            fetched = env.store.job("fetch-one")
            assert fetched["state"] == "succeeded" and fetched["plan_hash"] == fetch_hash
        return True

    env.store.finish_firmware = finish
    env.broker.principal_valid = current
    await env.broker.queue.join()
    assert changed
    # Review classification fix: losing the receipt before the write ("prewrite")
    # with a confirmed close is `failed` (was needs_review); after the write
    # ("commit") it stays needs_review.
    expected = {"dispatch": "cancelled", "prewrite": "failed"}.get(phase, "needs_review")
    assert env.broker.job("receipt-revoke")["state"] == expected, env.broker.job("receipt-revoke")
    assert env.store.firmware_current("fixture") == previous
    if phase != "commit":
        assert env.driver.writes == before
    else:
        assert guard_calls == 2
        assert env.store.job("fetch-one")["diagnostic"]["source"]["source_receipt"] == env.receipt
    assert env.driver.closed and not env.broker._execution_intents


@pytest.mark.parametrize("kind", ["artifact_bundle", "full_backup"])
async def test_another_principal_restores_the_device_lkg(env, kind):
    """LKG is a fact about the device; flash authority, not who installed it, gates use."""
    broker, store = env.broker, env.store
    if kind == "full_backup":
        # Only a restore from alice's own full backup records a full_backup LKG.
        restored = env.prepare("alice-restore", Action.RESTORE, backup_job_id="backup-one")
        env.execute(restored)
        await broker.queue.join()
        assert broker.job("alice-restore")["state"] == "succeeded", broker.job("alice-restore")
    broker.release(LeaseRequest(session=broker.session.capability))
    session = broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="bob")["session"]
    row = store.db.execute(
        "SELECT receipt,principal FROM firmware_lkg_history WHERE device_id=? AND kind=?",
        ("fixture", kind),
    ).fetchone()
    assert row is not None and row[1] == "alice"
    before = env.driver.writes
    prepared = broker.prepare(
        PrepareRequest(
            session=session,
            job_id="bob-restore",
            test=Action.RESTORE,
            device_id="fixture",
            lkg_receipt=row[0],
        )
    )
    assert prepared["plan"]["flash_principal"] == "bob"
    assert prepared["plan"]["lkg_receipt"] == row[0]
    broker.execute(ExecuteRequest(session=session, plan_hash=prepared["plan_hash"]))
    await broker.queue.join()
    assert broker.job("bob-restore")["state"] == "succeeded", broker.job("bob-restore")
    assert env.driver.writes > before
    current = store.firmware_current("fixture")
    assert current["receipt"] == row[0] and current["principal"] == "bob"
    # The other principal's backup bytes stay owned; only their own job can name them.
    with pytest.raises(BrokerError):
        broker.prepare(
            PrepareRequest(
                session=session,
                job_id="bob-backup-reference",
                test=Action.RESTORE,
                device_id="fixture",
                backup_job_id="backup-one",
            )
        )
