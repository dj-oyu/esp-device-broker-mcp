"""Stage 0.5 characterization: untested flash failure paths, synthetic ROM only.

Pins CURRENT behaviour before the internal redesign. Lifecycle cases use the
lowest owner (FlashLifecycle + fake driver). Job-level cases reuse the broker
fixture from test_flash_policy (real broker/store, fake driver, never USB) and
assert the terminal state/reason, that the prior LKG target, LKG history and
``device_writes.seq`` are unchanged, and that no retry/rollback write happened.
"""

import hashlib
import pathlib
import re

import pytest
import test_flash_policy
from test_flash_lifecycle import FakeDriver

from cardputer_mcp.flash_lifecycle import FlashFailure, FlashLifecycle, OperationPlan
from cardputer_mcp.flash_lkg_store import FirmwareSnapshots
from cardputer_mcp.flash_manifest import FlashBundle, FlashSegment
from cardputer_mcp.models import TestName as Action

# Reuse the real-broker/fake-driver fixture (prior backup + flashed LKG).
env = test_flash_policy.env

# ---------------------------------------------------------------------------
# Lifecycle layer
# ---------------------------------------------------------------------------

OFFSET = 4096


def _bundle(image: bytes, *, size: int | None = None, sha: str | None = None) -> FlashBundle:
    segment = FlashSegment(
        OFFSET,
        "app.bin",
        len(image) if size is None else size,
        hashlib.sha256(image).hexdigest() if sha is None else sha,
    )
    return FlashBundle("b" * 64, "c" * 64, (segment,), "dio", "8MB", "80m")


class _Counting(FakeDriver):
    def __init__(self):
        super().__init__()
        self.reads: list[tuple[int, int]] = []

    def read(self, offset, size):
        self.reads.append((offset, size))
        return super().read(offset, size)


def _lifecycle_case(case):
    driver = _Counting()
    engine = FlashLifecycle(driver)
    prior_image = bytes(driver.data[OFFSET : OFFSET + 160])
    new_image = b"new-image" * 20
    new_bundle = _bundle(new_image)
    prior_bundle = _bundle(prior_image)
    operation = "restore" if case.startswith("restore") else "flash"
    plan = OperationPlan(
        operation,
        "job",
        "owner",
        "d" * 32,
        "e" * 32,
        "f" * 64,
        len(driver.data),
        "a" * 64,
        "1" * 64,
        "b" * 64 if operation == "flash" else None,
        "c" * 64,
        "artifact_bundle",
    )
    recovery_images: tuple[bytes, ...] = (prior_image,)
    if case.endswith("lkg_sha_changed"):
        recovery_images = (b"!" + prior_image[1:],)
    elif case.endswith("lkg_size_changed"):
        recovery_images = (prior_image + b"!",)
    elif case.endswith("lkg_count_changed"):
        recovery_images = (prior_image, prior_image)
    elif case == "flash_lkg_prewrite_mismatch":
        driver.data[OFFSET + 7] ^= 1
    elif case == "flash_readback_mismatch":
        original = driver.write

        def corrupting_write(segments):
            original(segments)
            driver.data[OFFSET] ^= 0xFF

        driver.write = corrupting_write
    stages: list[str] = []
    kwargs = dict(
        live=lambda: True,
        reader_closed=lambda: True,
        stage=stages.append,
        recovery=(prior_bundle, recovery_images),
        defer_commit=True,
    )
    if operation == "flash":
        kwargs.update(bundle=new_bundle, images=(new_image,))
    else:
        kwargs.update(recovery_table_offset=OFFSET)
    return driver, engine, plan, kwargs, stages, new_image


# case -> (code, writes, last stage reached)
LIFECYCLE_CASES = {
    "flash_lkg_sha_changed": ("lkg_bytes_changed", 0, "flash_security_probe"),
    "flash_lkg_size_changed": ("lkg_bytes_changed", 0, "flash_security_probe"),
    "flash_lkg_count_changed": ("lkg_bytes_changed", 0, "flash_security_probe"),
    "restore_lkg_sha_changed": ("lkg_bytes_changed", 0, "flash_security_probe"),
    "flash_lkg_prewrite_mismatch": (
        "lkg_current_firmware_changed_needs_review",
        0,
        "flash_backup_prewrite_check",
    ),
    "flash_readback_mismatch": (
        "flash_readback_mismatch_needs_review",
        1,
        "flash_readback_verify",
    ),
}


@pytest.mark.parametrize("case", sorted(LIFECYCLE_CASES))
def test_lifecycle_failure_is_terminal_without_retry_or_rollback(case):
    code, writes, last_stage = LIFECYCLE_CASES[case]
    driver, engine, plan, kwargs, stages, new_image = _lifecycle_case(case)
    with pytest.raises(FlashFailure) as raised:
        engine.run(plan, plan.digest, **kwargs)
    assert raised.value.code == code
    assert str(raised.value) == code
    assert raised.value.diagnostic is None
    assert driver.writes == writes
    assert stages[-1] == last_stage
    assert "flash_boot_observe" not in stages
    # Confirmed close; the engine is reusable and never marked committed.
    assert driver.closed and not engine.active and not engine.uncertain
    assert engine._committed is None
    if case == "flash_readback_mismatch":
        # The (corrupted) new bytes stay installed: no rollback/second write.
        assert bytes(driver.data[OFFSET + 1 : OFFSET + len(new_image)]) == new_image[1:]
        # Exactly one readback of the written segment; the read is not retried.
        assert driver.reads == [(OFFSET, 160), (OFFSET, len(new_image))]
    elif case == "flash_lkg_prewrite_mismatch":
        assert driver.reads == [(OFFSET, 160)]
    else:
        assert driver.reads == []


def test_lkg_prewrite_fence_stops_at_first_changed_segment():
    driver = _Counting()
    engine = FlashLifecycle(driver)
    first = bytes(driver.data[0:4096])
    second = bytes(driver.data[4096:8192])
    driver.data[0] ^= 1
    prior = FlashBundle(
        "b" * 64,
        "c" * 64,
        (
            FlashSegment(0, "boot.bin", 4096, hashlib.sha256(first).hexdigest()),
            FlashSegment(4096, "app.bin", 4096, hashlib.sha256(second).hexdigest()),
        ),
        "dio",
        "8MB",
        "80m",
    )
    image = b"n" * 64
    plan = OperationPlan(
        "flash", "j", "o", "d" * 32, "e" * 32, "f" * 64, 8192, "a" * 64, "1" * 64, "b" * 64,
        "c" * 64, "artifact_bundle",
    )  # fmt: skip
    with pytest.raises(FlashFailure, match="^lkg_current_firmware_changed_needs_review$"):
        engine.run(
            plan,
            plan.digest,
            live=lambda: True,
            reader_closed=lambda: True,
            stage=lambda _: None,
            bundle=_bundle(image),
            images=(image,),
            recovery=(prior, (first, second)),
        )
    assert driver.reads == [(0, 4096)]
    assert driver.writes == 0


# ---------------------------------------------------------------------------
# Job layer (real broker + store, fake driver)
# ---------------------------------------------------------------------------


def _durable(env):
    db = env.store.db
    return dict(
        current=env.store.firmware_current("fixture"),
        history=[tuple(row) for row in db.execute("SELECT * FROM firmware_lkg_history ORDER BY 1")],
        seq=env.store.device_write_seq(),
        writes=env.driver.writes,
    )


def _lkg_root(env):
    return env.broker.settings.flash_devices["fixture"].backup_directory / "lkg"


def _assert_terminal(env, job, code, before, writes, state="needs_review"):
    record = env.broker.job(job)
    assert (record["state"], record["result"]) == (state, code), record
    after = _durable(env)
    assert after["current"] == before["current"]
    assert after["history"] == before["history"]
    assert after["seq"] == before["seq"]
    assert after["writes"] - before["writes"] == writes
    assert env.store.firmware_summary(job, "alice") is None
    assert not list(_lkg_root(env).glob("*.pending"))
    assert not env.broker.firmware_flash.active and not env.broker.firmware_flash.uncertain
    assert env.broker.status()["execution_uncertain"] is False
    assert env.driver.closed


async def _flash(env, job, action=Action.FLASH, **references):
    if action == Action.FLASH and not references:
        references = {"build_job_id": "build-one"}
    prepared = env.prepare(job, action, **references)
    env.execute(prepared)
    await env.broker.queue.join()
    return prepared


async def test_control_successful_flash_bumps_seq_once_and_selects_target(env):
    before = _durable(env)
    await _flash(env, "control")
    assert env.broker.job("control")["state"] == "succeeded"
    after = _durable(env)
    assert after["seq"] == before["seq"] + 1
    assert after["writes"] == before["writes"] + 1
    assert after["current"]["completed_job"] == "control"
    # Same build bytes: INSERT OR IGNORE keeps the first completed_job in history.
    assert after["history"] == before["history"]


async def test_readback_mismatch_needs_review_and_keeps_prior_lkg(env):
    before = _durable(env)
    original = env.driver.write

    def corrupting_write(segments):
        original(segments)
        offset, data = segments[-1]
        env.driver.data[offset] ^= 0xFF

    env.driver.write = corrupting_write
    await _flash(env, "readback")
    _assert_terminal(env, "readback", "flash_readback_mismatch_needs_review", before, 1)


async def test_lkg_prewrite_current_firmware_mismatch_never_writes(env):
    metadata, _ = FirmwareSnapshots(_lkg_root(env), windows_private_acl_confirmed=True).load(
        env.selected
    )
    app = metadata["bundle"]["segments"][-1]
    env.driver.data[app["offset"] + 3] ^= 1
    before = _durable(env)
    # The fence applies only when the LKG is named explicitly (decision 2026-10-25);
    # with no baseline named, the same device content is flashed without a fence.
    await _flash(env, "prewrite", build_job_id="build-one", lkg_receipt=env.selected)
    # Review classification fix: the pre-write fence failed before any write and the
    # driver close was confirmed, so the job is `failed` (it was needs_review); the
    # result code is unchanged and the prior LKG stays selected.
    _assert_terminal(
        env, "prewrite", "lkg_current_firmware_changed_needs_review", before, 0, "failed"
    )
    assert not env.store.needs_review()


@pytest.mark.parametrize("how", ["cancelled", "authority_revoked"])
async def test_lkg_promotion_cancel_after_verified_write_keeps_prior_lkg(env, how, monkeypatch):
    original = FlashLifecycle.run

    def run_then_interrupt(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        # A race that lands after the verified write/boot/close, before staging.
        if how == "cancelled":
            self.cancelled.set()
        else:
            env.broker.principal_valid = lambda *args: False
        return result

    monkeypatch.setattr(FlashLifecycle, "run", run_then_interrupt)
    before = _durable(env)
    await _flash(env, "promotion")
    _assert_terminal(env, "promotion", "lkg_promotion_cancelled_needs_review", before, 1)
    assert "promotion" not in env.broker.firmware_flash.promotions


async def test_existing_cas_object_changed_during_staging_keeps_prior_lkg(env, monkeypatch):
    root = _lkg_root(env)
    _, files = FirmwareSnapshots(root, windows_private_acl_confirmed=True).load(env.selected)
    target = root / hashlib.sha256(files["app.elf"]).hexdigest()
    original = FlashLifecycle.run

    def run_then_tamper(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        target.write_bytes(b"tampered" + target.read_bytes())
        return result

    monkeypatch.setattr(FlashLifecycle, "run", run_then_tamper)
    before = _durable(env)
    await _flash(env, "cas-changed")
    # put() re-verifies an existing object via get(); `lkg_object_changed` is not reached.
    _assert_terminal(env, "cas-changed", "lkg_object_missing_or_changed", before, 1)


async def test_cas_object_conflict_at_publication_keeps_prior_lkg(env, monkeypatch):
    root = _lkg_root(env)
    real_exists = pathlib.Path.exists
    conflicts = []

    def exists(self, *args, **kwargs):
        # A competing object appears after our pending file was written.
        if (
            self.parent == root
            and re.fullmatch(r"[a-f0-9]{64}", self.name)
            and real_exists(self.with_name(self.name + ".pending"))
        ):
            conflicts.append(self.name)
            return True
        return real_exists(self, *args, **kwargs)

    before = _durable(env)
    # A full-backup restore stages a new envelope-only receipt (a fresh CAS object).
    prepared = env.prepare("conflict", Action.RESTORE, backup_job_id="backup-one")
    monkeypatch.setattr(pathlib.Path, "exists", exists)
    env.execute(prepared)
    await env.broker.queue.join()
    monkeypatch.undo()
    assert len(conflicts) == 1
    # `lkg_object_conflict` is raised inside put()'s try and re-mapped.
    _assert_terminal(env, "conflict", "lkg_persistence_failed_needs_review", before, 1)
    assert not (root / conflicts[0]).exists()


@pytest.mark.parametrize(
    "existing,code",
    [
        # Metadata sidecar write fails inside save()'s try block.
        ("json", "backup_persistence_failed_needs_review"),
        # The .bin O_EXCL open is outside the try: a raw FileExistsError reaches
        # the broker's generic handler.
        ("bin", "backend_error_no_retry"),
    ],
)
async def test_backup_persistence_failure_needs_review_without_write(env, existing, code):
    directory = env.broker.settings.flash_devices["fixture"].backup_directory
    (directory / f"backup-two.{existing}").write_bytes(b"pre-existing")
    before = _durable(env)
    await _flash(env, "backup-two", Action.BACKUP)
    _assert_terminal(env, "backup-two", code, before, 0)
    assert (directory / f"backup-two.{existing}").read_bytes() == b"pre-existing"
    with pytest.raises(FlashFailure):
        env.broker.firmware_flash._backup(
            env.broker.settings.flash_devices["fixture"], "backup-two", "alice"
        )
