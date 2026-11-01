"""Broker integration for separately requested backup/flash/restore operations."""

import asyncio
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .build_logs import BuildLogs
from .device_observation import DeviceObservation
from .flash_backup import BackupStore
from .flash_driver import (
    BootAttempt,
    NativeFlashDriver,
    boot_verification_reason,
    esptool_fingerprint,
)
from .flash_lifecycle import FlashFailure, FlashLifecycle, Operation, OperationPlan
from .flash_lkg_bundle import (
    baseline_layout,
    bundle_from_record,
    bundle_record,
    compatibility,
    same_layout,
)
from .flash_lkg_models import bound_lkg_metadata, lkg_compat
from .flash_lkg_store import FirmwareSnapshots
from .flash_manifest import _file, verify_bundle
from .job_logs import attach
from .manifest_export import make_manifest_export, manifest_export_sha256, validate_plan_export
from .models import TestName
from .offload_io import offload, pure_io
from .public_reasons import RECOVERY_POINT_REFUSAL_CODES
from .registration_digest import flash_digest

FLASH_ACTIONS: dict[str, Operation] = {
    "firmware_backup": "backup",
    "firmware_flash": "flash",
    "firmware_restore": "restore",
}
FLASH_STAGES = [
    "flash_security_probe",
    "flash_backup_or_verify",
    "flash_write_or_restore",
    "flash_readback_verify",
    "flash_boot_observe",
    "flash_close",
]
FLASH_EFFECTS = [
    "Successful flash/restore atomically selects verified recovery bytes with the successful job",
    "Retained LKG may replace full backup input; the exact receipt is frozen at prepare time",
    "Artifact restore targets firmware regions; full_backup restore also rewrites device data",
    (
        "Firmware may alter NVS/filesystem after boot; firmware recovery cannot promise "
        "data compatibility"
    ),
    (
        "Executes only the registered external Python/esptool 5.1.0 ROM worker; may "
        "reset the selected device"
    ),
    (
        "Backup reads full plaintext flash into a private local receipt; "
        "flash/restore require independent explicit operation"
    ),
    (
        "Writes only attested manifest regions or the exact device-bound backup; "
        "interrupted work remains diagnostic history; current uncertainty blocks execution"
    ),
    (
        "Flash/restore: one 100 ms normal-boot reset and up to 30 s passive IDF/ELF "
        "observation; at most one same-USB reopen after close, without another reset"
    ),
    "Backup: ROM probe/read resets may occur; no normal-boot reset or firmware write",
    (
        "ROM reads: fixed 60 s + 600 s/MiB hard deadline, min 300 s/max 9660 s; "
        "60 s without advancing progress stops; lease renewal remains explicit"
    ),
    (
        "Flash leaves the new firmware installed; "
        "restore is a separate explicit operation; no rollback"
    ),
    (
        "Shares bounded redacted progress/boot logs only with the requesting "
        "principal; no raw backup delivery"
    ),
]
# A firmware_flash that names neither backup_job_id nor lkg_receipt (decision of
# 2026-10-25): no recovery point is required or kept, so there is no pre-write
# fence. A separate constant keeps every stored FLASH_EFFECTS plan valid.
FLASH_EFFECTS_NO_RECOVERY_POINT = [
    (
        "Successful flash atomically selects the verified written bundle as the "
        "device LKG with the successful job"
    ),
    (
        "No recovery point is required or kept by this flash: no LKG or backup "
        "precondition and no pre-write comparison of current device bytes"
    ),
    (
        "Writes the attested bootloader, partition table and app; compatibility of "
        "partitions, NVS or filesystem data with the previous firmware is not checked"
    ),
    (
        "Recovery from a bad build is flashing a fixed build; restore stays a "
        "separate explicit operation"
    ),
    (
        "Executes only the registered external Python/esptool 5.1.0 ROM worker; may "
        "reset the selected device"
    ),
    (
        "Only the device identified by the pre-write probe is accepted for the write, "
        "readback and boot check; current uncertainty blocks execution"
    ),
    (
        "Flash: one 100 ms normal-boot reset and up to 30 s passive IDF/ELF "
        "observation; at most one same-USB reopen after close, without another reset"
    ),
    (
        "ROM reads: fixed 60 s + 600 s/MiB hard deadline, min 300 s/max 9660 s; "
        "60 s without advancing progress stops; lease renewal remains explicit"
    ),
    (
        "Flash leaves the new firmware installed; interrupted work remains "
        "diagnostic history; no rollback"
    ),
    "Shares bounded redacted progress/boot logs only with the requesting principal",
]
RECOVERY_KINDS = ("full_backup", "artifact_bundle", "none")
FLASH_FIELDS = {
    "flash_operation",
    "lkg_receipt",
    "recovery_kind",
    "device_id",
    "build_job_id",
    "backup_job_id",
    "flash_principal",
    "flash_bytes",
    "device_fingerprint",
    "expected_device_sha256",
    "backup_sha256",
    "manifest_sha256",
    "expected_elf_sha256",
    "expected_app_bin_sha256",
    "registration_sha256",
    "fingerprint",
    "effects",
    "argv_template",
    "expected_idf",
    "target",
}


def flash_readiness(registration: Any, has_lkg: bool) -> dict[str, Any]:
    """What a registered device allows; no hardware.

    A flash needs no LKG or backup: `flash_baseline` is "not_required" whenever
    flash is enabled. `lkg_recorded` says whether a verified LKG exists that a
    flash or restore may name explicitly with `lkg_receipt`.
    """
    operations = {
        name: getattr(registration, "allow_" + name) is True
        for name in ("backup", "flash", "restore")
    }
    return {
        "operations": operations,
        "flash_baseline": "not_required" if operations["flash"] else "flash_not_enabled",
        "lkg_recorded": has_lkg,
    }


def no_recovery_point_flash(plan: dict[str, Any]) -> bool:
    return bool(plan["flash_operation"] == "flash" and plan["recovery_kind"] == "none")


def operation_from_plan(plan: dict[str, Any]) -> OperationPlan:
    return OperationPlan(
        plan["flash_operation"],
        plan["job_id"],
        plan["flash_principal"],
        plan["broker_epoch"],
        plan["session_id"],
        plan["device_fingerprint"],
        plan["flash_bytes"],
        plan["expected_device_sha256"],
        plan["backup_sha256"],
        plan["manifest_sha256"],
        plan["expected_elf_sha256"],
        plan["recovery_kind"],
    )


def validate_flash_plan(plan: dict[str, Any]) -> None:
    base = {
        "version",
        "broker_epoch",
        "session_id",
        "device",
        "backend",
        "job_id",
        "test",
        "stages",
    }
    optional = {"manifest_export_sha256"} if "manifest_export_sha256" in plan else set()
    if set(plan) != base | FLASH_FIELDS | optional or plan.get("test") not in FLASH_ACTIONS:
        raise ValueError("invalid_flash_plan")
    if optional and (
        plan["test"] != TestName.FLASH.value
        or type(plan["manifest_export_sha256"]) is not str
        or re.fullmatch(r"[a-f0-9]{64}", plan["manifest_export_sha256"]) is None
    ):
        raise ValueError("invalid_flash_plan")
    # "none" exists only for new firmware_flash plans (which always carry
    # manifest_export_sha256) and names no recovery input or expected device.
    no_recovery_point = plan.get("recovery_kind") == "none"
    if no_recovery_point and (
        plan["test"] != TestName.FLASH.value
        or not optional
        or any(
            plan[key] is not None
            for key in ("lkg_receipt", "backup_job_id", "backup_sha256", "expected_device_sha256")
        )
    ):
        raise ValueError("invalid_flash_plan")
    if (
        plan["flash_operation"] != FLASH_ACTIONS[plan["test"]]
        or plan["recovery_kind"] not in RECOVERY_KINDS
        or plan["backend"] not in ("mock", "serial")
        or plan["device"]
        != ("mock-cardputer" if plan["backend"] == "mock" else "configured-cardputer")
        or plan["expected_idf"] != "6.0.1"
        or plan["target"] != "esp32s3"
        or type(plan["flash_bytes"]) is not int
        or plan["flash_bytes"] not in (2097152, 4194304, 8388608, 16777216)
        or not isinstance(plan["flash_principal"], str)
        or re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", plan["flash_principal"]) is None
        or plan["effects"]
        != (FLASH_EFFECTS_NO_RECOVERY_POINT if no_recovery_point else FLASH_EFFECTS)
        or plan["stages"] != FLASH_STAGES
        or plan["argv_template"]
        != ["registered-python", "fixed-flash-worker", "broker-owned-operation"]
    ):
        raise ValueError("invalid_flash_plan")
    for key in ("device_id", "job_id", "build_job_id", "backup_job_id"):
        value = plan[key]
        if (key in ("device_id", "job_id") or value is not None) and (
            not isinstance(value, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value) is None
        ):
            raise ValueError("invalid_flash_plan")
    for key in (
        "device_fingerprint",
        "registration_sha256",
        "fingerprint",
        "expected_device_sha256",
        "backup_sha256",
        "manifest_sha256",
        "expected_elf_sha256",
        "expected_app_bin_sha256",
        "lkg_receipt",
    ):
        value = plan[key]
        if (
            key in ("device_fingerprint", "registration_sha256", "fingerprint") or value is not None
        ) and (not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None):
            raise ValueError("invalid_flash_plan")
    if operation_from_plan(plan).digest != plan["fingerprint"]:
        raise ValueError("invalid_flash_plan")


def _load_backup(directory: Path, acl: Any, job_id: str, principal: str) -> Any:
    return BackupStore(directory, windows_private_acl_confirmed=acl).load(job_id, principal)


def _load_snapshot(directory: Path, acl: Any, receipt: str) -> Any:
    return FirmwareSnapshots(directory, windows_private_acl_confirmed=acl).load(receipt)


def _verify_bundle(root: Path, manifest: dict[str, Any], flash_bytes: int) -> Any:
    return verify_bundle(root, manifest, flash_bytes=flash_bytes)


class FirmwareFlash:
    def __init__(self, settings: Any, backend: Any, build: Any, store: Any) -> None:
        self.settings, self.backend, self.build, self.store = settings, backend, build, store
        self.driver_factory: Any = None  # Trusted test injection only, never configuration/MCP.
        self.resolver: Any = None
        # (recorded, current) owner check for build/backup inputs; the broker injects
        # its rotation-lineage aware check. Standalone default: exact principals.
        self.same_owner: Callable[[object, object], bool] = lambda recorded, current: (
            recorded == current
        )
        self.engine: FlashLifecycle | None = None
        self.job_id: str | None = None
        self.logs: dict[str, BuildLogs] = {}
        self.on_logs_change: Callable[[], None] | None = None
        # Durable per-job log hooks, set by the broker; None keeps RAM-only behaviour.
        self.open_job_log: Callable[[str, str, str], Any] | None = None
        self.job_logs: Any = None
        # Opened on the broker loop: the store's SQLite connection is loop-bound.
        self._preopened_logs: dict[str, Any] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.promotions: dict[str, dict[str, str]] = {}
        # Stage names with UTC times per job, for the durable flash_outcome record.
        self.phases: dict[str, list[tuple[str, str]]] = {}
        self._cancelled_jobs: set[str] = set()
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        return self.engine is not None and self.engine.active

    @property
    def uncertain(self) -> bool:
        return self.engine is not None and self.engine.uncertain

    def failure_evidence(self, job_id: str) -> tuple[bool, bool]:
        """`(write_possibly_started, close_confirmed)` for a failed run of `job_id`.

        Read by the broker after `execute` raised, to classify the outcome
        (device_failure_outcome.py). If no lifecycle was created for this job, the
        failure came from the pre-driver checks: no driver call was made, and the
        close is confirmed only when no earlier engine is still active or uncertain.
        """
        with self._lock:
            engine = self.engine if self.job_id == job_id else None
        if engine is None:
            return False, not self.active and not self.uncertain
        driver = engine.driver
        try:
            closed = not (engine.active or engine.uncertain or driver.active or driver.uncertain)
        except Exception:
            closed = False
        return engine.write_started is not False, closed

    def request_stop(self, job_id: str | None = None) -> bool:
        with self._lock:
            if job_id is not None and self.job_id != job_id:
                self._cancelled_jobs.add(job_id)
                return True
            if self.engine is not None:
                return self.engine.request_stop(self.job_id or "")
            if job_id is not None:
                self._cancelled_jobs.add(job_id)
            return True

    def _registration(self, device_id: str, operation: str) -> Any:
        registration = self.settings.flash_devices.get(device_id)
        if registration is None:
            raise FlashFailure("registered_flash_device_required")
        if getattr(registration, "allow_" + operation) is not True:
            raise FlashFailure("registered_flash_operation_not_enabled")
        if self.driver_factory is None:
            if self.backend.name != "serial" or self.settings.serial is None:
                raise FlashFailure("flash_serial_opt_in_required")
            if registration.selector != self.settings.serial.selector:
                raise FlashFailure("flash_registration_selector_mismatch")
            from .firmware_build import hash_file

            # Pure hashing; offloaded from the loop when the caller uses offload_io.
            if (
                pure_io("hash_file", hash_file, registration.python_tool.executable)
                != registration.python_tool.sha256
                or pure_io(
                    "esptool_fingerprint", esptool_fingerprint, registration.esptool_directory
                )
                != registration.esptool_sha256
            ):
                raise FlashFailure("registered_flash_tools_changed")
        return registration

    def _backup(self, registration: Any, job_id: str, principal: str) -> Any:
        job = self.store().job(job_id)
        plan = self.store().plan(job["plan_hash"]) if job else None
        if (
            job is None
            or job["state"] != "succeeded"
            or plan is None
            or plan.get("test") != "firmware_backup"
            or not self.same_owner(plan.get("flash_principal"), principal)
        ):
            raise FlashFailure("successful_owned_backup_required")
        # The retained backup stays keyed by the principal that took it; a rotated
        # successor of that key reads it under the recorded owner.
        return pure_io(
            "backup_load",
            _load_backup,
            registration.backup_directory,
            registration.private_backup_acl_confirmed,
            job_id,
            plan["flash_principal"],
        )

    def _snapshots(self, registration: Any) -> FirmwareSnapshots:
        return FirmwareSnapshots(
            registration.backup_directory / "lkg",
            windows_private_acl_confirmed=registration.private_backup_acl_confirmed,
        )

    def _known(self, registration: Any, device_id: str, receipt: str | None) -> Any:
        """The device's verified firmware; any principal with flash authority may build on it."""
        row = (
            self.store().firmware_known(device_id, receipt)
            if receipt
            else self.store().firmware_current(device_id)
        )
        if row is None:
            raise FlashFailure("verified_lkg_or_initial_backup_required")
        metadata, files = pure_io(
            "lkg_load",
            _load_snapshot,
            registration.backup_directory / "lkg",
            registration.private_backup_acl_confirmed,
            row["receipt"],
        )
        selector = hashlib.sha256(registration.selector.model_dump_json().encode()).hexdigest()
        binding = dict(
            device_id=device_id,
            principal=row["principal"],
            selector_sha256=selector,
            flash_bytes=registration.flash_bytes,
        )
        bound_lkg_metadata(metadata, binding, row["kind"])
        # Callers keep the parsed object itself; it is never re-dumped.
        return row, metadata, files

    def commit_success(self, plan: dict[str, Any], result: str, live: Any) -> None:
        def guard() -> bool:
            return (
                live() is True
                and self.engine is not None
                and not self.engine.cancelled.is_set()
                and not self.active
                and not self.uncertain
            )

        summary = dict(self.results.get(plan["job_id"], {}))
        if plan["job_id"] in self.promotions:
            summary["last_known_good_receipt"] = self.promotions[plan["job_id"]]["receipt"]
        self.store().finish_firmware(
            plan["job_id"],
            result,
            self.promotions.get(plan["job_id"]),
            guard,
            summary if plan["flash_operation"] != "backup" else None,
            plan["flash_principal"],
        )
        if self.engine is not None:
            self.engine._committed = plan["job_id"]
        if plan["job_id"] in self.promotions:
            self.results[plan["job_id"]]["last_known_good_receipt"] = self.promotions[
                plan["job_id"]
            ]["receipt"]

    def _build_context(self, job_id: str, principal: str) -> tuple[Path, dict[str, Any]]:
        job = self.store().job(job_id)
        plan = self.store().plan(job["plan_hash"]) if job else None
        if (
            job is None
            or job["state"] != "succeeded"
            or plan is None
            or plan.get("test") != TestName.BUILD.value
            or not self.same_owner(plan.get("build_principal"), principal)
        ):
            raise FlashFailure("successful_owned_build_required")
        resolver = self.resolver or getattr(self.build, "resolve_flash_manifest", None)
        if resolver is None:
            raise FlashFailure("build_manifest_resolver_unavailable")
        # Hashes every owned artifact; it uses its own read-only SQLite connection.
        # Resolved under the build's recorded principal (lineage checked above).
        root, manifest = pure_io(
            "build_manifest", resolver, job_id, plan["build_principal"], plan["project_id"]
        )
        if not isinstance(root, Path) or not isinstance(manifest, dict):
            raise FlashFailure("invalid_internal_build_manifest_context")
        return root, manifest

    def prepare(self, request: Any, principal: str, epoch: str, session_id: str) -> dict[str, Any]:
        try:
            return self._prepare(request, principal, epoch, session_id)
        except FlashFailure as failure:
            if failure.code in RECOVERY_POINT_REFUSAL_CODES:
                failure.public_details = self._recovery_point_details(failure.code, request)
            raise

    def _recovery_point_details(self, code: str, request: Any) -> dict[str, Any] | None:
        """The device's flash_baseline/lkg_recorded and one fixed next step; no ids."""
        registration = self.settings.flash_devices.get(request.device_id)
        if registration is None:
            return None
        readiness = flash_readiness(
            registration, self.store().firmware_current(request.device_id) is not None
        )
        backup_enabled = readiness["operations"]["backup"]
        if code == "verified_lkg_or_initial_backup_required" and (
            getattr(request, "lkg_receipt", None) is not None
        ):
            step = "choose_lkg_receipt_from_status_recovery"
        elif not backup_enabled:
            step = "backup_not_enabled_in_local_registration"
        elif code == "verified_lkg_or_initial_backup_required":
            step = "run_firmware_backup_then_pass_backup_job_id"
        else:
            step = "name_own_succeeded_backup_of_this_device"
        return {
            "flash_baseline": readiness["flash_baseline"],
            "lkg_recorded": readiness["lkg_recorded"],
            "next_step": step,
        }

    def _prepare(self, request: Any, principal: str, epoch: str, session_id: str) -> dict[str, Any]:
        operation = FLASH_ACTIONS[request.test.value]
        registration = self._registration(request.device_id, operation)
        if not principal:
            raise FlashFailure("flash_authenticated_principal_required")
        if self.driver_factory is None:
            usb = pure_io("usb_inventory", self.backend.prepare, TestName.RESTART)[
                "device_fingerprint"
            ]
        else:
            usb = hashlib.sha256(("mock:" + self.backend.device).encode()).hexdigest()
        expected_device = backup_digest = manifest_digest = elf_digest = image_digest = None
        export_digest = None
        lkg_receipt = None
        recovery_kind = "full_backup"
        known = None
        if operation != "backup":
            if request.backup_job_id is not None:
                receipt, _ = self._backup(registration, request.backup_job_id, principal)
                if (
                    receipt.device_fingerprint != usb
                    or receipt.flash_bytes != registration.flash_bytes
                ):
                    raise FlashFailure("backup_registered_device_mismatch")
                expected_device, backup_digest = receipt.device_sha256, receipt.sha256
            elif operation == "flash" and getattr(request, "lkg_receipt", None) is None:
                # No baseline named: no LKG/backup lookup and no pre-write fence
                # (decision 2026-10-25). Identity is bound at execution from the probe.
                recovery_kind = "none"
            else:
                row, metadata, files = self._known(
                    registration,
                    request.device_id,
                    getattr(request, "lkg_receipt", None),
                )
                known = metadata, files
                lkg_receipt, recovery_kind = row["receipt"], row["kind"]
                expected_device = metadata["expected_device_sha256"]
                backup_digest = (
                    metadata["backup_sha256"] if recovery_kind == "full_backup" else lkg_receipt
                )
            if operation == "flash":
                root, manifest = self._build_context(request.build_job_id, principal)
                build_job = self.store().job(request.build_job_id)
                build_plan = self.store().plan(build_job["plan_hash"])
                if build_plan["project_id"] != registration.project_id:
                    raise FlashFailure("flash_build_project_mismatch")
                bundle = pure_io(
                    "verify_bundle", _verify_bundle, root, manifest, registration.flash_bytes
                )
                manifest_digest = bundle.manifest_sha256
                export_digest = manifest_export_sha256(make_manifest_export(manifest))
                layout = json.loads(pure_io("file", _file, root, manifest["flash_layout"], 65536))
                app = layout.get("app", {}).get("file")
                if not isinstance(app, str) or app not in {item.name for item in bundle.segments}:
                    raise FlashFailure("flash_app_receipt_required")
                image_digest = next(item.sha256 for item in bundle.segments if item.name == app)
                elf = str(Path(app).with_suffix(".elf")).replace("\\", "/")
                receipt_elf = next((a for a in manifest["artifacts"] if a["name"] == elf), None)
                if receipt_elf is None:
                    raise FlashFailure("flash_app_elf_receipt_required")
                pure_io("file", _file, root, receipt_elf, 64 * 1024 * 1024)
                elf_digest = receipt_elf["sha256"]
            elif known is not None:
                metadata, _ = known
                image_digest = metadata["expected_app_bin_sha256"]
                elf_digest = metadata["expected_elf_sha256"]
            else:
                image_digest = (
                    self.backend.config.expected_firmware_sha256
                    if self.backend.name == "serial"
                    else "0" * 64
                )
                elf_digest = registration.original_elf_sha256
                if elf_digest is None or image_digest is None:
                    raise FlashFailure("restore_original_firmware_receipt_required")
        registration_digest = flash_digest(registration)
        op = OperationPlan(
            operation,
            request.job_id,
            principal,
            epoch,
            session_id,
            usb,
            registration.flash_bytes,
            expected_device,
            backup_digest,
            manifest_digest,
            elf_digest,
            recovery_kind,
        )
        return dict(
            **({"manifest_export_sha256": export_digest} if export_digest is not None else {}),
            flash_operation=operation,
            lkg_receipt=lkg_receipt,
            recovery_kind=recovery_kind,
            device_id=request.device_id,
            build_job_id=request.build_job_id,
            backup_job_id=request.backup_job_id,
            flash_principal=principal,
            flash_bytes=registration.flash_bytes,
            device_fingerprint=usb,
            expected_device_sha256=expected_device,
            backup_sha256=backup_digest,
            manifest_sha256=manifest_digest,
            expected_elf_sha256=elf_digest,
            expected_app_bin_sha256=image_digest,
            registration_sha256=registration_digest,
            fingerprint=op.digest,
            effects=FLASH_EFFECTS_NO_RECOVERY_POINT if recovery_kind == "none" else FLASH_EFFECTS,
            stages=FLASH_STAGES,
            expected_idf="6.0.1",
            target="esp32s3",
            argv_template=["registered-python", "fixed-flash-worker", "broker-owned-operation"],
        )

    def prepare_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        from types import SimpleNamespace

        validate_flash_plan(plan)
        request = SimpleNamespace(
            test=TestName(plan["test"]),
            job_id=plan["job_id"],
            device_id=plan["device_id"],
            build_job_id=plan["build_job_id"],
            backup_job_id=plan["backup_job_id"],
            lkg_receipt=plan["lkg_receipt"],
        )
        details = self.prepare(
            request, plan["flash_principal"], plan["broker_epoch"], plan["session_id"]
        )
        # Old immutable plans stay old: never supplement their committed payload.
        # A malformed PRESENT commitment was rejected above, without fallback.
        if "manifest_export_sha256" not in plan:
            details.pop("manifest_export_sha256", None)
        elif details.get("manifest_export_sha256") != plan["manifest_export_sha256"]:
            raise FlashFailure("flash_inputs_changed_needs_review")
        return details

    async def execute(self, plan: dict[str, Any], stage: Any, live: Any) -> str:
        loop = asyncio.get_running_loop()
        job_id = plan["job_id"]
        # The durable log opener reads the broker store, which only the loop may use;
        # the flash driver runs in a worker thread.
        self._preopened_logs[job_id] = (
            self.open_job_log(job_id, plan["flash_principal"], plan.get("test", "firmware_flash"))
            if self.open_job_log is not None
            else None
        )
        try:
            return await self._run(plan, stage, live, loop)
        finally:
            self._preopened_logs.pop(job_id, None)

    async def _run(
        self, plan: dict[str, Any], stage: Any, live: Any, loop: asyncio.AbstractEventLoop
    ) -> str:
        # SQLite/registry access remains on the broker loop, never the IO worker.
        # Pure file reads/hashing run in worker threads (offload_io); the packet's
        # last pass, with every durable check, is synchronous on the loop.
        preparing = asyncio.ensure_future(offload(lambda: self._packet(plan)))
        interrupted = False
        while not preparing.done():
            try:
                await asyncio.shield(preparing)
            except asyncio.CancelledError:
                interrupted = True
                self.request_stop(plan["job_id"])
        if interrupted:
            # No driver call was made; the verified reads are discarded.
            preparing.exception()
            raise FlashFailure("flash_cancelled_needs_review")
        packet = preparing.result()

        async def check_on_loop() -> bool:
            return live() is True

        def worker_live() -> bool:
            return asyncio.run_coroutine_threadsafe(check_on_loop(), loop).result(timeout=5)

        worker = asyncio.create_task(
            asyncio.to_thread(
                self._execute,
                plan,
                lambda name: loop.call_soon_threadsafe(stage, name),
                worker_live,
                packet,
            )
        )
        interrupted = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                interrupted = True
                self.request_stop(plan["job_id"])
        result = worker.result()
        if interrupted:
            raise FlashFailure("flash_cancelled_needs_review")
        return result

    def _packet(self, plan: dict[str, Any]) -> tuple[Any, ...]:
        """Verified inputs for one flash; side-effect free, so offload may re-run it."""
        validate_flash_plan(plan)
        details = self.prepare_plan(plan)
        if any(plan[key] != value for key, value in details.items()):
            raise FlashFailure("flash_inputs_changed_needs_review")
        registration = self._registration(plan["device_id"], plan["flash_operation"])
        backup = None
        bundle = None
        images: tuple[bytes, ...] = ()
        known = None
        recovery = None
        snapshot_files = {}
        metadata = None
        no_recovery_point = no_recovery_point_flash(plan)
        current = None
        if no_recovery_point:
            # No LKG/backup lookup at all. firmware_lkg.baseline is NOT NULL; a flash
            # without a recovery point records its own job id as its baseline.
            baseline = plan["job_id"]
        else:
            current = self.store().firmware_current(plan["device_id"])
            baseline = current["baseline"] if current else plan["backup_job_id"]
        if plan["lkg_receipt"] is not None:
            row, metadata, snapshot_files = self._known(
                registration, plan["device_id"], plan["lkg_receipt"]
            )
            known = metadata, snapshot_files
            baseline = row["baseline"]
            if plan["recovery_kind"] == "artifact_bundle":
                old_bundle = bundle_from_record(metadata["bundle"])
                recovery = (
                    old_bundle,
                    tuple(snapshot_files[item.name] for item in old_bundle.segments),
                )
            else:
                # The baseline backup stays owned by whoever took it; it is used
                # internally here and never delivered to the caller.
                _, backup = self._backup(registration, metadata["backup_job_id"], row["principal"])
        elif plan["flash_operation"] != "backup" and not no_recovery_point:
            _, backup = self._backup(registration, plan["backup_job_id"], plan["flash_principal"])
        if plan["flash_operation"] == "flash":
            root, manifest = self._build_context(plan["build_job_id"], plan["flash_principal"])
            validate_plan_export(plan, manifest)
            bundle = pure_io(
                "verify_bundle", _verify_bundle, root, manifest, registration.flash_bytes
            )
            receipts = {item["name"]: item for item in manifest["artifacts"]}
            images = tuple(
                pure_io("file", _file, root, receipts[item.name], 16 * 1024 * 1024)
                for item in bundle.segments
            )
            snapshot_files = {
                name: data
                for name, data in zip((item.name for item in bundle.segments), images, strict=True)
            }
            for receipt in (manifest["flash_layout"], manifest["flash_security"]):
                snapshot_files[receipt["name"]] = pure_io("file", _file, root, receipt, 1048576)
            app = json.loads(snapshot_files[manifest["flash_layout"]["name"]])["app"]["file"]
            elf_name = str(Path(app).with_suffix(".elf")).replace("\\", "/")
            elf = next(item for item in manifest["artifacts"] if item["name"] == elf_name)
            snapshot_files[elf_name] = pure_io("file", _file, root, elf, 67108864)
            compat = compatibility(manifest, bundle, snapshot_files, registration.flash_bytes)
            # The bundle's own layout is validated above. Without a named recovery
            # point (current is None, known is None, backup is None) there is no
            # previous layout to match: the partition table is written with the
            # bundle and partition/NVS/data compatibility is not checked.
            if current is not None and known is None:
                _, previous, _ = self._known(registration, plan["device_id"], current["receipt"])
                previous_compat = lkg_compat(previous)
                if previous_compat is not None:
                    same_layout(previous_compat, compat)
            known_compat = lkg_compat(known[0]) if known is not None else None
            if known_compat is not None:
                same_layout(known_compat, compat)
            elif backup is not None:
                table = next(
                    item for item in bundle.segments if item.offset == compat["table_offset"]
                )
                baseline_layout(backup, compat, snapshot_files[table.name])
            metadata = dict(
                kind="artifact_bundle",
                device_id=plan["device_id"],
                principal=plan["flash_principal"],
                expected_device_sha256=plan["expected_device_sha256"],
                flash_bytes=plan["flash_bytes"],
                selector_sha256=hashlib.sha256(
                    registration.selector.model_dump_json().encode()
                ).hexdigest(),
                expected_elf_sha256=plan["expected_elf_sha256"],
                expected_app_bin_sha256=plan["expected_app_bin_sha256"],
                bundle=bundle_record(bundle),
                manifest=manifest,
                compat=compat,
            )
        if plan["flash_operation"] == "restore" and metadata is None:
            metadata = dict(
                kind="full_backup",
                device_id=plan["device_id"],
                principal=plan["flash_principal"],
                expected_device_sha256=plan["expected_device_sha256"],
                flash_bytes=plan["flash_bytes"],
                selector_sha256=hashlib.sha256(
                    registration.selector.model_dump_json().encode()
                ).hexdigest(),
                expected_elf_sha256=plan["expected_elf_sha256"],
                expected_app_bin_sha256=plan["expected_app_bin_sha256"],
                backup_sha256=plan["backup_sha256"],
                backup_job_id=plan["backup_job_id"],
                compat=None,
            )
        return registration, backup, bundle, images, recovery, metadata, snapshot_files, baseline

    def _execute(self, plan: dict[str, Any], stage: Any, live: Any, packet: Any) -> str:
        validate_flash_plan(plan)
        if plan["job_id"] in self._cancelled_jobs or live() is not True:
            raise FlashFailure("flash_cancelled_or_authority_changed_needs_review")
        if self.active or self.uncertain:
            raise FlashFailure("flash_device_busy_or_uncertain")
        registration, backup, bundle, images, recovery, metadata, snapshot_files, baseline = packet
        backup_store = BackupStore(
            registration.backup_directory,
            windows_private_acl_confirmed=registration.private_backup_acl_confirmed,
        )
        factory = self.driver_factory or NativeFlashDriver
        driver = factory(
            registration,
            self.backend,
            plan["device_fingerprint"],
            registration.backup_directory / "work",
            plan["expected_app_bin_sha256"],
            plan["expected_elf_sha256"],
            live,
        )
        with self._lock:
            if plan["job_id"] in self._cancelled_jobs:
                raise FlashFailure("flash_cancelled_needs_review")
            self.engine = FlashLifecycle(driver)
            self.job_id = plan["job_id"]
        log = BuildLogs(
            plan["flash_principal"], job_id=plan["job_id"], on_change=self.on_logs_change
        )
        log.redactions = tuple(
            value
            for key, value in os.environ.items()
            if re.search(r"(?i)token|secret|password|key|bearer", key) and len(value) >= 8
        )
        attach(
            log,
            lambda *_: self._preopened_logs.pop(plan["job_id"], None),
            plan["job_id"],
            plan["flash_principal"],
            plan.get("test", "firmware_flash"),
            "flash",
        )
        while len(self.logs) >= 8:
            oldest = next(iter(self.logs))
            del self.logs[oldest]
            self.results.pop(oldest, None)
            self.phases.pop(oldest, None)
        self.logs[self.job_id] = log
        phases = self.phases.setdefault(self.job_id, [])

        def progress_log(message: str) -> None:
            with self._lock:
                log.append(message)

        driver.progress_log = progress_log

        def emit(name: str) -> None:
            with self._lock:
                log.append("-- " + name)
                if len(phases) < 32:
                    phases.append((name, datetime.now(UTC).isoformat(timespec="milliseconds")))
            stage(name)

        failure_code: str | None = "flash_operation_failed"
        try:
            op = operation_from_plan(plan)
            receipt, data, summary = self.engine.run(
                op,
                op.digest,
                live=live,
                reader_closed=lambda: (
                    not self.backend.active
                    and not self.backend.hardware_connected
                    and not getattr(self.backend, "ownership_uncertain", False)
                ),
                stage=emit,
                bundle=bundle,
                images=images,
                backup=backup,
                recovery=recovery,
                defer_commit=True,
                recovery_table_offset=metadata["compat"]["table_offset"]
                if recovery is not None
                else None,
            )
            if receipt is not None and data is not None:
                retained = backup_store.save(self.job_id, plan["flash_principal"], receipt, data)
                summary["backup_receipt"] = {
                    key: value for key, value in asdict(retained).items() if key != "principal"
                }
            if plan["flash_operation"] != "backup":
                if metadata is None:
                    metadata = dict(
                        kind="full_backup",
                        device_id=plan["device_id"],
                        principal=plan["flash_principal"],
                        expected_device_sha256=plan["expected_device_sha256"],
                        flash_bytes=plan["flash_bytes"],
                        selector_sha256=hashlib.sha256(
                            registration.selector.model_dump_json().encode()
                        ).hexdigest(),
                        expected_elf_sha256=plan["expected_elf_sha256"],
                        expected_app_bin_sha256=plan["expected_app_bin_sha256"],
                        backup_sha256=plan["backup_sha256"],
                        backup_job_id=plan["backup_job_id"],
                    )
                if no_recovery_point_flash(plan):
                    # The new LKG is bound to the device the pre-write probe identified.
                    metadata = dict(metadata, expected_device_sha256=summary["bound_device_sha256"])
                if self.engine.cancelled.is_set() or live() is not True:
                    raise FlashFailure("lkg_promotion_cancelled_needs_review")
                selected = (
                    self._snapshots(registration).stage(metadata, snapshot_files)
                    if plan["flash_operation"] == "flash" or plan["lkg_receipt"] is None
                    else plan["lkg_receipt"]
                )
                self.promotions[self.job_id] = dict(
                    device_id=plan["device_id"],
                    principal=plan["flash_principal"],
                    receipt=selected,
                    kind=metadata["kind"],
                    baseline=baseline or "",
                )
            self.results[self.job_id] = summary
            log.append("-- Flash operation completed; verified receipt/observations recorded")
            failure_code = None
            return f"firmware_{plan['flash_operation']}_completed"
        except Exception as failure:
            if isinstance(failure, FlashFailure) and failure.code in {
                "flash_boot_unverified_needs_review",
                "flash_boot_observation_missing",
                "flash_boot_device_changed",
                "flash_timeout_needs_review",
                "flash_failed_needs_review",
                "flash_shutdown_uncertain_needs_review",
            }:
                failure_code = failure.code
            try:
                log.append("error: flash operation failed; no retry or automatic restore")
            except Exception:
                pass
            raise
        finally:
            try:
                self._retain_boot_evidence(log, driver, plan["expected_elf_sha256"], failure_code)
            except Exception:
                # Diagnostic delivery must never replace the operation/close failure.
                pass
            finally:
                driver.last_observation = None
                driver.last_boot_attempt = None

    @staticmethod
    def _retain_boot_evidence(
        log: BuildLogs, driver: Any, expected: str | None, failure_code: str | None
    ) -> None:
        observation = getattr(driver, "last_observation", None)
        attempt = getattr(driver, "last_boot_attempt", None)
        if not isinstance(observation, DeviceObservation):
            observation = None
        if not isinstance(attempt, BootAttempt):
            attempt = None
        if observation is None and attempt is None:
            return
        if observation is not None:
            for line in observation.logs:
                log.append(line, "boot_observation", console_output=False)
        diagnostic = {
            "schema_version": 1,
            "recorded_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "operation_failure_code": failure_code,
            "attempt": attempt.metadata() if attempt is not None else None,
            "verification_reason": boot_verification_reason(observation, expected),
            "observation": observation.metadata() if observation is not None else None,
        }
        # No private boot text or metadata may escape to the broker stderr sink.
        prefix = "error: " if failure_code is not None else "info: "
        log.append(
            prefix + "boot_diagnostic " + json.dumps(diagnostic, sort_keys=True),
            "boot_observation",
            console_output=False,
        )

    def outcome(self, plan: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
        """A bounded structured summary of one flash/backup/restore job's outcome.

        Holds hashes, ids, fixed reason codes, phase names and times only: no
        firmware bytes, device output, paths or credentials.
        """
        job_id = plan["job_id"]
        write_started, close_confirmed = self.failure_evidence(job_id)
        with self._lock:
            summary = dict(self.results.get(job_id, {}))
            promotion = self.promotions.get(job_id)
            phases = list(self.phases.get(job_id, ()))
        succeeded = job.get("state") == "succeeded"
        recorded = datetime.now(UTC).isoformat(timespec="milliseconds")
        duration = None
        if phases:
            try:
                start = datetime.fromisoformat(phases[0][1])
                duration = round((datetime.fromisoformat(recorded) - start).total_seconds(), 3)
            except ValueError:
                duration = None
        return {
            "schema_version": 1,
            "job_id": job_id,
            "test": plan.get("test"),
            "operation": plan.get("flash_operation"),
            "state": job.get("state"),
            "reason_code": job.get("result"),
            "last_stage": job.get("stage"),
            "phase_reached": phases[-1][0] if phases else None,
            "phases": [{"name": name, "t": at} for name, at in phases],
            "write_started": bool(write_started),
            "close_confirmed": bool(close_confirmed),
            "plan_hash": job.get("plan_hash"),
            "device": {
                "device_id": plan.get("device_id"),
                "device_fingerprint": plan.get("device_fingerprint"),
                "expected_device_sha256": plan.get("expected_device_sha256"),
                "bound_device_sha256": summary.get("bound_device_sha256"),
                "flash_bytes": plan.get("flash_bytes"),
            },
            "references": {
                key: plan.get(key)
                for key in (
                    "recovery_kind",
                    "lkg_receipt",
                    "backup_job_id",
                    "backup_sha256",
                    "build_job_id",
                    "manifest_sha256",
                    "expected_elf_sha256",
                    "expected_app_bin_sha256",
                )
            },
            "backup_receipt": summary.get("backup_receipt"),
            "staged_lkg_receipt": promotion["receipt"] if promotion else None,
            "lkg_selected": succeeded and promotion is not None,
            "verification": {
                key: value
                for key, value in summary.items()
                if key not in ("backup_receipt", "bound_device_sha256")
                and isinstance(value, (str, int, bool))
            },
            "timings": {
                "created": job.get("created"),
                "finished": job.get("finished"),
                "first_phase": phases[0][1] if phases else None,
                "recorded": recorded,
                "duration_seconds": duration,
            },
        }

    def record_outcome(self, plan: dict[str, Any], job: dict[str, Any]) -> bool:
        """Append the flash_outcome record to the job's durable log; never raises.

        Uses the log attached when the worker started, or opens the job's log when
        the job ended before the worker ran (admission/input failures).
        """
        opened = None
        try:
            log = self.logs.get(plan["job_id"])
            durable = getattr(log, "durable", None) if log is not None else None
            if durable is None:
                if self.open_job_log is None:
                    return False
                principal = plan.get("flash_principal")
                if not isinstance(principal, str) or not principal:
                    return False
                opened = durable = self.open_job_log(
                    plan["job_id"], principal, str(plan.get("test", "firmware_flash"))
                )
                if durable is None:
                    return False
            durable.record("flash_outcome", self.outcome(plan, job), reserve=True)
            return not durable.unavailable
        except Exception:
            return False
        finally:
            if opened is not None:
                try:
                    opened.close()
                except Exception:
                    pass

    def read_logs(self, request: Any, principal: str) -> dict[str, Any]:
        with self._lock:
            logs = self.logs.get(request.job_id)
            if logs is None:
                result = dict(
                    entries=[],
                    expired=True,
                    gap=True,
                    has_more=False,
                    next_cursor=dict(epoch="0" * 32, sequence=0),
                )
            else:
                result = logs.read(principal, request)
            if result["expired"] and self.job_logs is not None:
                durable = self.job_logs.build_read(request.job_id, principal, "flash", request)
                if durable is not None:
                    result = durable
            persisted = self.store().firmware_summary(request.job_id, principal)
            if persisted is not None:
                result["flash_summary"] = persisted
            elif not result["expired"] and request.job_id in self.results:
                result["flash_summary"] = self.results[request.job_id]
            if self.job_logs is not None:
                outcomes = self.job_logs.records(request.job_id, principal, "flash_outcome")
                if outcomes:
                    result["flash_outcome"] = outcomes[-1]["data"]
            return result
