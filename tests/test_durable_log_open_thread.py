"""Build and flash open their durable log on the broker loop, never in the worker thread.

The broker store's SQLite connection belongs to the loop thread; opening the log from
the build/flash worker raised inside the opener, was swallowed, and left the job with
no durable log at all.
"""

import threading

from cardputer_mcp.firmware_build import FirmwareBuild
from cardputer_mcp.firmware_flash import FirmwareFlash


def _opener(seen):
    def open_job_log(job_id, principal_id, kind):
        seen.append((threading.get_ident(), job_id, principal_id, kind))
        return object()

    return open_job_log


async def test_build_opens_its_durable_log_on_the_loop_thread():
    seen: list[tuple[int, str, str, str]] = []
    build = object.__new__(FirmwareBuild)
    build._preopened_logs = {}
    build.open_job_log = _opener(seen)
    build.authority = None
    build._authority_monitor = None
    build._check_cancelled = lambda: None
    workers: list[object] = []

    def run(plan, stage, defer_publish):
        # The worker takes the pre-opened log; it never opens one itself.
        workers.append(build._preopened_logs.get(plan["job_id"]))
        return "firmware_build_succeeded_no_flash"

    build._execute = run
    plan = {"job_id": "b1", "build_principal": "p", "test": "firmware_build"}
    assert await build.execute(plan, lambda name: None) == "firmware_build_succeeded_no_flash"
    assert seen == [(threading.get_ident(), "b1", "p", "firmware_build")]
    assert workers and workers[0] is not None
    assert build._preopened_logs == {}


async def test_flash_opens_its_durable_log_on_the_loop_thread():
    seen: list[tuple[int, str, str, str]] = []
    flash = object.__new__(FirmwareFlash)
    flash._preopened_logs = {}
    flash.open_job_log = _opener(seen)
    taken: list[object] = []

    async def run(plan, stage, live, loop):
        taken.append(flash._preopened_logs.get(plan["job_id"]))
        return "firmware_flash_completed"

    flash._run = run
    plan = {"job_id": "f1", "flash_principal": "p", "test": "firmware_flash"}
    assert await flash.execute(plan, lambda name: None, lambda: True) == "firmware_flash_completed"
    assert seen == [(threading.get_ident(), "f1", "p", "firmware_flash")]
    assert taken and taken[0] is not None
    assert flash._preopened_logs == {}
