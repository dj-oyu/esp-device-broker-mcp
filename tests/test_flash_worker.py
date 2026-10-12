"""Worker API contract with a fake esptool module, never a serial port."""

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_worker import run_job


class FakePort:
    is_open = True

    def close(self):
        self.is_open = False


class FakeChip:
    CHIP_NAME = "ESP32-S3"
    IS_STUB = False
    secure_download_mode = False

    def __init__(self):
        self._port = FakePort()
        self.flags = {"SECURE_BOOT_EN": False, "SECURE_DOWNLOAD_ENABLE": False}
        self.crypt_count = 0

    def get_security_info(self, cache=False):
        return {"parsed_flags": self.flags, "flash_crypt_cnt": self.crypt_count}

    def flash_id(self):
        return 21 << 16

    def read_mac(self):
        return (1, 2, 3, 4, 5, 6)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.chip = FakeChip()
        self.writes = []
        self.calls = []

        def detect(port, **kwargs):
            self.calls.append((port, kwargs))
            return self.chip

        self.tool = SimpleNamespace(
            __version__="5.1.0",
            loader=SimpleNamespace(WRITE_BLOCK_ATTEMPTS=3),
            cmds=SimpleNamespace(
                detect_chip=detect,
                attach_flash=lambda chip: None,
                read_flash=lambda chip, offset, size, **kwargs: b"x" * size,
                write_flash=lambda chip, data, **kwargs: self.writes.append((data, kwargs)),
            ),
        )
        self.job = {
            "action": "probe",
            "port": "COM1",
            "flash_bytes": 2097152,
            "expected_device_sha256": None,
        }

    def test_probe_returns_only_hashed_identity_and_safe_flags(self):
        result = run_job(self.job, self.root, self.tool)
        self.assertEqual(
            result["device_sha256"],
            hashlib.sha256(b"esp32s3\0" + bytes(self.chip.read_mac())).hexdigest(),
        )
        self.assertEqual(
            self.calls, [("COM1", {"connect_mode": "default-reset", "connect_attempts": 1})]
        )
        self.assertFalse(self.chip._port.is_open)
        self.assertEqual(self.writes, [])
        self.assertEqual(self.chip.WRITE_FLASH_ATTEMPTS, 1)
        self.assertEqual(self.tool.loader.WRITE_BLOCK_ATTEMPTS, 1)

    def test_rom_limit_rejects_32mb_before_device_open(self):
        with self.assertRaisesRegex(ValueError, "capacity_required"):
            run_job(dict(self.job, flash_bytes=33554432), self.root, self.tool)
        self.assertEqual(self.calls, [])

    def test_existing_stub_is_rejected(self):
        self.chip.IS_STUB = True
        with self.assertRaisesRegex(ValueError, "target_mismatch"):
            run_job(self.job, self.root, self.tool)
        self.assertEqual(self.writes, [])

    def test_rom_crossing_read_and_write_rejected_before_device_open(self):
        for job in (
            dict(self.job, action="read", flash_bytes=16777216, offset=0xFFF000, size=8192),
            dict(
                self.job,
                action="write",
                flash_bytes=16777216,
                segments=[{"offset": 0xFFF000, "bytes": 8192}],
            ),
        ):
            with self.assertRaises(ValueError):
                run_job(job, self.root, self.tool)
        self.assertEqual(self.calls, [])

    def test_unknown_protected_or_encrypted_device_blocks_write(self):
        for field, value in (("SECURE_BOOT_EN", True), ("SECURE_DOWNLOAD_ENABLE", None)):
            self.setUp()
            self.chip.flags[field] = value
            self.job["action"] = "probe"
            with self.assertRaisesRegex(ValueError, "security_not_verified"):
                run_job(self.job, self.root, self.tool)
            self.assertEqual(self.writes, [])
            self.assertFalse(self.chip._port.is_open)
        self.setUp()
        self.chip.crypt_count = 1
        with self.assertRaisesRegex(ValueError, "security_not_verified"):
            run_job(self.job, self.root, self.tool)

    def test_network_or_arbitrary_port_and_command_are_rejected(self):
        for key, value in (
            ("port", "rfc2217://example.invalid"),
            ("action", "erase-all"),
            ("action", "burn-efuse"),
        ):
            job = dict(self.job, **{key: value})
            with self.assertRaises(ValueError):
                run_job(job, self.root, self.tool)
        self.assertEqual(self.calls, [])

    def test_read_output_is_private_binary_with_hash_receipt(self):
        result = run_job(dict(self.job, action="read", offset=0, size=128), self.root, self.tool)
        self.assertEqual((self.root / "read.bin").read_bytes(), b"x" * 128)
        self.assertEqual(result["sha256"], hashlib.sha256(b"x" * 128).hexdigest())
        self.assertNotIn("data", result)
        self.assertEqual(self.writes, [])

    def test_write_uses_attested_bytes_and_never_dangerous_options(self):
        data = b"synthetic-image" * 8
        (self.root / "segment-0.bin").write_bytes(data)
        digest = hashlib.sha256(b"esp32s3\0" + bytes(self.chip.read_mac())).hexdigest()
        job = dict(
            self.job,
            action="write",
            expected_device_sha256=digest,
            segments=[
                {
                    "offset": 4096,
                    "name": "segment-0.bin",
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            ],
        )
        run_job(job, self.root, self.tool)
        self.assertEqual(self.writes[0][0], [(4096, data)])
        self.assertEqual(
            self.writes[0][1],
            {
                "flash_freq": "keep",
                "flash_mode": "keep",
                "flash_size": "keep",
                "erase_all": False,
                "encrypt": False,
                "force": False,
                "compress": False,
            },
        )

    def test_mismatched_device_and_non_s3_boot_image_never_write(self):
        job = dict(
            self.job,
            action="write",
            expected_device_sha256="0" * 64,
            segments=[{"offset": 4096, "name": "segment-0.bin", "bytes": 16, "sha256": "a" * 64}],
        )
        with self.assertRaisesRegex(ValueError, "device_changed"):
            run_job(job, self.root, self.tool)
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
