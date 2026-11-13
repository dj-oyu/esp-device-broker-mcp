"""Loop-owned log opening, worker handoff and cleanup; no device operations."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from cardputer_mcp.firmware_flash import FirmwareFlash
from cardputer_mcp.flash_lifecycle import FlashFailure


class Log:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def fixture():
    flash = object.__new__(FirmwareFlash)
    flash.logs = {}
    flash._packet = lambda plan: ()
    flash.request_stop = lambda job: True
    plan = {"job_id": "synthetic", "flash_principal": "owner"}
    return flash, plan


@pytest.mark.parametrize("attached", [False, True])
async def test_worker_failure_closes_only_unattached_log(attached):
    flash, plan = fixture()
    log = Log()
    loop_thread = threading.get_ident()

    def opener(*args):
        assert threading.get_ident() == loop_thread
        return log

    def worker(plan, stage, live, packet, durable):
        assert threading.get_ident() != loop_thread
        assert durable is log
        if attached:
            flash.logs[plan["job_id"]] = SimpleNamespace(durable=durable)
        raise FlashFailure("synthetic_failure")

    flash.open_job_log, flash._execute = opener, worker
    with pytest.raises(FlashFailure, match="synthetic_failure"):
        await flash.execute(plan, lambda name: None, lambda: True)
    assert log.closed is not attached


@pytest.mark.parametrize("opener", [None, lambda *args: None])
async def test_absent_durable_log_leaves_worker_result_unchanged(opener):
    flash, plan = fixture()
    flash.open_job_log = opener
    flash._execute = lambda plan, stage, live, packet, durable: "synthetic_success"
    assert await flash.execute(plan, lambda name: None, lambda: True) == "synthetic_success"


async def test_failed_log_open_is_best_effort():
    flash, plan = fixture()

    def opener(*args):
        raise OSError("synthetic log failure")

    def worker(plan, stage, live, packet, durable):
        assert durable is None
        return "synthetic_success"

    flash.open_job_log, flash._execute = opener, worker
    assert await flash.execute(plan, lambda name: None, lambda: True) == "synthetic_success"


async def test_packet_rejection_does_not_open_or_execute():
    flash, plan = fixture()

    def packet(plan):
        raise FlashFailure("synthetic_packet_failure")

    def unexpected(*args):
        pytest.fail("not reached before packet verification")

    flash._packet, flash.open_job_log, flash._execute = packet, unexpected, unexpected
    with pytest.raises(FlashFailure, match="synthetic_packet_failure"):
        await flash.execute(plan, lambda name: None, lambda: True)


async def test_cancelled_wrapper_retains_log_until_worker_returns():
    flash, plan = fixture()
    log = Log()
    started, release = threading.Event(), threading.Event()
    stopped = asyncio.Event()
    flash.request_stop = lambda job: stopped.set()
    flash.open_job_log = lambda *args: log

    def worker(plan, stage, live, packet, durable):
        started.set()
        assert release.wait(5)
        assert not durable.closed
        return "synthetic_success"

    flash._execute = worker
    task = asyncio.create_task(flash.execute(plan, lambda name: None, lambda: True))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.wait_for(stopped.wait(), 2)
        assert not log.closed
        release.set()
        with pytest.raises(FlashFailure, match="flash_cancelled_needs_review"):
            await task
        assert log.closed
    finally:
        release.set()
        if not task.done():
            await task
