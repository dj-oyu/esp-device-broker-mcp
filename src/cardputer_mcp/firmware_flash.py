"""Broker integration for separately requested backup/flash/restore operations."""

import asyncio
import hashlib
import json
import os
import re
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .build_logs import BuildLogs
from .flash_backup import BackupStore
from .flash_driver import NativeFlashDriver, esptool_fingerprint
from .flash_lifecycle import FlashFailure, FlashLifecycle, Operation, OperationPlan
from .flash_lkg_bundle import (
    baseline_layout,
    bundle_from_record,
    bundle_record,
    compatibility,
    same_layout,
)
from .flash_lkg_store import FirmwareSnapshots
from .flash_manifest import _file, verify_bundle
from .models import TestName

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
        "interrupted/uncertain work needs review"
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
    "Flash leaves the new firmware installed; restore is a separate explicit operation; no rollback",
    (
        "Shares bounded redacted progress/boot logs only with the requesting "
        "principal; no raw backup delivery"
    ),
]
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
    if set(plan) != base | FLASH_FIELDS or plan.get("test") not in FLASH_ACTIONS:
        raise ValueError("invalid_flash_plan")
    if (
        plan["flash_operation"] != FLASH_ACTIONS[plan["test"]]
        or plan["recovery_kind"] not in ("full_backup", "artifact_bundle")
        or plan["backend"] not in ("mock", "serial")
        or plan["device"]
        != ("mock-cardputer" if plan["backend"] == "mock" else "configured-cardputer")
        or plan["expected_idf"] != "6.0.1"
        or plan["target"] != "esp32s3"
        or type(plan["flash_bytes"]) is not int
        or plan["flash_bytes"] not in (2097152, 4194304, 8388608, 16777216)
        or not isinstance(plan["flash_principal"], str)
        or re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", plan["flash_principal"]) is None
        or plan["effects"] != FLASH_EFFECTS
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


class FirmwareFlash:
    def __init__(self, settings: Any, backend: Any, build: Any, store: Any) -> None:
        self.settings, self.backend, self.build, self.store = settings, backend, build, store
        self.driver_factory: Any = None  # Trusted test injection only, never configuration/MCP.
        self.resolver: Any = None
        self.engine: FlashLifecycle | None = None
        self.job_id: str | None = None
        self.logs: dict[str, BuildLogs] = {}
        self.results: dict[str, dict[str, Any]] = {}
        self.promotions: dict[str, dict[str, str]] = {}
        self._cancelled_jobs: set[str] = set()
        self._lock = threading.RLock()

    @property
    def active(self) -> bool:
        return self.engine is not None and self.engine.active

    @property
    def uncertain(self) -> bool:
        return self.engine is not None and self.engine.uncertain

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

            if (
                hash_file(registration.python_tool.executable) != registration.python_tool.sha256
                or esptool_fingerprint(registration.esptool_directory)
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
            or plan.get("flash_principal") != principal
        ):
            raise FlashFailure("successful_owned_backup_required")
        return BackupStore(
            registration.backup_directory,
            windows_private_acl_confirmed=registration.private_backup_acl_confirmed,
        ).load(job_id, principal)

    def _snapshots(self, registration: Any) -> FirmwareSnapshots:
        return FirmwareSnapshots(
            registration.backup_directory / "lkg",
            windows_private_acl_confirmed=registration.private_backup_acl_confirmed,
        )

    def _known(self, registration: Any, device_id: str, principal: str, receipt: str | None) -> Any:
        row = (
            self.store().firmware_known(device_id, principal, receipt)
            if receipt
            else self.store().firmware_current(device_id, principal)
        )
        if row is None:
            raise FlashFailure("verified_lkg_or_initial_backup_required")
        metadata, files = self._snapshots(registration).load(row["receipt"])
        selector = hashlib.sha256(registration.selector.model_dump_json().encode()).hexdigest()
        if (
            metadata["device_id"] != device_id
            or metadata["principal"] != principal
            or metadata["selector_sha256"] != selector
            or metadata["flash_bytes"] != registration.flash_bytes
        ):
            raise FlashFailure("lkg_device_binding_changed")
        if metadata.get("kind") != row["kind"]:
            raise FlashFailure("lkg_receipt_kind_changed")
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
            or plan.get("build_principal") != principal
        ):
            raise FlashFailure("successful_owned_build_required")
        resolver = self.resolver or getattr(self.build, "resolve_flash_manifest", None)
        if resolver is None:
            raise FlashFailure("build_manifest_resolver_unavailable")
        root, manifest = resolver(job_id, principal, plan["project_id"])
        if not isinstance(root, Path) or not isinstance(manifest, dict):
            raise FlashFailure("invalid_internal_build_manifest_context")
        return root, manifest

    def prepare(self, request: Any, principal: str, epoch: str, session_id: str) -> dict[str, Any]:
        operation = FLASH_ACTIONS[request.test.value]
        registration = self._registration(request.device_id, operation)
        if not principal:
            raise FlashFailure("flash_authenticated_principal_required")
        if self.driver_factory is None:
            usb = self.backend.prepare(TestName.RESTART)["device_fingerprint"]
        else:
            usb = hashlib.sha256(("mock:" + self.backend.device).encode()).hexdigest()
        expected_device = backup_digest = manifest_digest = elf_digest = image_digest = None
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
            else:
                row, metadata, files = self._known(
                    registration,
                    request.device_id,
                    principal,
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
                bundle = verify_bundle(root, manifest, flash_bytes=registration.flash_bytes)
                manifest_digest = bundle.manifest_sha256
                layout = json.loads(_file(root, manifest["flash_layout"], 65536))
                app = layout.get("app", {}).get("file")
                if not isinstance(app, str) or app not in {item.name for item in bundle.segments}:
                    raise FlashFailure("flash_app_receipt_required")
                image_digest = next(item.sha256 for item in bundle.segments if item.name == app)
                elf = str(Path(app).with_suffix(".elf")).replace("\\", "/")
                receipt_elf = next((a for a in manifest["artifacts"] if a["name"] == elf), None)
                if receipt_elf is None:
                    raise FlashFailure("flash_app_elf_receipt_required")
                _file(root, receipt_elf, 64 * 1024 * 1024)
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
        registration_digest = hashlib.sha256(registration.model_dump_json().encode()).hexdigest()
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
            effects=FLASH_EFFECTS,
            stages=FLASH_STAGES,
            expected_idf="6.0.1",
            target="esp32s3",
            argv_template=["registered-python", "fixed-flash-worker", "broker-owned-operation"],
        )

    def prepare_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        from types import SimpleNamespace

        request = SimpleNamespace(
            test=TestName(plan["test"]),
            job_id=plan["job_id"],
            device_id=plan["device_id"],
            build_job_id=plan["build_job_id"],
            backup_job_id=plan["backup_job_id"],
            lkg_receipt=plan["lkg_receipt"],
        )
        return self.prepare(
            request, plan["flash_principal"], plan["broker_epoch"], plan["session_id"]
        )

    async def execute(self, plan: dict[str, Any], stage: Any, live: Any) -> str:
        loop = asyncio.get_running_loop()
        # SQLite/registry access remains on the broker loop, never the IO worker.
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
        current = self.store().firmware_current(plan["device_id"], plan["flash_principal"])
        baseline = current["baseline"] if current else plan["backup_job_id"]
        if plan["lkg_receipt"] is not None:
            row, metadata, snapshot_files = self._known(
                registration, plan["device_id"], plan["flash_principal"], plan["lkg_receipt"]
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
                _, backup = self._backup(
                    registration, metadata["backup_job_id"], plan["flash_principal"]
                )
        elif plan["flash_operation"] != "backup":
            _, backup = self._backup(registration, plan["backup_job_id"], plan["flash_principal"])
        if plan["flash_operation"] == "flash":
            root, manifest = self._build_context(plan["build_job_id"], plan["flash_principal"])
            bundle = verify_bundle(root, manifest, flash_bytes=registration.flash_bytes)
            receipts = {item["name"]: item for item in manifest["artifacts"]}
            images = tuple(
                _file(root, receipts[item.name], 16 * 1024 * 1024) for item in bundle.segments
            )
            snapshot_files = {
                name: data
                for name, data in zip((item.name for item in bundle.segments), images, strict=True)
            }
            for receipt in (manifest["flash_layout"], manifest["flash_security"]):
                snapshot_files[receipt["name"]] = _file(root, receipt, 1048576)
            app = json.loads(snapshot_files[manifest["flash_layout"]["name"]])["app"]["file"]
            elf_name = str(Path(app).with_suffix(".elf")).replace("\\", "/")
            elf = next(item for item in manifest["artifacts"] if item["name"] == elf_name)
            snapshot_files[elf_name] = _file(root, elf, 67108864)
            compat = compatibility(manifest, bundle, snapshot_files, registration.flash_bytes)
            if current is not None and known is None:
                _, previous, _ = self._known(
                    registration, plan["device_id"], plan["flash_principal"], current["receipt"]
                )
                if previous.get("compat") is not None:
                    same_layout(previous["compat"], compat)
            if known is not None and known[0].get("compat") is not None:
                same_layout(known[0]["compat"], compat)
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
        packet = registration, backup, bundle, images, recovery, metadata, snapshot_files, baseline

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
        log = BuildLogs(plan["flash_principal"], job_id=plan["job_id"])
        log.redactions = tuple(
            value
            for key, value in os.environ.items()
            if re.search(r"(?i)token|secret|password|key|bearer", key) and len(value) >= 8
        )
        while len(self.logs) >= 8:
            oldest = next(iter(self.logs))
            del self.logs[oldest]
            self.results.pop(oldest, None)
        self.logs[self.job_id] = log

        def progress_log(message: str) -> None:
            with self._lock:
                log.append(message)

        driver.progress_log = progress_log

        def emit(name: str) -> None:
            with self._lock:
                log.append("-- " + name)
            stage(name)

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
            observation = getattr(driver, "last_observation", None)
            if observation is not None:
                for line in observation.logs:
                    log.append(line)
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
            return f"firmware_{plan['flash_operation']}_completed"
        except Exception:
            log.append("error: flash operation failed; needs review; no retry or automatic restore")
            raise
        finally:
            driver.last_observation = None

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
            persisted = self.store().firmware_summary(request.job_id, principal)
            if persisted is not None:
                result["flash_summary"] = persisted
            elif not result["expired"] and request.job_id in self.results:
                result["flash_summary"] = self.results[request.job_id]
            return result
