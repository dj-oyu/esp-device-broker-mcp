"""Exercise real async build adapter/process cleanup with a gated synthetic reader."""

import asyncio
import io
import threading
from types import SimpleNamespace

import pytest
from test_broker import broker as broker
from test_profile_jobs import register

from cardputer_mcp import build_process
from cardputer_mcp.broker import BrokerError
from cardputer_mcp.models import CancelRequest, ExecuteRequest, Origin, PrepareRequest
from cardputer_mcp.models import TestName as Action


@pytest.mark.parametrize(
    "cleanup_confirmed,interruption",
    [(True, "none"), (False, "none"), (True, "cancel"), (True, "revoke"), (True, "deadline")],
)
async def test_owned_cleanup_crosses_watchdog_without_revoking_build(
    broker, tmp_path, monkeypatch, cleanup_confirmed, interruption
):
    register(SimpleNamespace(resident=broker), tmp_path)
    process = broker.firmware_build.process
    reader_release = threading.Event()
    cleanup_release = threading.Event()
    watched_cleanup = asyncio.Event()
    readers = []
    published = []

    class Pipe(io.BytesIO):
        def read(self, size=-1):
            assert reader_release.wait(4), "test must release its synthetic reader"
            return b""

    class Process:
        pid = 123

        def __init__(self, *args, **kwargs):
            self.stdout = Pipe()
            self.stderr = io.BytesIO()

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    class Reader(threading.Thread):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            readers.append(self)

        def join(self, timeout=None):
            if cleanup_confirmed:
                super().join(timeout)
            else:
                # Simulate an owned reader that cannot yet confirm shutdown.
                assert cleanup_release.wait(4)

    def no_remaining_group(*args):
        raise ProcessLookupError

    monkeypatch.setattr(
        build_process, "os", SimpleNamespace(name="posix", killpg=no_remaining_group)
    )
    monkeypatch.setattr(build_process, "signal", SimpleNamespace(SIGKILL=9))
    monkeypatch.setattr(
        build_process, "subprocess", SimpleNamespace(Popen=Process, DEVNULL=-3, PIPE=-1)
    )
    monkeypatch.setattr(build_process, "threading", SimpleNamespace(Thread=Reader))

    # Keep FirmwareBuild.execute and BuildProcess.run real. Only the build body
    # and OS process boundary are synthetic; no compiler or native process runs.
    def build(plan, stage, defer_publish):
        assert defer_publish
        code = process.run(["synthetic-build"], {}, tmp_path, lambda *_: None)
        assert code == 0
        return "build_completed_no_device_write"

    monkeypatch.setattr(broker.firmware_build, "_execute", build)
    monkeypatch.setattr(
        broker.firmware_build,
        "commit_deferred",
        lambda plan, live: published.append(live()),
    )
    binding = broker.profile_jobs._binding_live

    def observe_watchdog(plan):
        if process.active and process.uncertain:
            watched_cleanup.set()
        return binding(plan)

    monkeypatch.setattr(broker.profile_jobs, "_binding_live", observe_watchdog)
    prepared = broker.profile_jobs.prepare(
        PrepareRequest(job_id="cleanup", test=Action.BUILD, project_id="fixture"),
        "owner",
        Origin.REMOTE.value,
    )
    broker.profile_jobs.execute(
        ExecuteRequest(plan_hash=prepared["plan_hash"]), "owner", Origin.REMOTE.value
    )
    try:
        try:
            # The real 100 ms watchdog must run while run() is in reader.join().
            await asyncio.wait_for(watched_cleanup.wait(), 2)
            assert process.active and process.uncertain
            assert not process.stop_requested.is_set()
            assert not broker.profile_jobs.stops
            if interruption == "cancel":
                broker.profile_jobs.cancel(
                    CancelRequest(job_id="cleanup"), "owner", Origin.REMOTE.value
                )
            elif interruption == "revoke":
                monkeypatch.setattr(broker, "principal_valid", lambda *_: False)
            elif interruption == "deadline":
                broker.profile_jobs.deadline = broker.clock() - 1
            if interruption != "none":
                assert await asyncio.to_thread(process.stop_requested.wait, 2)
        finally:
            cleanup_release.set()
            if cleanup_confirmed:
                reader_release.set()
            await asyncio.wait_for(broker.profile_jobs.queue.join(), 2)
        job = broker.job("cleanup")
        if cleanup_confirmed and interruption == "none":
            assert job["state"] == "succeeded"
            assert published == [True]
            assert not process.active and not process.uncertain
            assert not broker.profile_jobs._uncertain()
            broker.profile_jobs.prepare(
                PrepareRequest(job_id="followup", test=Action.BUILD, project_id="fixture"),
                "owner",
                Origin.REMOTE.value,
            )
        elif not cleanup_confirmed:
            assert job["state"] == "needs_review"
            assert job["result"] == "build_process_exit_unconfirmed"
            assert process.active and process.uncertain
            assert broker.profile_jobs.state_uncertain
            assert broker._physical_busy()
            assert published == []
            with pytest.raises(BrokerError, match="recovery_review_required"):
                broker.profile_jobs.prepare(
                    PrepareRequest(job_id="blocked", test=Action.BUILD, project_id="fixture"),
                    "owner",
                    Origin.REMOTE.value,
                )
        else:
            assert job["state"] == "cancelled"
            assert published == []
            assert not process.active and not process.uncertain
    finally:
        # Dispose synthetic readers only after asserting retained ownership.
        cleanup_release.set()
        reader_release.set()
        for reader in readers:
            threading.Thread.join(reader, 2)
            assert not reader.is_alive()
        process.active = process.uncertain = False
