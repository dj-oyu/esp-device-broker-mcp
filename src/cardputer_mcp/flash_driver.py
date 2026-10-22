"""Registered Windows ROM-only driver. Constructed only for explicitly requested execution."""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .backend import BackendResult, DeviceOperationFailure
from .device_observation import DeviceObservation
from .build_process import BuildProcess, ProcessDeadlineExpired
from .firmware_build import OS_ENV, hash_file
from .flash_lifecycle import (
    _SAFE_ROM_EXCEPTION_TYPES,
    _SAFE_ROM_FAILURE_CLASSES,
    FlashFailure,
    SecurityReceipt,
)
from .flash_read_progress import ReadProgress, read_deadline_seconds
from .models import TestName
from .ports import resolve_port
from .serial_backend import SerialBackend, SerialFailure, fingerprint


def boot_verification_reason(observation: DeviceObservation | None, expected: str | None) -> str:
    if observation is None:
        return "observation_unavailable"
    metadata = observation.firmware.metadata()
    prefix = metadata.get("elf_sha256_prefix")
    if metadata.get("status") != "observed":
        return "startup_identity_unverified"
    if (metadata.get("idf_version") or "").removeprefix("v") != "6.0.1":
        return "idf_version_mismatch"
    if (
        not isinstance(prefix, str) or not 8 <= len(prefix) <= 16
        or any(c not in "0123456789abcdef" for c in prefix)
    ):
        return "elf_prefix_invalid"
    if expected is None:
        return "expected_elf_missing"
    return "elf_idf_match" if expected.startswith(prefix) else "elf_sha256_mismatch"


@dataclass(frozen=True)
class BootAttempt:
    started_utc: datetime
    finished_utc: datetime
    attempt_elapsed_ms: int
    observation_elapsed_ms: int | None
    reason: str
    primary_failure_code: str | None

    def metadata(self) -> dict[str, Any]:
        safe = DeviceOperationFailure.CODES | {
            "boot_failed", "boot_observation_completed", "flash_boot_device_changed",
            "flash_boot_observation_missing", "flash_boot_unverified_needs_review",
        }
        return {
            "started_utc": self.started_utc.isoformat(timespec="milliseconds"),
            "finished_utc": self.finished_utc.isoformat(timespec="milliseconds"),
            "attempt_elapsed_ms": self.attempt_elapsed_ms,
            "observation_elapsed_ms": self.observation_elapsed_ms,
            "reason": self.reason if self.reason in safe else "boot_failed",
            "primary_failure_code": self.primary_failure_code
            if self.primary_failure_code in DeviceOperationFailure.CODES else None,
        }


def _worker_read_failure(line: str) -> dict[str, str] | None:
    try:
        result = json.loads(line)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(result, dict)
        or set(result) != {"ok", "error", "operation", "exception_type", "failure_class"}
        or result["ok"] is not False
        or result["error"] != "flash_native_failed_needs_review"
        or result["operation"] != "rom_read"
        or type(result["exception_type"]) is not str
        or type(result["failure_class"]) is not str
        or result["exception_type"] not in _SAFE_ROM_EXCEPTION_TYPES
        or result["failure_class"] not in _SAFE_ROM_FAILURE_CLASSES
    ):
        return None
    return {
        "failure_origin": "native_worker",
        "exception_type": result["exception_type"],
        "failure_class": result["failure_class"],
    }


def esptool_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    count = total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink() or (
            path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise FlashFailure("linked_esptool_not_supported")
        if not path.is_file():
            continue
        count += 1
        total += path.stat().st_size
        if count > 2048 or total > 64 * 1024 * 1024:
            raise FlashFailure("registered_esptool_limit")
        digest.update(relative.as_posix().encode() + b"\0" + bytes.fromhex(hash_file(path)))
    if count == 0:
        raise FlashFailure("registered_esptool_missing")
    return digest.hexdigest()


class GuardedObservationPort:
    """Check the live authority immediately before each serial read/write."""

    def __init__(self, port: Any, check: Callable[[], None]) -> None:
        object.__setattr__(self, "_port", port)
        object.__setattr__(self, "_check", check)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._port, name)

    def __setattr__(self, name: str, value: Any) -> None:
        # Deasserting an already started pulse is cleanup, even after revocation.
        if name in {"dtr", "rts"} and value is not False:
            self._check()
        setattr(self._port, name, value)

    def open(self) -> None:
        self._check()
        self._port.open()

    def reset_input_buffer(self) -> None:
        self._check()
        self._port.reset_input_buffer()

    def read(self, size: int = 1) -> bytes:
        self._check()
        return bytes(self._port.read(size))

    def write(self, data: bytes) -> int | None:
        self._check()
        result: int | None = self._port.write(data)
        return result


class NativeFlashDriver:
    def __init__(
        self,
        registration: Any,
        backend: Any,
        expected_usb: str,
        work_root: Path,
        expected_app_bin_sha256: str | None = None,
        expected_elf_sha256: str | None = None,
        live: Callable[[], bool] | None = None,
    ) -> None:
        self.registration = registration
        self.backend = backend
        self.expected_usb = expected_usb
        self.work_root = work_root
        self.process = BuildProcess()
        self.device_sha256: str | None = None
        self._stop = threading.Event()
        self.last_observation: Any = None
        self.last_boot_attempt: BootAttempt | None = None
        self.expected_app_bin_sha256 = expected_app_bin_sha256
        self.expected_elf_sha256 = expected_elf_sha256
        self.live = live
        self.progress_log: Callable[[str], None] | None = None
        self._observation_backend: Any = None

    @property
    def active(self) -> bool:
        observation_active = self._observation_backend is not None and (
            self._observation_backend.active or self._observation_backend.hardware_connected
        )
        return bool(
            self.process.active
            or self.backend.active
            or self.backend.hardware_connected
            or observation_active
        )

    @property
    def uncertain(self) -> bool:
        return bool(
            self.process.uncertain
            or getattr(self.backend, "ownership_uncertain", False)
            or getattr(self._observation_backend, "ownership_uncertain", False)
        )

    def _authority(self) -> None:
        if self._stop.is_set() or self.live is None or self.live() is not True:
            raise FlashFailure("flash_authority_changed_needs_review")

    def _verified_port(self) -> str:
        self._authority()
        if self._stop.is_set():
            raise FlashFailure("flash_cancelled_needs_review")
        if os.name != "nt" or self.backend.name != "serial":
            raise FlashFailure("registered_windows_serial_required")
        port = resolve_port(self.registration.selector, self.backend.inventory())
        if fingerprint(port) != self.expected_usb:
            raise FlashFailure("flash_registered_device_changed")
        if (
            self.registration.selector != self.backend.config.selector
            or self.registration.python_tool.executable.is_symlink()
            or hash_file(self.registration.python_tool.executable)
            != self.registration.python_tool.sha256
            or esptool_fingerprint(self.registration.esptool_directory)
            != self.registration.esptool_sha256
        ):
            raise FlashFailure("registered_flash_tools_changed")
        return port.port

    def _invoke(
        self,
        action: str,
        *,
        offset: int = 0,
        size: int = 0,
        segments: tuple[tuple[int, bytes], ...] = (),
    ) -> tuple[dict[str, Any], bytes | None]:
        native_seconds = read_deadline_seconds(size) if action == "read" else 300
        port = self._verified_port()
        if self.backend.active or self.backend.hardware_connected or self.active or self.uncertain:
            raise FlashFailure("flash_reader_handoff_unconfirmed")
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="flash-op-", dir=self.work_root))
        payload: dict[str, Any] = {
            "action": action,
            "port": port,
            "flash_bytes": self.registration.flash_bytes,
            "expected_device_sha256": self.device_sha256,
            "esptool_directory": str(self.registration.esptool_directory),
            "expected_python_version": self.registration.python_tool.version,
        }
        if action == "read":
            payload.update(offset=offset, size=size)
        elif action == "write":
            records = []
            for i, (position, data) in enumerate(segments):
                name = f"segment-{i}.bin"
                (temporary / name).write_bytes(data)
                records.append(
                    dict(
                        offset=position,
                        name=name,
                        bytes=len(data),
                        sha256=hashlib.sha256(data).hexdigest(),
                    )
                )
            payload["segments"] = records
        job = temporary / "job.json"
        job.write_text(json.dumps(payload), encoding="utf-8")
        if os.name == "posix":
            job.chmod(0o600)
        lines: list[str] = []
        read_progress = ReadProgress(size, native_seconds) if action == "read" else None
        worker_failure: dict[str, str] | None = None

        def read_diagnostic(code: str, origin: str = "driver_or_process") -> dict[str, Any] | None:
            if read_progress is None:
                return None
            return {
                "code": code,
                "operation": "rom_read",
                **read_progress.snapshot(),
                **(worker_failure or {"failure_origin": origin}),
            }

        if read_progress is not None and self.progress_log is not None:
            self.progress_log(
                f"info: ROM read start {size} bytes; hard deadline {native_seconds} s; idle 60 s"
            )

        def monitor() -> None:
            if self._stop.is_set():
                raise FlashFailure("flash_cancelled_needs_review")
            self._authority()
            if read_progress is not None:
                read_progress.check()

        def output(line: str, stream: str) -> None:
            if (
                stream == "stdout"
                and read_progress is not None
                and line.startswith("FLASH_PROGRESS=")
            ):
                message = read_progress.accept(line.removeprefix("FLASH_PROGRESS="))
                if message is not None and self.progress_log is not None:
                    self.progress_log(message)
            elif stream == "stdout" and line.startswith("FLASH_RESULT=") and len(lines) < 2:
                lines.append(line.removeprefix("FLASH_RESULT="))

        env = {key.upper(): value for key, value in os.environ.items() if key.upper() in OS_ENV}
        worker = Path(__file__).with_name("flash_worker.py")
        try:
            code = self.process.run(
                [
                    str(self.registration.python_tool.executable),
                    "-I",
                    "-B",
                    "-X",
                    f"pycache_prefix={temporary / 'pycache'}",
                    str(worker),
                    str(job),
                ],
                env,
                temporary,
                output,
                timeout=native_seconds + 1 if read_progress is not None else 300,
                monitor=monitor if read_progress is not None else None,
            )
            if read_progress is not None:
                if self.progress_log is not None:
                    self.progress_log(
                        f"info: ROM read native exit {code}; {read_progress.summary()}"
                    )
                monitor()
            if code or len(lines) != 1:
                if len(lines) == 1:
                    worker_failure = _worker_read_failure(lines[0])
                raise FlashFailure("flash_native_failed_needs_review")
            result = json.loads(lines[0])
            if result.get("ok") is not True:
                worker_failure = _worker_read_failure(lines[0])
                raise FlashFailure("flash_native_failed_needs_review")
            read_data: bytes | None = None
            if action == "read":
                path = temporary / "read.bin"
                if path.is_symlink() or path.stat().st_size != size:
                    raise FlashFailure("flash_native_read_incomplete")
                read_data = path.read_bytes()
                if len(read_data) != size or hashlib.sha256(read_data).hexdigest() != result.get(
                    "sha256"
                ):
                    raise FlashFailure("flash_native_read_incomplete")
            return result, read_data
        except FlashFailure as failure:
            if read_progress is not None:
                if failure.diagnostic is None:
                    origin = (
                        "driver_guard"
                        if failure.code
                        in {
                            "flash_cancelled_needs_review",
                            "flash_authority_changed_needs_review",
                            "flash_read_deadline_needs_review",
                            "flash_read_stalled_needs_review",
                            "flash_read_progress_invalid_needs_review",
                        }
                        else "driver_or_process"
                    )
                    failure.diagnostic = read_diagnostic(failure.code, origin)
                if self.progress_log is not None:
                    self.progress_log(
                        f"error: ROM read stopped {failure.code}; {read_progress.summary()}"
                    )
            raise
        except ProcessDeadlineExpired:
            if read_progress is not None:
                if self.progress_log is not None:
                    self.progress_log(
                        f"error: ROM read stopped flash_read_deadline_needs_review; "
                        f"{read_progress.summary()}"
                    )
                raise FlashFailure(
                    "flash_read_deadline_needs_review",
                    read_diagnostic("flash_read_deadline_needs_review", "driver_guard"),
                ) from None
            raise FlashFailure("flash_native_failed_needs_review") from None
        except Exception:
            if read_progress is not None:
                failure_code = (
                    "flash_cancelled_needs_review"
                    if self._stop.is_set() or self.process.stop_requested.is_set()
                    else "flash_native_failed_needs_review"
                )
                if self.progress_log is not None:
                    self.progress_log(
                        f"error: ROM read stopped {failure_code}; {read_progress.summary()}"
                    )
                raise FlashFailure(failure_code, read_diagnostic(failure_code)) from None
            raise FlashFailure("flash_native_failed_needs_review") from None
        finally:
            env.clear()
            if not self.process.active and not self.process.uncertain:
                shutil.rmtree(temporary)

    def probe(self) -> SecurityReceipt:
        result, _ = self._invoke("probe")
        receipt = SecurityReceipt(
            **{
                key: result[key]
                for key in (
                    "device_sha256",
                    "flash_bytes",
                    "chip",
                    "secure_boot",
                    "flash_encryption",
                    "secure_download",
                )
            }
        )
        receipt.verify(None, self.registration.flash_bytes)
        self.device_sha256 = receipt.device_sha256
        return receipt

    def read(self, offset: int, size: int) -> bytes:
        _, data = self._invoke("read", offset=offset, size=size)
        if data is None:
            raise FlashFailure("flash_native_read_incomplete")
        return data

    def write(self, segments: tuple[tuple[int, bytes], ...]) -> None:
        self._invoke("write", segments=segments)

    def boot(self) -> dict[str, Any]:
        self.last_observation = None
        self.last_boot_attempt = None
        if self._observation_backend is not None:
            self._observation_backend.restart_observation_elapsed_ms = None
        started_utc, started = datetime.now(UTC), time.monotonic()
        reason = "boot_failed"
        primary: str | None = None
        try:
            result = self._boot()
            reason = "boot_observation_completed"
            return result
        except SerialFailure as failure:
            self.last_observation = failure.observation
            failure.observation = None
            reason, primary = failure.code, failure.primary_failure_code
            raise
        except FlashFailure as failure:
            reason = failure.code
            raise
        finally:
            elapsed = getattr(self._observation_backend, "restart_observation_elapsed_ms", None)
            self.last_boot_attempt = BootAttempt(
                started_utc, datetime.now(UTC),
                max(0, min(86400000, int((time.monotonic() - started) * 1000))),
                elapsed, reason, primary,
            )

    def _boot(self) -> dict[str, Any]:
        self._verified_port()
        plan = {"test": TestName.RESTART.value, **self.backend.prepare(TestName.RESTART)}
        if plan["device_fingerprint"] != self.expected_usb:
            raise FlashFailure("flash_boot_device_changed")
        self._observation_backend = SerialBackend(
            self.backend.config,
            inventory=self.backend.inventory,
            factory=lambda: GuardedObservationPort(self.backend.factory(), self._authority),
            clock=self.backend.clock,
            pause=self.backend.pause,
        )
        result = asyncio.run(self._observation_backend.execute(plan, lambda _: None))
        if not isinstance(result, BackendResult) or result.observation is None:
            raise FlashFailure("flash_boot_observation_missing")
        self.last_observation = result.observation
        metadata = result.observation.firmware.metadata()
        if boot_verification_reason(result.observation, self.expected_elf_sha256) != "elf_idf_match":
            raise FlashFailure("flash_boot_unverified_needs_review")
        self._authority()
        return {
            "boot_observed": metadata["status"] == "observed",
            "idf_version": (metadata["idf_version"] or "").removeprefix("v"),
            "target": "esp32s3",
            "elf_sha256_prefix": metadata["elf_sha256_prefix"],
        }

    def stop(self) -> None:
        self._stop.set()
        self.process.stop_requested.set()

    def close(self) -> None:
        if self.active or self.uncertain:
            raise FlashFailure("flash_shutdown_uncertain_needs_review")
