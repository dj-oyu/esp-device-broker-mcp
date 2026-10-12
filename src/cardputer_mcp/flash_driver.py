"""Registered Windows ROM-only driver. Constructed only for approved execution."""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from .backend import BackendResult
from .build_process import BuildProcess
from .firmware_build import OS_ENV, hash_file
from .flash_lifecycle import FlashFailure, SecurityReceipt
from .models import TestName
from .ports import resolve_port
from .serial_backend import fingerprint


def esptool_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    count = total = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
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


class NativeFlashDriver:
    def __init__(self, registration: Any, backend: Any, expected_usb: str, work_root: Path) -> None:
        self.registration = registration
        self.backend = backend
        self.expected_usb = expected_usb
        self.work_root = work_root
        self.process = BuildProcess()
        self.device_sha256: str | None = None
        self._stop = threading.Event()
        self.last_observation: Any = None

    @property
    def active(self) -> bool:
        return bool(self.process.active or self.backend.active or self.backend.hardware_connected)

    @property
    def uncertain(self) -> bool:
        return bool(self.process.uncertain or getattr(self.backend, "ownership_uncertain", False))

    def _verified_port(self) -> str:
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
            or hash_file(self.registration.python_tool.executable) != self.registration.python_tool.sha256
            or esptool_fingerprint(self.registration.esptool_directory) != self.registration.esptool_sha256
        ):
            raise FlashFailure("registered_flash_tools_changed")
        return port.port

    def _invoke(self, action: str, *, offset: int = 0, size: int = 0,
                segments: tuple[tuple[int, bytes], ...] = ()) -> tuple[dict[str, Any], bytes | None]:
        port = self._verified_port()
        if self.backend.active or self.backend.hardware_connected or self.active or self.uncertain:
            raise FlashFailure("flash_reader_handoff_unconfirmed")
        self.work_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="flash-op-", dir=self.work_root))
        payload: dict[str, Any] = {
            "action": action, "port": port, "flash_bytes": self.registration.flash_bytes,
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
                records.append(dict(offset=position, name=name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
            payload["segments"] = records
        job = temporary / "job.json"
        job.write_text(json.dumps(payload), encoding="utf-8")
        if os.name == "posix":
            job.chmod(0o600)
        lines: list[str] = []
        def output(line: str, stream: str) -> None:
            if stream == "stdout" and line.startswith("FLASH_RESULT=") and len(lines) < 2:
                lines.append(line.removeprefix("FLASH_RESULT="))
        env = {key.upper(): value for key, value in os.environ.items() if key.upper() in OS_ENV}
        worker = Path(__file__).with_name("flash_worker.py")
        try:
            code = self.process.run(
                [str(self.registration.python_tool.executable), "-I", "-B", "-X",
                 f"pycache_prefix={temporary / 'pycache'}", str(worker), str(job)],
                env, temporary, output, timeout=300,
            )
            if code or len(lines) != 1:
                raise FlashFailure("flash_native_failed_needs_review")
            result = json.loads(lines[0])
            if result.get("ok") is not True:
                raise FlashFailure("flash_native_failed_needs_review")
            data = None
            if action == "read":
                path = temporary / "read.bin"
                if path.is_symlink() or path.stat().st_size != size:
                    raise FlashFailure("flash_native_read_incomplete")
                data = path.read_bytes()
                if len(data) != size or hashlib.sha256(data).hexdigest() != result.get("sha256"):
                    raise FlashFailure("flash_native_read_incomplete")
            return result, data
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
        receipt = SecurityReceipt(**{key: result[key] for key in (
            "device_sha256", "flash_bytes", "chip", "secure_boot", "flash_encryption", "secure_download")})
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
        plan = self.backend.prepare(TestName.RESTART)
        if plan["device_fingerprint"] != self.expected_usb:
            raise FlashFailure("flash_boot_device_changed")
        result = asyncio.run(self.backend.execute(plan, lambda _: None))
        if not isinstance(result, BackendResult) or result.observation is None:
            raise FlashFailure("flash_boot_observation_missing")
        self.last_observation = result.observation
        metadata = result.observation.firmware.metadata()
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
