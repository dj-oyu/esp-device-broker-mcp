"""A finished device job must not stay `active_job` (issue #132); fakes only, no hardware."""

import asyncio

from test_gui_review import queue
from test_pending_execution import prepare, running, submit

from cardputer_mcp import resource_recheck
from cardputer_mcp.gui_review import recheck_available
from cardputer_mcp.models import LeaseRequest


def busy_profile_build(broker):
    """A registered-project build owns the shared build process, outside the device lane."""
    broker.profile_jobs.active = "profile-build"
    broker.firmware_build.process.active = True


def finish_profile_build(broker):
    broker.firmware_build.process.active = False
    broker.profile_jobs.active = None


async def test_device_job_finishing_during_profile_build_is_released(tmp_path):
    async with running(tmp_path) as (broker, _):
        busy_profile_build(broker)
        try:
            submit(broker, prepare(broker), waiting=False)
            await broker.queue.join()
            assert broker.job("submitted")["state"] == "succeeded"
            assert broker.active_job is None
            assert broker._physical_busy() is False
        finally:
            finish_profile_build(broker)
        assert broker.release(LeaseRequest(session=broker.session.capability))["released"]
        assert broker.session is None


async def test_device_lane_process_left_running_keeps_the_job_active(tmp_path):
    async with running(tmp_path) as (broker, _):
        # No registered-project job: the surviving process belongs to the device lane.
        broker.backend.gate = asyncio.Event()
        submit(broker, prepare(broker), waiting=False)
        while broker.active_job is None:
            await asyncio.sleep(0)
        broker.firmware_build.process.active = True
        try:
            broker.backend.gate.set()
            await broker.queue.join()
            assert broker.job("submitted")["state"] == "succeeded"
            assert broker.active_job == "submitted"
            resource_recheck.recheck(broker)
            assert broker.active_job == "submitted", "an active owned process keeps the job"
        finally:
            broker.firmware_build.process.active = False
            broker.active_job = None


async def test_explicit_recheck_drops_a_stale_active_job_without_any_latch(tmp_path):
    async with running(tmp_path) as (broker, _):
        submit(broker, prepare(broker), waiting=False)
        await broker.queue.join()
        broker.active_job = "submitted"  # the state left behind before the fix
        assert resource_recheck.uncertainty(broker) == []

        broker.profile_jobs.active = "profile-build"
        try:
            result = resource_recheck.recheck(broker)
            assert broker.active_job == "submitted", "a running profile job keeps the lane"
        finally:
            broker.profile_jobs.active = None

        result = resource_recheck.recheck(broker)
        assert broker.active_job is None
        assert result["checked"] == [] and result["remaining"] == []
        assert broker.release(LeaseRequest(session=broker.session.capability))["released"]


def test_gui_offers_recheck_for_a_recorded_active_job():
    assert recheck_available(queue(active_job="submitted"))
    assert not recheck_available(queue(active_job=None))
