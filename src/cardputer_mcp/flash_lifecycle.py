"""Single-owner flash lifecycle with injectable drivers and no implicit retry."""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from .flash_manifest import FlashBundle

Operation = Literal["backup", "flash", "restore"]

_SAFE_ROM_EXCEPTION_TYPES = frozenset(
    {
        "EOFError",
        "FatalError",
        "FileNotFoundError",
        "OSError",
        "OtherError",
        "PermissionError",
        "RuntimeError",
        "SerialException",
        "SerialTimeoutException",
        "TimeoutError",
        "TypeError",
        "UnsupportedCommandError",
        "ValueError",
    }
)
_SAFE_ROM_FAILURE_CLASSES = frozenset({"timeout", "io", "validation", "native_other"})


def _bounded_read_diagnostic(code: str, value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    fields = {"code", "operation", "read_bytes", "read_total", "elapsed_seconds", "failure_origin"}
    native_fields = fields | {"exception_type", "failure_class"}
    origin = value.get("failure_origin")
    if (
        type(origin) is not str
        or set(value) != (native_fields if origin == "native_worker" else fields)
        or value.get("code") != code
        or value.get("operation") != "rom_read"
        or origin not in {"native_worker", "driver_guard", "driver_or_process"}
        or type(value.get("read_bytes")) is not int
        or type(value.get("read_total")) is not int
        or not 0 <= value["read_bytes"] <= value["read_total"] <= 16777216
        or type(value.get("elapsed_seconds")) is not int
        or not 0 <= value["elapsed_seconds"] <= 100000
    ):
        return None
    if origin == "native_worker" and (
        type(value["exception_type"]) is not str
        or type(value["failure_class"]) is not str
        or value["exception_type"] not in _SAFE_ROM_EXCEPTION_TYPES
        or value["failure_class"] not in _SAFE_ROM_FAILURE_CLASSES
    ):
        return None
    return dict(value)


class FlashFailure(ValueError):
    def __init__(self, code: str, diagnostic: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = _bounded_read_diagnostic(code, diagnostic)
        # Set only by FirmwareFlash.prepare for a recovery-point refusal; the public
        # error gate re-validates it against fixed vocabulary.
        self.public_details: dict[str, Any] | None = None


@dataclass(frozen=True)
class SecurityReceipt:
    device_sha256: str
    flash_bytes: int
    chip: str
    secure_boot: bool | None
    flash_encryption: bool | None
    secure_download: bool | None

    def verify(self, expected: str | None, capacity: int) -> None:
        if (
            self.chip != "esp32s3"
            or self.flash_bytes != capacity
            or len(self.device_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.device_sha256)
            or (expected is not None and self.device_sha256 != expected)
            or self.secure_boot is not False
            or self.flash_encryption is not False
            or self.secure_download is not False
        ):
            raise FlashFailure("device_security_or_identity_not_verified")


@dataclass(frozen=True)
class OperationPlan:
    operation: Operation
    job_id: str
    principal: str
    broker_epoch: str
    session_id: str
    device_fingerprint: str
    flash_bytes: int
    expected_device_sha256: str | None = None
    backup_sha256: str | None = None
    manifest_sha256: str | None = None
    expected_elf_sha256: str | None = None
    # "full_backup" / "artifact_bundle" name a frozen recovery point; "none" (flash
    # only) names none: no recovery-bytes check and no pre-write fence, and the
    # device identity is bound from the probe just before the write.
    recovery_kind: str = "full_backup"

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class BackupReceipt:
    device_sha256: str
    device_fingerprint: str
    flash_bytes: int
    sha256: str


class FlashDriver(Protocol):
    """Internal registered-device driver; no remote command/path interface."""

    @property
    def active(self) -> bool: ...

    @property
    def uncertain(self) -> bool: ...

    def probe(self) -> SecurityReceipt: ...
    def read(self, offset: int, size: int) -> bytes: ...
    def write(self, segments: tuple[tuple[int, bytes], ...]) -> None: ...
    def boot(self) -> dict[str, Any]: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class FlashLifecycle:
    """Called only by a broker holding its original lease/device/state locks.

    The explicit request binds an exact immutable plan digest. The live callback
    is rechecked between every side effect. An uncertain
    close retains busy ownership; cancellation never claims synchronous shutdown.
    """

    def __init__(self, driver: FlashDriver) -> None:
        self.driver = driver
        self.active = False
        self.uncertain = False
        self.cancelled = threading.Event()
        self._lock = threading.RLock()
        self._committed: str | None = None
        # Evidence for failure classification (device_failure_outcome.py): set
        # immediately before `driver.write` is called and never cleared, so any
        # failure after this point counts as "write started or possibly started".
        self.write_started = False

    def request_stop(self, job_id: str) -> bool:
        with self._lock:
            if self._committed == job_id:
                return False
            self.cancelled.set()
        self.driver.stop()
        return True

    def recheck_shutdown(self) -> tuple[bool, str]:
        """Explicit operator re-check of an unconfirmed shutdown; never writes.

        Uses only the driver's existing confirmed close, which refuses while any
        driver process or serial handle is active or uncertain.
        """
        with self._lock:
            if not self.uncertain:
                return True, "not_set"
            try:
                self.driver.close()
                if self.driver.active or self.driver.uncertain:
                    return False, "flash_driver_still_owned"
            except Exception:
                return False, "flash_driver_still_owned"
            self.uncertain = False
            self.active = False
            return True, "flash_shutdown_confirmed"

    def _live(self, live: Callable[[], bool]) -> None:
        if self.cancelled.is_set():
            raise FlashFailure("flash_cancelled_needs_review")
        if live() is not True:
            raise FlashFailure("flash_authority_changed_needs_review")

    def run(
        self,
        plan: OperationPlan,
        requested_digest: str,
        *,
        live: Callable[[], bool],
        reader_closed: Callable[[], bool],
        stage: Callable[[str], None],
        bundle: FlashBundle | None = None,
        images: tuple[bytes, ...] = (),
        backup: bytes | None = None,
        recovery: tuple[FlashBundle, tuple[bytes, ...]] | None = None,
        defer_commit: bool = False,
        recovery_table_offset: int | None = None,
    ) -> tuple[BackupReceipt | None, bytes | None, dict[str, Any]]:
        with self._lock:
            if self.active or self.uncertain or self.driver.active or self.driver.uncertain:
                raise FlashFailure("flash_device_busy_or_uncertain")
            if requested_digest != plan.digest or reader_closed() is not True:
                raise FlashFailure("flash_plan_or_handoff_required")
            if plan.recovery_kind == "none" and (
                plan.operation != "flash"
                or plan.expected_device_sha256 is not None
                or plan.backup_sha256 is not None
                or backup is not None
                or recovery is not None
            ):
                raise FlashFailure("flash_plan_or_handoff_required")
            if plan.operation == "flash" and (
                not isinstance(plan.expected_elf_sha256, str)
                or len(plan.expected_elf_sha256) != 64
                or any(c not in "0123456789abcdef" for c in plan.expected_elf_sha256)
            ):
                raise FlashFailure("flash_expected_elf_receipt_required")
            self.active = True
            self._committed = None
        result: tuple[BackupReceipt | None, bytes | None, dict[str, Any]]
        try:
            self._live(live)
            stage("flash_security_probe")
            security = self.driver.probe()
            self._live(live)
            security.verify(plan.expected_device_sha256, plan.flash_bytes)
            if plan.operation == "backup":
                stage("flash_backup_read")
                data = self.driver.read(0, plan.flash_bytes)
                self._live(live)
                if len(data) != plan.flash_bytes:
                    raise FlashFailure("flash_backup_incomplete_needs_review")
                receipt = BackupReceipt(
                    security.device_sha256,
                    plan.device_fingerprint,
                    plan.flash_bytes,
                    hashlib.sha256(data).hexdigest(),
                )
                result = receipt, data, {"backup_verified": True, "hardware_write": False}
            else:
                no_recovery_point = plan.recovery_kind == "none"
                if plan.operation not in ("flash", "restore") or (
                    plan.expected_device_sha256 is None and not no_recovery_point
                ):
                    raise FlashFailure("verified_device_recovery_required")
                # Without a recovery point the expected identity is the device the
                # probe identified (verify() rejected anything not a 64-hex id). The
                # registered driver forwards it to the write, every readback and the
                # post-reset same-USB boot reconnect; a different device is refused.
                bound_device = security.device_sha256
                if plan.recovery_kind == "artifact_bundle":
                    if recovery is None:
                        raise FlashFailure("verified_firmware_recovery_required")
                    prior, prior_images = recovery
                    if len(prior.segments) != len(prior_images) or any(
                        len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256
                        for item, data in zip(prior.segments, prior_images, strict=True)
                    ):
                        raise FlashFailure("lkg_bytes_changed")
                elif not no_recovery_point and (
                    backup is None
                    or len(backup) != plan.flash_bytes
                    or hashlib.sha256(backup).hexdigest() != plan.backup_sha256
                ):
                    raise FlashFailure("verified_device_backup_required")
                if plan.operation == "flash":
                    if (
                        bundle is None
                        or bundle.manifest_sha256 != plan.manifest_sha256
                        or len(images) != len(bundle.segments)
                        or any(
                            len(data) != item.size
                            or hashlib.sha256(data).hexdigest() != item.sha256
                            for item, data in zip(bundle.segments, images, strict=True)
                        )
                    ):
                        raise FlashFailure("flash_manifest_changed_needs_review")
                    # Recheck every sector that will be erased, including padding.
                    # An unrelated NVS change outside these ranges is not erased.
                    # Only an explicitly named recovery point has a fence to keep.
                    if not no_recovery_point:
                        stage("flash_backup_prewrite_check")
                    if recovery is not None:
                        prior, prior_images = recovery
                        for item, expected in zip(prior.segments, prior_images, strict=True):
                            self._live(live)
                            if self.driver.read(item.offset, item.size) != expected:
                                raise FlashFailure("lkg_current_firmware_changed_needs_review")
                    elif not no_recovery_point:
                        assert backup is not None
                        for item in bundle.segments:
                            size = ((item.size + 4095) // 4096) * 4096
                            self._live(live)
                            if (
                                self.driver.read(item.offset, size)
                                != backup[item.offset : item.offset + size]
                            ):
                                raise FlashFailure("flash_backup_stale_needs_review")
                    segments = tuple(
                        (item.offset, data)
                        for item, data in zip(bundle.segments, images, strict=True)
                    )
                elif recovery is not None:
                    prior, prior_images = recovery
                    table = next(
                        (item for item in prior.segments if item.offset == recovery_table_offset),
                        None,
                    )
                    if table is None:
                        raise FlashFailure("lkg_restore_layout_evidence_required")
                    expected = prior_images[prior.segments.index(table)]
                    self._live(live)
                    if self.driver.read(table.offset, table.size) != expected:
                        raise FlashFailure("lkg_restore_partition_changed_needs_review")
                    segments = tuple(
                        (item.offset, data)
                        for item, data in zip(prior.segments, prior_images, strict=True)
                    )
                else:
                    assert backup is not None
                    segments = ((0, backup),)
                self._live(live)
                stage("flash_write" if plan.operation == "flash" else "flash_restore_write")
                self._live(live)
                self.write_started = True
                self.driver.write(segments)
                self._live(live)
                stage("flash_readback_verify")
                for offset, data in segments:
                    self._live(live)
                    actual = self.driver.read(offset, len(data))
                    if hashlib.sha256(actual).digest() != hashlib.sha256(data).digest():
                        raise FlashFailure("flash_readback_mismatch_needs_review")
                stage("flash_boot_observe")
                self._live(live)
                summary = self.driver.boot()
                self._live(live)
                if (
                    summary.get("boot_observed") is not True
                    or summary.get("idf_version") != "6.0.1"
                    or summary.get("target") != "esp32s3"
                    or (
                        plan.expected_elf_sha256 is not None
                        and (
                            not isinstance(summary.get("elf_sha256_prefix"), str)
                            or not 8 <= len(summary["elf_sha256_prefix"]) <= 16
                            or any(
                                c not in "0123456789abcdef" for c in summary["elf_sha256_prefix"]
                            )
                            or not plan.expected_elf_sha256.startswith(summary["elf_sha256_prefix"])
                        )
                    )
                ):
                    raise FlashFailure("flash_boot_unverified_needs_review")
                # Accept only explicitly bounded public fields, never raw output.
                result = (
                    None,
                    None,
                    {
                        "boot_observed": True,
                        "idf_version": "6.0.1",
                        "target": "esp32s3",
                        "readback_verified": True,
                        "passive_boot_verified": True,
                        # Boot text is matching evidence, not authenticated identity.
                        "boot_text_identity_verified": False,
                        "boot_log_matches_expected_artifact": True,
                        "observed_elf_sha256_prefix": summary["elf_sha256_prefix"],
                        "observed_idf_version": "6.0.1",
                        "verification_basis": "full_segment_sha256_readback_and_boot_elf_prefix",
                        "boot_text_is_cryptographic_proof": False,
                    },
                )
                if no_recovery_point:
                    result[2].update(
                        recovery_kind="none",
                        prewrite_fence="not_applied_no_recovery_point",
                        data_compatibility="not_checked",
                        device_identity_basis="probed_before_write",
                        bound_device_sha256=bound_device,
                    )
            self._live(live)
        except FlashFailure:
            raise
        except TimeoutError:
            raise FlashFailure("flash_timeout_needs_review") from None
        except Exception:
            raise FlashFailure("flash_failed_needs_review") from None
        finally:
            try:
                self.driver.close()
                if self.driver.active or self.driver.uncertain:
                    raise RuntimeError
            except Exception:
                self.uncertain = True
                raise FlashFailure("flash_shutdown_uncertain_needs_review") from None
            if not self.uncertain:
                self.active = False
        self._live(live)
        with self._lock:
            if self.cancelled.is_set():
                raise FlashFailure("flash_cancelled_needs_review")
            if not defer_commit:
                self._committed = plan.job_id
        return result
