"""Real broker/store/GUI approval tests with a synthetic non-hardware backend."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path


class BuildApprovalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_serial_and_mock_gui_submission_approval_and_no_replay(self):
        try:
            from cardputer_mcp.broker import Broker, BrokerError
            from cardputer_mcp.config import BuildProject, SerialSettings, Settings
            from cardputer_mcp.gui_approvals import PlanReview, review_warning
            from cardputer_mcp.models import (
                AcquireRequest,
                CancelRequest,
                ExecuteRequest,
                Origin,
                PrepareRequest,
            )
            from cardputer_mcp.plan_approval import PlanApprovals
            from cardputer_mcp.ports import DeviceSelector
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        for backend_name in ("mock", "serial"):
            with self.subTest(backend=backend_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                project = root / "project"
                project.mkdir()
                (project / "CMakeLists.txt").write_text("synthetic source")
                profile = root / "profile.ps1"
                profile.write_text("return")
                serial = (
                    SerialSettings(selector=DeviceSelector(vid=1, pid=2, serial_number="synthetic"))
                    if backend_name == "serial"
                    else None
                )
                settings = Settings(
                    backend=backend_name,
                    state_dir=root / "state",
                    allow_mock_execution=backend_name == "mock",
                    serial=serial,
                    build_projects={
                        "registered": BuildProject(
                            project_dir=project,
                            trusted_profile=profile,
                            build_root=root / "output",
                        )
                    },
                )

                class SyntheticBackend:
                    name = backend_name
                    device = "mock-cardputer" if backend_name == "mock" else "configured-cardputer"
                    active = max_active = 0
                    hardware_connected = False
                    config = serial

                    def prepare(self, test):
                        raise AssertionError("build must not enumerate/open a USB backend")

                    async def execute(self, plan, stage):
                        raise AssertionError("build must not execute a USB backend")

                broker = Broker(
                    settings,
                    backend=SyntheticBackend(),
                    serial_enabled=True,
                    lock_path=root / "device.lock",
                    principal_valid=lambda who, *_: who == "alice",
                )
                await broker.start()
                try:
                    acquired = broker.acquire(
                        AcquireRequest(owner=Origin.REMOTE), principal_id="alice"
                    )
                    lease = acquired["session"]
                    with self.assertRaises(BrokerError):
                        broker.require_owner(lease, Origin.REMOTE.value, principal_id="bob")
                    prepared = broker.prepare(
                        PrepareRequest(
                            session=lease,
                            job_id="buildjob",
                            test="firmware_build",
                            project_id="registered",
                        )
                    )
                    self.assertTrue(prepared["approval_required"])
                    request = ExecuteRequest(
                        session=lease, plan_hash=prepared["plan_hash"], wait_for_approval=True
                    )
                    submitted = broker.execute(request)
                    self.assertEqual("awaiting_approval", submitted["state"])
                    self.assertTrue(broker.execute(request)["deduplicated"])
                    approvals = PlanApprovals(broker)
                    detail = approvals.detail(prepared["plan_hash"])
                    review = PlanReview.parse({"ok": True, **detail}, time.monotonic())
                    warning = review_warning(review)
                    self.assertIn("BUILD executes", warning)
                    self.assertIn("alice", warning)
                    self.assertIn("log", warning)
                    calls = []

                    async def synthetic_build(plan, stage, calls=calls):
                        calls.append(plan["job_id"])
                        return "firmware_build_succeeded_no_flash"

                    broker.firmware_build.execute = synthetic_build
                    approved = approvals.approve(
                        prepared["plan_hash"], detail["review_revision"], ""
                    )
                    self.assertTrue(approved["approved"])
                    await broker.queue.join()
                    self.assertEqual("succeeded", broker.job("buildjob")["state"])
                    self.assertTrue(broker.execute(request)["deduplicated"])
                    self.assertEqual(["buildjob"], calls)
                    # A later explicit build can be cancelled after approval; its
                    # durable outcome must be review, with no success manifest.
                    second = broker.prepare(
                        PrepareRequest(
                            session=lease,
                            job_id="canceljob",
                            test="firmware_build",
                            project_id="registered",
                        )
                    )
                    start = asyncio.Event()
                    finish = asyncio.Event()

                    async def cancellable_build(
                        plan, stage, started=start, finished=finish, resident=broker
                    ):
                        started.set()
                        await finished.wait()
                        resident.firmware_build._check_cancelled()
                        return "firmware_build_succeeded_no_flash"

                    broker.firmware_build.execute = cancellable_build
                    broker.execute(
                        ExecuteRequest(
                            session=lease, plan_hash=second["plan_hash"], wait_for_approval=True
                        )
                    )
                    second_detail = approvals.detail(second["plan_hash"])
                    approvals.approve(second["plan_hash"], second_detail["review_revision"], "")
                    await asyncio.wait_for(start.wait(), timeout=2)
                    cancelled = broker.cancel(CancelRequest(session=lease, job_id="canceljob"))
                    self.assertTrue(cancelled["stop_requested"])
                    finish.set()
                    await broker.queue.join()
                    self.assertEqual("needs_review", broker.job("canceljob")["state"])
                    self.assertNotIn("canceljob", broker.firmware_build.manifests)
                finally:
                    await broker.close()


    async def test_failed_diagnostic_job_keeps_fixed_code_and_private_logs(self):
        from cardputer_mcp.broker import Broker
        from cardputer_mcp.build_logs import BuildLogs
        from cardputer_mcp.config import BuildProject, Settings
        from cardputer_mcp.firmware_build import FixedDiagnosticFailure
        from cardputer_mcp.models import AcquireRequest, ExecuteRequest, Origin, PrepareRequest
        from cardputer_mcp.plan_approval import PlanApprovals

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "CMakeLists.txt").write_text("synthetic")
            profile = root / "profile.ps1"
            profile.write_text("return")
            settings = Settings(
                state_dir=root / "state",
                build_projects={
                    "registered": BuildProject(
                        project_dir=project,
                        trusted_profile=profile,
                        build_root=root / "output",
                    )
                },
            )
            broker = Broker(
                settings,
                lock_path=root / "device.lock",
                principal_valid=lambda who, *_: who == "alice",
            )
            await broker.start()
            try:
                lease = broker.acquire(
                    AcquireRequest(owner=Origin.REMOTE), principal_id="alice"
                )["session"]
                prepared = broker.prepare(
                    PrepareRequest(
                        session=lease,
                        job_id="diagnostic-failure",
                        test="firmware_build",
                        project_id="registered",
                    )
                )

                async def failed_build(plan, stage):
                    stage("idf_diagnostics")
                    logs = BuildLogs("alice")
                    logs.append("error: Windows- unsupported token=synthetic-secret", "stderr")
                    broker.firmware_build.logs[plan["job_id"]] = logs
                    raise FixedDiagnosticFailure("idf_version_diagnostic_failed")

                broker.firmware_build.execute = failed_build
                broker.execute(
                    ExecuteRequest(
                        session=lease, plan_hash=prepared["plan_hash"], wait_for_approval=True
                    )
                )
                approvals = PlanApprovals(broker)
                detail = approvals.detail(prepared["plan_hash"])
                approvals.approve(prepared["plan_hash"], detail["review_revision"], "")
                await broker.queue.join()
                job = broker.job("diagnostic-failure")
                self.assertEqual(job["state"], "needs_review")
                self.assertEqual(job["diagnostic"], {"code": "idf_version_diagnostic_failed"})
                self.assertIn("idf_version_diagnostic_failed", str(job))
                self.assertNotIn("synthetic-secret", str(job))
                self.assertFalse(broker.firmware_build.manifests)
                self.assertEqual(
                    broker._store().job("diagnostic-failure")["diagnostic"],
                    {"code": "idf_version_diagnostic_failed"},
                )
            finally:
                await broker.close()
