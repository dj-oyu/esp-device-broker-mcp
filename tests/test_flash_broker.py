"""Actual broker approval/store/thread handoff with an injected ROM fake, no USB."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
try:
    import filelock  # noqa: F401
except ModuleNotFoundError:
    raise unittest.SkipTest("locked broker dependencies required") from None

from test_flash_lifecycle import FakeDriver

from cardputer_mcp.broker import Broker, BrokerError
from cardputer_mcp.config import Settings
from cardputer_mcp.flash_models import FlashDevice
from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest, TestName
from cardputer_mcp.plan_approval import PlanApprovals


class FlashBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registration = FlashDevice(
            selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
            flash_bytes=2097152,
            project_id="fixture",
            python_tool={
                "executable": self.root / "python.exe",
                "sha256": "a" * 64,
                "version": "Python 3.13.16",
            },
            esptool_directory=self.root / "tools",
            esptool_sha256="b" * 64,
            backup_directory=self.root / "backup",
            private_backup_acl_confirmed=True,
            allow_backup=True,
        )
        self.broker = Broker(
            Settings(
                state_dir=self.root / "state",
                allow_mock_execution=True,
                flash_devices={"fixture": self.registration},
            ),
            lock_path=self.root / "device.lockfile",
        )
        self.driver = FakeDriver()
        self.driver.data = bytearray(b"x" * self.registration.flash_bytes)
        from dataclasses import replace

        self.driver.security = replace(
            self.driver.security, flash_bytes=self.registration.flash_bytes
        )
        self.broker.firmware_flash.driver_factory = lambda *args: self.driver
        await self.broker.start()
        self.token = self.broker.acquire(AcquireRequest(owner=Origin.LOCAL), principal_id="alice")[
            "session"
        ]

    async def asyncTearDown(self):
        await self.broker.close()
        self.temp.cleanup()

    async def test_backup_requires_exact_local_approval_even_with_mock_auto_enabled(self):
        result = self.broker.prepare(
            PrepareRequest(
                session=self.token, job_id="backup-one", test=TestName.BACKUP, device_id="fixture"
            )
        )
        self.assertTrue(result["approval_required"])
        request = ExecuteRequest(session=self.token, plan_hash=result["plan_hash"])
        with self.assertRaisesRegex(BrokerError, "operator_approval_required"):
            self.broker.execute(request)
        approvals = PlanApprovals(self.broker)
        detail = approvals.detail(result["plan_hash"])
        self.assertEqual(detail["attestation"], "flash_device_read_verified")
        with self.assertRaises(ValueError):
            approvals.approve(result["plan_hash"], detail["review_revision"], "wrong")
        approvals.approve(
            result["plan_hash"],
            detail["review_revision"],
            "REGISTERED DEVICE VERIFIED FOR THIS BACKUP READ",
        )
        self.broker.execute(request)
        await self.broker.queue.join()
        self.assertEqual(self.broker.job("backup-one")["state"], "succeeded")
        self.assertEqual(self.driver.writes, 0)
        self.assertTrue(self.driver.closed)
        self.assertTrue(self.broker.execute(request)["deduplicated"])
        receipt, data = self.broker.firmware_flash._backup(self.registration, "backup-one", "alice")
        self.assertEqual(receipt.flash_bytes, len(data))
        with self.assertRaises(ValueError):
            self.broker.firmware_flash._backup(self.registration, "backup-one", "bob")

    async def test_registration_default_disables_flash_and_restore(self):
        self.assertFalse(self.registration.allow_flash)
        self.assertFalse(self.registration.allow_restore)
        with self.assertRaisesRegex(BrokerError, "not_enabled"):
            self.broker.prepare(
                PrepareRequest(
                    session=self.token,
                    job_id="flash-one",
                    test=TestName.FLASH,
                    device_id="fixture",
                    build_job_id="build-one",
                    backup_job_id="backup-one",
                )
            )
