"""Single-owner flash lifecycle with injectable drivers and no implicit retry."""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from .flash_manifest import FlashBundle

Operation = Literal["backup", "flash", "restore"]


class FlashFailure(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
    expected_app_sha256: str | None = None

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

    active: bool
    uncertain: bool

    def probe(self) -> SecurityReceipt: ...
    def read(self, offset: int, size: int) -> bytes: ...
    def write(self, segments: tuple[tuple[int, bytes], ...]) -> None: ...
    def boot(self) -> dict[str, Any]: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...


class FlashLifecycle:
    """Called only by a broker holding its original lease/device/state locks.

    Approval is an exact locally obtained plan digest, not a boolean supplied by
    MCP. The live callback is rechecked between every side effect. An uncertain
    close retains busy ownership; cancellation never claims synchronous shutdown.
    """

    def __init__(self, driver: FlashDriver) -> None:
        self.driver = driver
        self.active = False
        self.uncertain = False
        self.cancelled = threading.Event()
        self._lock = threading.RLock()
        self._committed: str | None = None

    def request_stop(self, job_id: str) -> bool:
        with self._lock:
            if self._committed == job_id:
                return False
            self.cancelled.set()
        self.driver.stop()
        return True

    def _live(self, live: Callable[[], bool]) -> None:
        if self.cancelled.is_set():
            raise FlashFailure("flash_cancelled_needs_review")
        if live() is not True:
            raise FlashFailure("flash_authority_changed_needs_review")

    def run(
        self,
        plan: OperationPlan,
        approved_digest: str,
        *,
        live: Callable[[], bool],
        reader_closed: Callable[[], bool],
        stage: Callable[[str], None],
        bundle: FlashBundle | None = None,
        images: tuple[bytes, ...] = (),
        backup: bytes | None = None,
    ) -> tuple[BackupReceipt | None, bytes | None, dict[str, Any]]:
        with self._lock:
            if self.active or self.uncertain or self.driver.active or self.driver.uncertain:
                raise FlashFailure("flash_device_busy_or_uncertain")
            if approved_digest != plan.digest or reader_closed() is not True:
                raise FlashFailure("flash_approval_or_handoff_required")
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
                if (
                    plan.operation not in ("flash", "restore")
                    or plan.expected_device_sha256 is None
                    or backup is None
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
                    stage("flash_backup_prewrite_check")
                    for item in bundle.segments:
                        size = ((item.size + 4095) // 4096) * 4096
                        self._live(live)
                        current = self.driver.read(item.offset, size)
                        if current != backup[item.offset : item.offset + size]:
                            raise FlashFailure("flash_backup_stale_needs_review")
                    segments = tuple(
                        (item.offset, data)
                        for item, data in zip(bundle.segments, images, strict=True)
                    )
                else:
                    segments = ((0, backup),)
                self._live(live)
                stage("flash_write" if plan.operation == "flash" else "flash_restore_write")
                self._live(live)
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
                        plan.expected_app_sha256 is not None
                        and summary.get("app_sha256") != plan.expected_app_sha256
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
                    },
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
        with self._lock:
            self._live(live)
            self._committed = plan.job_id
        return result
