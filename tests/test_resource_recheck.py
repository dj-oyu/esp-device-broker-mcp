"""Explicit operator re-check of runtime uncertainty latches; fakes only, no hardware."""

import threading
from types import SimpleNamespace

import pytest
from test_home_recovery import running as serial_running
from test_pending_execution import running

from cardputer_mcp import resource_recheck
from cardputer_mcp.broker import digest_plan
from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.config import SerialSettings
from cardputer_mcp.flash_lifecycle import FlashLifecycle
from cardputer_mcp.serial_backend import SerialBackend
from cardputer_mcp.windows_process import WindowsProcessError

# --- lowest layer: each component's own check ------------------------------------------


class Owner:
    def __init__(self, exited: bool) -> None:
        self.exited, self.closes, self.stops = exited, 0, 0

    def close(self) -> None:
        self.closes += 1
        if not self.exited:
            raise WindowsProcessError("owned_job_still_active")

    def stop(self, timeout: float = 5.0) -> bool:  # pragma: no cover - must never run
        self.stops += 1
        raise AssertionError("a re-check never terminates a process")


class Pipe:
    closed = False

    def close(self) -> None:
        self.closed = True


def uncertain_process(owner=None, readers=(), pipes=()) -> BuildProcess:
    process = BuildProcess()
    process.uncertain = process.active = True
    process.owner = owner
    process._retained = (list(readers), list(pipes))
    return process


def test_build_process_without_latch_reports_not_set():
    assert BuildProcess().recheck_exited() == (True, "not_set")


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("no_owner", (False, "process_tree_unverifiable")),
        ("cleanup_pending", (False, "process_cleanup_pending")),
        ("reader_alive", (False, "process_output_reader_alive")),
        ("tree_active", (False, "process_tree_still_active")),
        ("exited", (True, "process_tree_exit_confirmed")),
    ],
)
def test_build_process_recheck_table(case, expected):
    owner = None if case == "no_owner" else Owner(exited=case != "tree_active")
    release = threading.Event()
    reader = threading.Thread(target=release.wait, daemon=True)
    if case == "reader_alive":
        reader.start()
    pipe = Pipe()
    process = uncertain_process(owner, [reader] if case == "reader_alive" else [], [pipe])
    process.cleanup_pending = case == "cleanup_pending"
    try:
        assert process.recheck_exited() == expected
    finally:
        release.set()
    cleared = expected[0]
    assert process.uncertain is not cleared and process.active is not cleared
    assert pipe.closed is cleared
    assert (process.owner is None) is (cleared or owner is None)
    assert owner is None or owner.stops == 0


class Port:
    def __init__(self, *, is_open: bool, close_fails: bool = False) -> None:
        self.is_open, self.close_fails, self.closes = is_open, close_fails, 0

    def close(self) -> None:
        self.closes += 1
        if self.close_fails:
            raise OSError("synthetic private close failure")
        self.is_open = False


def serial_backend() -> SerialBackend:
    def never_open():
        raise AssertionError("a re-check never opens a port")

    config = SerialSettings(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        expected_firmware_sha256="a" * 64,
    )
    return SerialBackend(config, inventory=lambda: [], factory=never_open)


@pytest.mark.parametrize(
    ("port", "expected", "closes"),
    [
        (None, (False, "serial_handle_unknown"), 0),
        (Port(is_open=False), (True, "serial_close_confirmed"), 0),
        (Port(is_open=True), (True, "serial_close_confirmed"), 1),
        (Port(is_open=True, close_fails=True), (False, "serial_close_unconfirmed"), 1),
    ],
)
def test_serial_recheck_uses_only_the_confirmed_close_helper(port, expected, closes):
    backend = serial_backend()
    assert backend.recheck_closed() == (True, "not_set")
    backend.ownership_uncertain = backend.hardware_connected = True
    backend.active = 1
    backend._unclosed_port = port
    assert backend.recheck_closed() == expected
    cleared = expected[0]
    assert backend.ownership_uncertain is not cleared
    assert backend.hardware_connected is not cleared
    assert backend.active == (0 if cleared else 1)
    assert (backend._unclosed_port is None) is (cleared or port is None)
    if port is not None:
        assert port.closes == closes


class Driver:
    def __init__(self, closable: bool) -> None:
        self.closable, self.closes = closable, 0
        self.active = self.uncertain = False

    def close(self) -> None:
        self.closes += 1
        if not self.closable:
            raise RuntimeError("synthetic")

    def write(self, segments):  # pragma: no cover - must never run
        raise AssertionError("a re-check never writes")


@pytest.mark.parametrize("closable", [True, False])
def test_flash_shutdown_recheck_uses_the_driver_confirmed_close(closable):
    engine = FlashLifecycle(Driver(closable))  # type: ignore[arg-type]
    assert engine.recheck_shutdown() == (True, "not_set")
    engine.uncertain = engine.active = True
    expected = (
        (True, "flash_shutdown_confirmed") if closable else (False, "flash_driver_still_owned")
    )
    assert engine.recheck_shutdown() == expected
    assert engine.uncertain is not closable and engine.active is not closable


# --- broker table: flag x check pass/fail -> cleared/kept --------------------------------


def checked(result):
    return {row["flag"]: (row["outcome"], row["reason"]) for row in result["checked"]}


def needs_review_job(broker, job_id="review-row"):
    store = broker._store()
    plan = {"version": 1, "job_id": job_id, "test": "firmware_observe"}
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    store.add_job(job_id, digest)
    store.set_job(job_id, "needs_review", "serial_close_unconfirmed", {"code": "x"})
    return dict(store.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())


def reset(broker):
    process = broker.firmware_build.process
    process.uncertain = process.active = False
    process.owner = None
    broker.firmware_build.output_uncertain = False
    broker.firmware_source.snapshots.uncertain = False
    profile = broker.profile_jobs
    profile.uncertain = profile.state_uncertain = False
    broker.execution_uncertain = False
    broker._worker_job = None


async def test_nothing_set_checks_nothing(tmp_path):
    async with running(tmp_path) as (broker, _):
        result = resource_recheck.recheck(broker)
        assert result["checked"] == [] and result["remaining"] == []
        assert result["broker_epoch"] == broker.epoch
        assert broker.status()["safety"]["uncertainty"] == []


@pytest.mark.parametrize("passes", [True, False])
async def test_build_process_flag(tmp_path, passes):
    async with running(tmp_path) as (broker, _):
        process = broker.firmware_build.process
        process.uncertain = process.active = True
        process.owner = Owner(exited=passes)  # type: ignore[assignment]
        assert broker.status()["safety"]["uncertainty"] == [
            {"flag": "build_process", "reason": "build_process_exit_unconfirmed"}
        ]
        try:
            result = resource_recheck.recheck(broker)
            assert checked(result) == {
                "build_process": ("cleared", "process_tree_exit_confirmed")
                if passes
                else ("kept", "process_tree_still_active")
            }
            assert process.uncertain is not passes
        finally:
            reset(broker)


@pytest.mark.parametrize("flag", ["build_output", "source_cleanup"])
async def test_flags_without_a_side_effect_free_check_are_kept(tmp_path, flag):
    async with running(tmp_path) as (broker, _):
        if flag == "build_output":
            broker.firmware_build.output_uncertain = True
        else:
            broker.firmware_source.snapshots.uncertain = True
        try:
            result = resource_recheck.recheck(broker)
            assert checked(result) == {flag: ("kept", "no_side_effect_free_check")}
            assert [row["flag"] for row in result["remaining"]] == [flag]
        finally:
            reset(broker)


@pytest.mark.parametrize(
    ("cause", "process_uncertain", "expected"),
    [
        ("owned_process", False, ("cleared", "owned_process_exit_confirmed")),
        ("owned_process", True, ("kept", "dependent_flag_kept")),
        ("other", False, ("kept", "no_side_effect_free_check")),
    ],
)
async def test_profile_state_flag(tmp_path, cause, process_uncertain, expected):
    async with running(tmp_path) as (broker, _):
        profile = broker.profile_jobs
        profile.state_uncertain = True
        profile.state_uncertain_reason = "build_process_exit_unconfirmed"
        profile.state_uncertain_cause = cause
        process = broker.firmware_build.process
        process.uncertain = process.active = process_uncertain  # no owner: kept
        try:
            status = broker.status()["safety"]["uncertainty"]
            assert {"flag": "profile_state", "reason": "build_process_exit_unconfirmed"} in status
            assert checked(resource_recheck.recheck(broker))["profile_state"] == expected
            assert profile.state_uncertain is (expected[0] == "kept")
        finally:
            reset(broker)


@pytest.mark.parametrize("flag", ["profile_worker", "execution"])
@pytest.mark.parametrize("state", ["consistent", "running_row", "store_busy"])
async def test_state_latches_need_idle_lane_and_consistent_state(tmp_path, flag, state):
    async with running(tmp_path) as (broker, _):
        store = broker._store()
        if state == "running_row":
            store.add_job("stuck", "c" * 64)
            store.set_job("stuck", "running")
        if flag == "profile_worker":
            broker.profile_jobs.uncertain = True
        else:
            broker._latch_execution("job_final_commit_failed")
            assert {
                "flag": "execution",
                "reason": "job_final_commit_failed",
            } in broker.status()["safety"]["uncertainty"]
        if state == "store_busy":
            store.db.execute("UPDATE jobs SET stage=stage WHERE 0")  # opens a transaction
            assert store.db.in_transaction
        try:
            result = resource_recheck.recheck(broker)
            expected = {
                "consistent": ("cleared", "lane_idle_state_consistent"),
                "running_row": ("kept", "job_row_still_running"),
                "store_busy": ("kept", "state_store_unavailable"),
            }[state]
            assert checked(result) == {flag: expected}
            assert result["execution_uncertain"] is (expected[0] == "kept")
        finally:
            if store.db.in_transaction:
                store.db.rollback()
            store.db.execute("DELETE FROM jobs WHERE id='stuck'")
            store.db.commit()
            reset(broker)


async def test_execution_waits_for_device_lane_latches(tmp_path):
    async with running(tmp_path) as (broker, _):
        broker._latch_execution("job_final_commit_failed")
        process = broker.firmware_build.process
        process.uncertain = process.active = True  # no retained owner: cannot verify
        try:
            assert checked(resource_recheck.recheck(broker)) == {
                "build_process": ("kept", "process_tree_unverifiable"),
                "execution": ("kept", "dependent_flag_kept"),
            }
            assert broker.execution_uncertain is True
        finally:
            reset(broker)


@pytest.mark.parametrize("busy", ["worker", "profile", "home"])
async def test_active_work_keeps_every_flag(tmp_path, busy):
    async with running(tmp_path) as (broker, _):
        broker._latch_execution("job_final_commit_failed")
        broker.firmware_build.output_uncertain = True
        if busy == "worker":
            broker._worker_job = "busy"
        elif busy == "profile":
            broker.profile_jobs.active = "busy"
        else:
            broker.home_recovery.entries["x"] = SimpleNamespace(state="running", task=None)
        try:
            assert checked(resource_recheck.recheck(broker)) == {
                "build_output": ("kept", "owned_work_active"),
                "execution": ("kept", "owned_work_active"),
            }
            assert broker.execution_uncertain is True
        finally:
            broker.profile_jobs.active = None
            broker.home_recovery.entries.clear()
            reset(broker)


async def test_recheck_never_changes_jobs_queue_or_device(tmp_path):
    async with running(tmp_path) as (broker, _):
        before = needs_review_job(broker)
        lease = broker.session
        calls = broker.backend.calls
        broker._latch_execution("job_review_write_refused")
        try:
            resource_recheck.recheck(broker)
            resource_recheck.recheck(broker)  # idempotent second run
            store = broker._store()
            assert dict(
                store.db.execute("SELECT * FROM jobs WHERE id='review-row'").fetchone()
            ) == (before)
            assert broker.session is lease and broker.queue.empty()
            assert broker.backend.calls == calls
        finally:
            reset(broker)


# --- serial latch on a serial broker -----------------------------------------------------


@pytest.mark.parametrize("close_fails", [False, True])
async def test_serial_latch_clears_only_on_confirmed_close(tmp_path, close_fails):
    async with serial_running(tmp_path) as e:
        e.backend.ownership_uncertain = e.backend.hardware_connected = True
        e.backend.active = 1
        e.backend._unclosed_port = e.port
        e.port.is_open, e.port.close_fails = True, close_fails
        e.b.active_job = "failed-derby"
        job = e.b.job("failed-derby")
        assert {"flag": "serial_ownership", "reason": "serial_close_unconfirmed"} in e.b.status()[
            "safety"
        ]["uncertainty"]
        result = resource_recheck.recheck(e.b)
        assert checked(result) == {
            "serial_ownership": ("kept", "serial_close_unconfirmed")
            if close_fails
            else ("cleared", "serial_close_confirmed")
        }
        assert e.port.opens == 0 and e.port.writes == [] and e.port.closes == 1
        assert e.backend.ownership_uncertain is close_fails
        assert (e.b.active_job is None) is not close_fails
        assert e.b.job("failed-derby") == job
        # Explicit only: a second re-check is another single close attempt, no loop.
        resource_recheck.recheck(e.b)
        assert e.port.closes == (2 if close_fails else 1)
