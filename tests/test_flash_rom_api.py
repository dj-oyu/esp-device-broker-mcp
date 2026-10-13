"""Optional pinned esptool 5.1 ROM API regression with a fake command transport."""

import itertools
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import esptool
    import esptool.cmds
    from esptool.targets.esp32s3 import ESP32S3ROM
except ModuleNotFoundError:
    raise unittest.SkipTest("run in the separate registered esptool 5.1 environment") from None
if esptool.__version__ != "5.1.0":
    raise unittest.SkipTest("esptool 5.1.0 required; no SDK fallback")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_worker import run_job


class PinnedROMReadTests(unittest.TestCase):
    def test_actual_rom_64_byte_reads_forward_safe_progress_without_stub(self):
        commands = []

        class ROM(ESP32S3ROM):
            def __init__(self):
                self._port = SimpleNamespace(is_open=True)
                self._port.close = lambda: setattr(self._port, "is_open", False)
                self.secure_download_mode = False
                self.params = []

            def get_security_info(self, cache=False):
                return {"parsed_flags": {"SECURE_BOOT_EN": False,
                                         "SECURE_DOWNLOAD_ENABLE": False}, "flash_crypt_cnt": 0}

            def flash_id(self):
                return 21 << 16

            def read_mac(self):
                return (1, 2, 3, 4, 5, 6)

            def flash_set_parameters(self, size):
                self.params.append(size)

            def check_command(self, label, op, data, *, resp_data_len):
                if op != self.ESP_CMDS["READ_FLASH_SLOW"] or resp_data_len != 64:
                    raise AssertionError("only the ROM read command is allowed")
                commands.append(struct.unpack("<II", data))
                return b"x" * 64

        chip = ROM()
        tool = SimpleNamespace(
            __version__=esptool.__version__, loader=esptool.loader,
            cmds=SimpleNamespace(detect_chip=lambda *args, **kwargs: chip,
                                 attach_flash=lambda chip: None,
                                 read_flash=esptool.cmds.read_flash),
        )
        updates = []
        clock = itertools.count(0, 6)
        with tempfile.TemporaryDirectory() as temp, patch(
            "cardputer_mcp.flash_worker.time.monotonic", side_effect=lambda: next(clock)
        ):
            result = run_job({"action": "read", "port": "COM1", "flash_bytes": 2097152,
                              "expected_device_sha256": None, "offset": 4096, "size": 2048},
                             Path(temp), tool, lambda done, total: updates.append((done, total)))
        self.assertEqual(commands, [(4096 + index * 64, 64) for index in range(32)])
        self.assertEqual(updates, [(1024, 2048), (2048, 2048)])
        self.assertEqual(result["bytes"], 2048)
        self.assertEqual(chip.params, [2097152])
        self.assertFalse(chip.IS_STUB)
        self.assertFalse(chip._port.is_open)


if __name__ == "__main__":
    unittest.main()
