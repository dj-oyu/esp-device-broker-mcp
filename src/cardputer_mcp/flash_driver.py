"""Registered Windows ROM-only driver. Constructed only for approved execution."""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .backend import BackendResult
from .build_process import BuildProcess
from .firmware_build import OS_ENV, hash_file
from .flash_lifecycle import FlashFailure, SecurityReceipt
from .flash_read_progress import ReadProgress
from .models import TestName
from .ports import resolve_port
from .serial_backend import SerialBackend, fingerprint


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
        read_progress = ReadProgress(size, 300) if action == "read" else None

        def monitor() -> None:
            self._authority()
            if read_progress is not None:
                read_progress.check()

        def output(line: str, stream: str) -> None:
            if stream == "stdout" and read_progress is not None and line.startswith("FLASH_PROGRESS="):
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
                timeout=300,
                monitor=monitor if read_progress is not None else None,
            )
            if read_progress is not None:
                monitor()
            if code or len(lines) != 1:
                raise FlashFailure("flash_native_failed_needs_review")
            result = json.loads(lines[0])
            if result.get("ok") is not True:
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
        except FlashFailure:
            raise
        except Exception:
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
        prefix = metadata.get("elf_sha256_prefix")
        if (
            metadata.get("status") != "observed"
            or (metadata.get("idf_version") or "").removeprefix("v") != "6.0.1"
            or not isinstance(prefix, str)
            or not 8 <= len(prefix) <= 16
            or any(c not in "0123456789abcdef" for c in prefix)
            or self.expected_elf_sha256 is None
            or not self.expected_elf_sha256.startswith(prefix)
        ):
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
