"""Fixed internal external-Python worker; no shell/remote command interface.

Only a registered Windows toolchain executes this script. Unit tests inject fake
esptool objects. It does not run at import time and never prints native output.
"""

import contextlib
import hashlib
import io
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any


class Discard(io.TextIOBase):
    def write(self, text: str) -> int:
        return len(text)


def _inside(root: Path, name: str) -> Path:
    if re.fullmatch(r"(?:segment-[0-9]+|read)\.bin", name) is None:
        raise ValueError("invalid_internal_flash_file")
    path = root / name
    if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
        raise ValueError("linked_internal_flash_file")
    return path


def run_job(job: dict[str, Any], root: Path, tool: Any) -> dict[str, Any]:
    if tool.__version__ != "5.1.0" or job.get("action") not in ("probe", "read", "write"):
        raise ValueError("unsupported_external_flash_tool")
    port = job.get("port")
    capacity = job.get("flash_bytes")
    if not isinstance(port, str) or re.fullmatch(r"COM[1-9][0-9]{0,3}", port) is None:
        raise ValueError("registered_windows_port_required")
    if type(capacity) is not int or capacity not in (2097152, 4194304, 8388608, 16777216):
        raise ValueError("verified_flash_capacity_required")
    if job["action"] == "read":
        offset, size = job.get("offset"), job.get("size")
        if (
            type(offset) is not int
            or type(size) is not int
            or not 0 <= offset < capacity
            or not 0 < size <= min(capacity, 0x1000000) - offset
        ):
            raise ValueError("invalid_internal_read_range")
    elif job["action"] == "write":
        records = job.get("segments")
        if not isinstance(records, list) or not 1 <= len(records) <= 16:
            raise ValueError("invalid_internal_write_map")
        for record in records:
            offset, size = record.get("offset"), record.get("bytes")
            if (
                type(offset) is not int
                or type(size) is not int
                or offset < 0
                or offset % 4096
                or not 0 < size <= min(capacity, 0x1000000) - offset
            ):
                raise ValueError("unsafe_internal_write_range")
    esp = None
    with contextlib.redirect_stdout(Discard()), contextlib.redirect_stderr(Discard()):
        try:
            esp = tool.cmds.detect_chip(port, connect_mode="default-reset", connect_attempts=1)
            if esp.CHIP_NAME != "ESP32-S3" or esp.IS_STUB is not False:
                raise ValueError("flash_target_mismatch")
            # esptool defaults can reconnect and replay a write, and retry blocks.
            # This isolated ROM worker must make exactly one attempt at each.
            esp.WRITE_FLASH_ATTEMPTS = 1
            tool.loader.WRITE_BLOCK_ATTEMPTS = 1
            security = esp.get_security_info(cache=False)
            flags = security.get("parsed_flags", {})
            count = security.get("flash_crypt_cnt")
            if (
                flags.get("SECURE_BOOT_EN") is not False
                or flags.get("SECURE_DOWNLOAD_ENABLE") is not False
                or type(count) is not int
                or not 0 <= count <= 255
                or count.bit_count() % 2 != 0
                or esp.secure_download_mode is not False
            ):
                raise ValueError("device_security_not_verified")
            tool.cmds.attach_flash(esp)
            flash_id = esp.flash_id()
            if type(flash_id) is not int or (1 << ((flash_id >> 16) & 255)) != capacity:
                raise ValueError("flash_capacity_mismatch")
            device = hashlib.sha256(b"esp32s3\0" + bytes(esp.read_mac())).hexdigest()
            expected = job.get("expected_device_sha256")
            if expected is not None and device != expected:
                raise ValueError("flash_device_changed")
            result = {
                "device_sha256": device,
                "flash_bytes": capacity,
                "chip": "esp32s3",
                "secure_boot": False,
                "flash_encryption": False,
                "secure_download": False,
            }
            if job["action"] == "read":
                offset, size = job.get("offset"), job.get("size")
                if (
                    type(offset) is not int
                    or type(size) is not int
                    or not 0 <= offset < capacity
                    or not 0 < size <= capacity - offset
                ):
                    raise ValueError("invalid_internal_read_range")
                data = tool.cmds.read_flash(
                    esp,
                    offset,
                    size,
                    output=None,
                    flash_size=f"{capacity // 1048576}MB",
                    no_progress=True,
                )
                if not isinstance(data, bytes) or len(data) != size:
                    raise ValueError("flash_read_incomplete")
                output = root / "read.bin"
                flags_open = (
                    os.O_CREAT
                    | os.O_EXCL
                    | os.O_WRONLY
                    | getattr(os, "O_BINARY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(output, flags_open, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                result.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            elif job["action"] == "write":
                if expected is None:
                    raise ValueError("device_bound_write_required")
                records = job.get("segments")
                if not isinstance(records, list) or not 1 <= len(records) <= 16:
                    raise ValueError("invalid_internal_write_map")
                segments = []
                previous_end = 0
                for record in records:
                    offset, name, size, digest = (
                        record["offset"],
                        record["name"],
                        record["bytes"],
                        record["sha256"],
                    )
                    if (
                        type(offset) is not int
                        or type(size) is not int
                        or offset % 4096
                        or offset < previous_end
                        or not 0 < size <= capacity - offset
                    ):
                        raise ValueError("unsafe_internal_write_range")
                    path = _inside(root, name)
                    if path.stat().st_size != size:
                        raise ValueError("internal_write_size_mismatch")
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError("internal_write_hash_mismatch")
                    if offset == 0 and (
                        len(data) < 24
                        or data[0] != 0xE9
                        or int.from_bytes(data[12:14], "little") != 9
                    ):
                        raise ValueError("esp32s3_boot_image_required")
                    segments.append((offset, data))
                    previous_end = ((offset + size + 4095) // 4096) * 4096
                # ROM-only; no stub upload, erase-all, force, encryption or eFuse operation.
                # Keep the attested image header unchanged for exact readback verification.
                tool.cmds.write_flash(
                    esp,
                    segments,
                    flash_freq="keep",
                    flash_mode="keep",
                    flash_size="keep",
                    erase_all=False,
                    encrypt=False,
                    force=False,
                    compress=False,
                )
            return result
        finally:
            if esp is not None:
                esp._port.close()
                if esp._port.is_open:
                    raise ValueError("flash_port_close_uncertain")


def main() -> None:
    try:
        path = Path(sys.argv[1])
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 32768:
            raise ValueError
        job = json.loads(path.read_text("utf-8"))
        if "Python " + platform.python_version() != job["expected_python_version"]:
            raise ValueError
        import esptool  # type: ignore[import-not-found]  # Registered external SDK only.
        import esptool.cmds  # type: ignore[import-not-found]

        if Path(esptool.__file__).resolve().parent != Path(job["esptool_directory"]).resolve():
            raise ValueError
        result = run_job(job, path.parent, esptool)
        print("FLASH_RESULT=" + json.dumps({"ok": True, **result}, sort_keys=True), flush=True)
    except Exception:
        print('FLASH_RESULT={"ok":false,"error":"flash_native_failed_needs_review"}', flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
