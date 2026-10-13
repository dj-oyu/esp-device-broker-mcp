"""Synthetic partition receipts; no physical backup or personal device data."""

import hashlib
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from cardputer_mcp.flash_lifecycle import FlashFailure
from cardputer_mcp.flash_lkg_geometry import factory_layout


def table(app_subtype=0, flags=0):
    rows = b"".join(
        struct.pack("<2sBBLL16sL", b"\xaa\x50", *row)
        for row in (
            (1, 2, 0x9000, 0x6000, b"nvs", 0),
            (1, 1, 0xF000, 0x1000, b"phy", 0),
            (0, app_subtype, 0x10000, 0x300000, b"factory", flags),
        )
    )
    raw = rows + b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(rows).digest()
    return raw + b"\xff" * (3072 - len(raw))


class GeometryTests(unittest.TestCase):
    def test_factory_bounds_leave_nvs_outside_firmware(self):
        result = factory_layout(table(), table_offset=0x8000, flash_bytes=8388608)
        self.assertEqual((result.app_offset, result.app_bytes), (0x10000, 0x300000))

    def test_duplicate_partition_label_is_rejected(self):
        raw = bytearray(table())
        raw[32 + 12:32 + 28] = raw[12:28]
        raw[96 + 16:96 + 32] = hashlib.md5(raw[:96]).digest()
        with self.assertRaisesRegex(FlashFailure, "label_invalid"):
            factory_layout(bytes(raw), table_offset=0x8000, flash_bytes=8388608)

    def test_unknown_ota_encryption_and_bad_checksum_fail_closed(self):
        for data in (table(16), table(flags=1), table()[:96] + b"x" + table()[97:]):
            with self.assertRaises(FlashFailure):
                factory_layout(data, table_offset=0x8000, flash_bytes=8388608)


if __name__ == "__main__":
    unittest.main()
