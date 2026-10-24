"""A firmware_flash that names no recovery point (decision 2026-10-25); never USB.

Without ``backup_job_id`` and ``lkg_receipt`` a flash needs no LKG or backup:
no recovery-bytes check, no pre-write fence and no previous-layout check. The
device identity is bound from the probe just before the write. Everything else
(security probe, write, full readback, boot ELF/IDF, confirmed close, final
authority and the atomic LKG commit) is unchanged. Lowest layer first.
"""

import hashlib
import json
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_flash_failure_characterization as characterization
import test_flash_policy
from test_flash_lifecycle import FakeDriver

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.firmware_flash import (
    FLASH_EFFECTS,
    FLASH_EFFECTS_NO_RECOVERY_POINT,
    FLASH_STAGES,
    FirmwareFlash,
    operation_from_plan,
    validate_flash_plan,
)
from cardputer_mcp.flash_driver import NativeFlashDriver
from cardputer_mcp.flash_lifecycle import FlashFailure, FlashLifecycle, OperationPlan
from cardputer_mcp.flash_lkg_store import FirmwareSnapshots
from cardputer_mcp.flash_manifest import FlashBundle, FlashSegment

env = test_flash_policy.env

OFFSET = 4096
IMAGE = b"new-image" * 20
BUNDLE = FlashBundle(
    "b" * 64,
    "c" * 64,
    (FlashSegment(OFFSET, "app.bin", len(IMAGE), hashlib.sha256(IMAGE).hexdigest()),),
    "dio",
    "8MB",
    "80m",
)
SUMMARY_FIELDS = {
    "recovery_kind": "none",
    "prewrite_fence": "not_applied_no_recovery_point",
    "data_compatibility": "not_checked",
    "device_identity_basis": "probed_before_write",
}


def _plan(**changes):
    plan = OperationPlan(
        "flash", "job", "owner", "d" * 32, "e" * 32, "f" * 64, 8192, None, None, "b" * 64,
        "c" * 64, "none",
    )  # fmt: skip
    return replace(plan, **changes)


class _Counting(FakeDriver):
    def __init__(self):
        super().__init__()
        self.reads: list[tuple[int, int]] = []
        self.calls: list[str] = []
        self.callback = self.calls.append

    def read(self, offset, size):
        self.reads.append((offset, size))
        return super().read(offset, size)


def _run(driver, plan=None, **kwargs):
    plan = plan or _plan()
    stages: list[str] = []
    engine = FlashLifecycle(driver)
    arguments = (
        dict(
            live=lambda: True,
            reader_closed=lambda: True,
            stage=stages.append,
            bundle=BUNDLE,
            images=(IMAGE,),
            defer_commit=True,
        )
        | kwargs
    )
    return engine, stages, lambda: engine.run(plan, plan.digest, **arguments)


# ---------------------------------------------------------------------------
# Lifecycle layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("device_content", ["blank", "unknown_firmware"])
def test_lifecycle_flashes_without_fence_and_binds_the_probed_device(device_content):
    driver = _Counting()
    if device_content == "unknown_firmware":
        driver.data[OFFSET : OFFSET + 64] = b"\x5a" * 64
    engine, stages, run = _run(driver)
    receipt, data, summary = run()
    assert receipt is None and data is None
    assert driver.writes == 1
    # Only the full readback of the written segment; no pre-write comparison read.
    assert driver.reads == [(OFFSET, len(IMAGE))]
    assert "flash_backup_prewrite_check" not in stages
    assert stages == [
        "flash_security_probe",
        "flash_write",
        "flash_readback_verify",
        "flash_boot_observe",
    ]
    assert bytes(driver.data[OFFSET : OFFSET + len(IMAGE)]) == IMAGE
    assert summary["readback_verified"] and summary["passive_boot_verified"]
    assert {key: summary[key] for key in SUMMARY_FIELDS} == SUMMARY_FIELDS
    assert summary["bound_device_sha256"] == driver.security.device_sha256
    assert driver.closed and not engine.active and not engine.uncertain
    # defer_commit: only the atomic store commit marks it committed.
    assert engine._committed is None


@pytest.mark.parametrize(
    "case",
    [
        "with_backup",
        "with_recovery",
        "expected_device",
        "backup_digest",
        "restore",
    ],
)
def test_lifecycle_no_recovery_point_plan_names_no_recovery_input(case):
    driver = _Counting()
    plan, kwargs = _plan(), {}
    if case == "with_backup":
        kwargs["backup"] = bytes(driver.data)
    elif case == "with_recovery":
        kwargs["recovery"] = (BUNDLE, (IMAGE,))
    elif case == "expected_device":
        plan = _plan(expected_device_sha256="a" * 64)
    elif case == "backup_digest":
        plan = _plan(backup_sha256="1" * 64)
    else:
        plan = _plan(operation="restore", manifest_sha256=None)
    engine, _, run = _run(driver, plan, **kwargs)
    with pytest.raises(FlashFailure, match="^flash_plan_or_handoff_required$"):
        run()
    assert driver.calls == [] and driver.writes == 0 and not engine.active


@pytest.mark.parametrize(
    "identity",
    [
        {"device_sha256": ""},
        {"device_sha256": "A" * 64},
        {"device_sha256": "a" * 63},
        {"chip": "esp32"},
        {"secure_boot": None},
        {"flash_encryption": True},
    ],
)
def test_lifecycle_never_writes_when_the_probe_cannot_identify_the_device(identity):
    driver = _Counting()
    driver.security = replace(driver.security, **identity)
    _, _, run = _run(driver)
    with pytest.raises(FlashFailure, match="^device_security_or_identity_not_verified$"):
        run()
    assert driver.writes == 0 and driver.reads == [] and driver.closed


@pytest.mark.parametrize(
    "case,code",
    [
        ("readback", "flash_readback_mismatch_needs_review"),
        ("boot", "flash_boot_unverified_needs_review"),
        ("authority", "flash_authority_changed_needs_review"),
        ("cancel", "flash_cancelled_needs_review"),
    ],
)
def test_lifecycle_post_write_failure_needs_review_without_rollback(case, code):
    driver = _Counting()
    live = {"ok": True}
    engine, stages, run = _run(driver, live=lambda: live["ok"])
    original = driver.write

    def write(segments):
        original(segments)
        if case == "readback":
            driver.data[OFFSET] ^= 0xFF
        elif case == "authority":
            live["ok"] = False
        elif case == "cancel":
            engine.request_stop("job")

    driver.write = write
    if case == "boot":
        driver.summary = dict(driver.summary, elf_sha256_prefix="0" * 9)
    with pytest.raises(FlashFailure) as raised:
        run()
    assert raised.value.code == code
    assert driver.writes == 1  # no retry, no rollback write
    assert driver.closed and not engine.active and not engine.uncertain
    assert engine._committed is None


# ---------------------------------------------------------------------------
# Native driver: the probed identity is the only accepted device
# ---------------------------------------------------------------------------


class _NativeHarness:
    """NativeFlashDriver with a fake owned worker process; no port or esptool."""

    def __init__(self, root: Path):
        self.attached = "a" * 64  # device actually answering on the port
        self.flash = bytearray(b"x" * 8192)
        self.payloads: list[dict] = []
        self.usb = "f" * 64
        registration = SimpleNamespace(
            flash_bytes=8192,
            esptool_directory=root / "tools",
            python_tool=SimpleNamespace(executable=root / "python.exe", version="Python 3.13.16"),
        )
        self.backend = SimpleNamespace(
            active=False,
            hardware_connected=False,
            prepare=lambda test: {"device_fingerprint": self.usb},
        )
        self.driver = NativeFlashDriver(
            registration, self.backend, "f" * 64, root / "work", None, "c" * 64, lambda: True
        )
        self.driver._verified_port = lambda: "COM1"
        self.driver.process = SimpleNamespace(
            active=False, uncertain=False, stop_requested=threading.Event(), run=self.run
        )

    def run(self, args, env, cwd, emit, *, timeout, monitor):
        payload = json.loads((cwd / "job.json").read_text())
        self.payloads.append(payload)
        expected = payload["expected_device_sha256"]
        # The real worker (flash_worker.py) refuses a MAC identity that differs
        # from expected_device_sha256 on every action; write requires one.
        if (expected is not None and self.attached != expected) or (
            payload["action"] == "write" and expected is None
        ):
            emit('FLASH_RESULT={"ok":false,"error":"flash_device_changed"}', "stdout")
            return 1
        if payload["action"] == "probe":
            result = {
                "ok": True,
                "device_sha256": self.attached,
                "flash_bytes": 8192,
                "chip": "esp32s3",
                "secure_boot": False,
                "flash_encryption": False,
                "secure_download": False,
            }
        elif payload["action"] == "write":
            for record in payload["segments"]:
                data = (cwd / record["name"]).read_bytes()
                self.flash[record["offset"] : record["offset"] + len(data)] = data
            result = {"ok": True}
        else:
            data = bytes(self.flash[payload["offset"] : payload["offset"] + payload["size"]])
            (cwd / "read.bin").write_bytes(data)
            result = {"ok": True, "sha256": hashlib.sha256(data).hexdigest()}
        emit("FLASH_RESULT=" + json.dumps(result), "stdout")
        return 0


@pytest.mark.parametrize("swap", ["readback", "boot_reconnect"])
def test_native_driver_refuses_a_different_device_after_the_write(swap):
    with tempfile.TemporaryDirectory() as temporary:
        harness = _NativeHarness(Path(temporary))
        original_run = harness.run

        def run(args, env, cwd, emit, *, timeout, monitor):
            code = original_run(args, env, cwd, emit, timeout=timeout, monitor=monitor)
            if harness.payloads[-1]["action"] == "write":
                # The write's ROM reset; a different device answers afterwards.
                if swap == "readback":
                    harness.attached = "9" * 64
                else:
                    harness.usb = "0" * 64
            return code

        harness.driver.process.run = run
        _, _, go = _run(harness.driver, _plan(flash_bytes=8192))
        with pytest.raises(FlashFailure) as raised:
            go()
        actions = [payload["action"] for payload in harness.payloads]
        assert actions.count("write") == 1
        # Every call after the probe carries the identity the probe bound.
        assert [p["expected_device_sha256"] for p in harness.payloads] == [None] + ["a" * 64] * (
            len(harness.payloads) - 1
        )
        if swap == "readback":
            assert raised.value.code == "flash_native_failed_needs_review"
            assert actions == ["probe", "write", "read"]
        else:
            assert raised.value.code == "flash_boot_device_changed"
            assert actions == ["probe", "write", "read"]


# ---------------------------------------------------------------------------
# Plan variant
# ---------------------------------------------------------------------------

NO_RECOVERY_EFFECTS_SHA256 = "64f939fa798ba440574b7fe1b80530aa7aea552d1c711400ab7d29e1f6d05eda"
SAMPLE_NONE_OPERATION_DIGEST = "78b4520b4c4091ee47a5946fce514d7015dcdfcf93aed373d7d9ddee6dbfce36"
SAMPLE_NONE_PLAN_HASH = "e4d796b7b6c98ef8b5f9127d2acb7f96af773b5da6f0e3b4ccd07deb60dae6c1"


def _stored_plan():
    op = OperationPlan(
        "flash", "flash-job", "alice", "0" * 32, "1" * 32, "2" * 64, 4194304, None, None,
        "5" * 64, "6" * 64, "none",
    )  # fmt: skip
    return op, {
        "version": 1,
        "broker_epoch": op.broker_epoch,
        "session_id": op.session_id,
        "device": "mock-cardputer",
        "backend": "mock",
        "job_id": op.job_id,
        "test": "firmware_flash",
        "stages": list(FLASH_STAGES),
        "manifest_export_sha256": "7" * 64,
        "flash_operation": "flash",
        "lkg_receipt": None,
        "recovery_kind": "none",
        "device_id": "fixture",
        "build_job_id": "build-one",
        "backup_job_id": None,
        "flash_principal": op.principal,
        "flash_bytes": op.flash_bytes,
        "device_fingerprint": op.device_fingerprint,
        "expected_device_sha256": None,
        "backup_sha256": None,
        "manifest_sha256": op.manifest_sha256,
        "expected_elf_sha256": op.expected_elf_sha256,
        "expected_app_bin_sha256": "8" * 64,
        "registration_sha256": "9" * 64,
        "fingerprint": op.digest,
        "effects": list(FLASH_EFFECTS_NO_RECOVERY_POINT),
        "argv_template": ["registered-python", "fixed-flash-worker", "broker-owned-operation"],
        "expected_idf": "6.0.1",
        "target": "esp32s3",
    }


def test_no_recovery_point_plan_text_and_digests_are_pinned():
    op, plan = _stored_plan()
    compact = json.dumps(FLASH_EFFECTS_NO_RECOVERY_POINT, separators=(",", ":")).encode()
    assert hashlib.sha256(compact).hexdigest() == NO_RECOVERY_EFFECTS_SHA256
    assert FLASH_EFFECTS_NO_RECOVERY_POINT != FLASH_EFFECTS
    validate_flash_plan(plan)
    assert operation_from_plan(plan) == op
    assert op.digest == SAMPLE_NONE_OPERATION_DIGEST
    assert digest_plan(plan) == SAMPLE_NONE_PLAN_HASH


@pytest.mark.parametrize(
    "change",
    [
        {"lkg_receipt": "4" * 64},
        {"backup_job_id": "backup-one"},
        {"backup_sha256": "4" * 64},
        {"expected_device_sha256": "3" * 64},
        {"effects": list(FLASH_EFFECTS)},
        {"test": "firmware_restore", "flash_operation": "restore"},
        "no_manifest_export",
    ],
)
def test_no_recovery_point_is_only_a_new_flash_plan_without_recovery_inputs(change):
    _, plan = _stored_plan()
    if change == "no_manifest_export":
        del plan["manifest_export_sha256"]
    else:
        plan.update(change)
    with pytest.raises(ValueError, match="^invalid_flash_plan$"):
        validate_flash_plan(plan)


@pytest.mark.parametrize("kind", ["artifact_bundle", "full_backup"])
@pytest.mark.parametrize("export", [True, False])
def test_stored_recovery_point_plans_still_validate(kind, export):
    _, plan = _stored_plan()
    op = replace(operation_from_plan(plan), expected_device_sha256="3" * 64)
    op = replace(op, backup_sha256="4" * 64, recovery_kind=kind)
    plan.update(
        recovery_kind=kind,
        lkg_receipt="4" * 64,
        expected_device_sha256="3" * 64,
        backup_sha256="4" * 64,
        effects=list(FLASH_EFFECTS),
        fingerprint=op.digest,
    )
    if not export:
        del plan["manifest_export_sha256"]
    validate_flash_plan(plan)
    plan["effects"] = list(FLASH_EFFECTS_NO_RECOVERY_POINT)
    with pytest.raises(ValueError, match="^invalid_flash_plan$"):
        validate_flash_plan(plan)


# ---------------------------------------------------------------------------
# Job layer (real broker + store, fake driver)
# ---------------------------------------------------------------------------


def _no_lookup(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a flash without a recovery point looked up a recovery point")

    monkeypatch.setattr(FirmwareFlash, "_known", refuse)
    monkeypatch.setattr(FirmwareFlash, "_backup", refuse)


def _lkg_root(env):
    return env.broker.settings.flash_devices["fixture"].backup_directory / "lkg"


@pytest.mark.parametrize("state", ["no_lkg_no_backup", "device_differs_from_lkg"])
async def test_flash_without_recovery_point_commits_the_build_as_lkg(env, state, monkeypatch):
    db = env.store.db
    if state == "no_lkg_no_backup":
        db.execute("DELETE FROM firmware_lkg")
        db.execute("DELETE FROM firmware_lkg_history")
        db.commit()
        directory = env.broker.settings.flash_devices["fixture"].backup_directory
        for path in (directory / "backup-one.bin", directory / "backup-one.json"):
            path.unlink()
    else:
        # The real case: an earlier write did not commit, so the device content no
        # longer matches the LKG. With the LKG named, the fence would refuse.
        metadata, _ = FirmwareSnapshots(_lkg_root(env), windows_private_acl_confirmed=True).load(
            env.selected
        )
        app = metadata["bundle"]["segments"][-1]
        env.driver.data[app["offset"] + 3] ^= 1
    _no_lookup(monkeypatch)
    before = characterization._durable(env)
    reads = []
    original_read = env.driver.read

    def read(offset, size):
        reads.append((offset, size))
        return original_read(offset, size)

    env.driver.read = read
    prepared = env.prepare("no-baseline", build_job_id="build-one")
    plan = prepared["plan"]
    assert plan["recovery_kind"] == "none"
    assert (plan["lkg_receipt"], plan["backup_sha256"], plan["backup_job_id"]) == (None,) * 3
    assert plan["expected_device_sha256"] is None
    assert plan["effects"] == FLASH_EFFECTS_NO_RECOVERY_POINT
    env.execute(prepared)
    await env.broker.queue.join()
    assert env.broker.job("no-baseline")["state"] == "succeeded", env.broker.job("no-baseline")
    after = characterization._durable(env)
    assert after["writes"] == before["writes"] + 1
    assert after["seq"] == before["seq"] + 1
    # Only the full readback of the three written segments (bootloader, table,
    # app); no pre-write comparison read of the current device bytes.
    assert len(reads) == 3
    current = env.store.firmware_current("fixture")
    assert current["completed_job"] == "no-baseline"
    assert current["kind"] == "artifact_bundle"
    assert current["baseline"] == "no-baseline"
    assert current["principal"] == "alice"
    metadata, _ = FirmwareSnapshots(_lkg_root(env), windows_private_acl_confirmed=True).load(
        current["receipt"]
    )
    assert metadata["expected_device_sha256"] == env.driver.security.device_sha256
    summary = env.store.firmware_summary("no-baseline", "alice")
    assert {key: summary[key] for key in SUMMARY_FIELDS} == SUMMARY_FIELDS
    assert summary["bound_device_sha256"] == env.driver.security.device_sha256
    assert summary["last_known_good_receipt"] == current["receipt"]
    assert env.driver.closed and not env.broker.firmware_flash.active


@pytest.mark.parametrize(
    "case,code",
    [
        ("readback", "flash_readback_mismatch_needs_review"),
        ("boot", "flash_boot_unverified_needs_review"),
        ("authority", "flash_authority_changed_needs_review"),
        ("cancel", "flash_cancelled_needs_review"),
    ],
)
async def test_flash_without_recovery_point_failure_keeps_prior_target(env, case, code):
    before = characterization._durable(env)
    original = env.driver.write

    def write(segments):
        original(segments)
        if case == "readback":
            offset, _ = segments[-1]
            env.driver.data[offset] ^= 0xFF
        elif case == "authority":
            env.broker.principal_valid = lambda *args: False
        elif case == "cancel":
            env.broker.firmware_flash.request_stop("failing")

    env.driver.write = write
    if case == "boot":
        env.driver.summary["elf_sha256_prefix"] = "0" * 16
    prepared = env.prepare("failing", build_job_id="build-one")
    assert prepared["plan"]["recovery_kind"] == "none"
    env.execute(prepared)
    await env.broker.queue.join()
    record = env.broker.job("failing")
    assert record["state"] == "needs_review", record
    if case in ("readback", "boot"):
        characterization._assert_terminal(env, "failing", code, before, 1)
    else:
        assert record["result"] in (code, "flash_cancelled_or_authority_changed_needs_review")
        after = characterization._durable(env)
        assert after["current"] == before["current"]
        assert after["history"] == before["history"]
        assert after["seq"] == before["seq"]
        assert after["writes"] == before["writes"] + 1
        assert env.store.firmware_summary("failing", "alice") is None
    assert "failing" not in env.broker.firmware_flash.promotions


@pytest.mark.parametrize("named", ["lkg_receipt", "backup_job_id"])
async def test_an_explicitly_named_recovery_point_keeps_the_prewrite_fence(env, named):
    metadata, _ = FirmwareSnapshots(_lkg_root(env), windows_private_acl_confirmed=True).load(
        env.selected
    )
    app = metadata["bundle"]["segments"][-1]
    env.driver.data[app["offset"] + 3] ^= 1
    before = characterization._durable(env)
    reference = (
        {"lkg_receipt": env.selected} if named == "lkg_receipt" else {"backup_job_id": "backup-one"}
    )
    prepared = env.prepare("fenced", build_job_id="build-one", **reference)
    assert prepared["plan"]["recovery_kind"] in ("artifact_bundle", "full_backup")
    assert prepared["plan"]["effects"] == FLASH_EFFECTS
    env.execute(prepared)
    await env.broker.queue.join()
    code = (
        "lkg_current_firmware_changed_needs_review"
        if named == "lkg_receipt"
        else "flash_backup_stale_needs_review"
    )
    # The fence refuses before any write with the close confirmed, so the job is
    # `failed`, not needs_review (running-job lease and review classification fix).
    characterization._assert_terminal(env, "fenced", code, before, 0, "failed")
