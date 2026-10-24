"""LeaseManager: the only writer of the device lease and of its deadline."""

import ast
from pathlib import Path

import pytest
from test_continuation import Clock, running
from test_stage05_cancellation import promote_waiting_job

from cardputer_mcp.broker import Broker
from cardputer_mcp.config import Settings
from cardputer_mcp.lease_manager import LeaseManager, ReleaseRefused, Session

SRC = Path(__file__).resolve().parents[1] / "src" / "cardputer_mcp"


def lease(deadline: float = 100.0) -> Session:
    return Session("c" * 64, "lease-id", "local_cli", deadline)


def manager(busy: bool = False) -> tuple[LeaseManager, list[bool]]:
    state = [busy]
    return LeaseManager(lambda: state[0]), state


def test_starts_without_a_lease():
    leases, _ = manager()
    assert leases.current is None


def test_grant_makes_the_lease_current():
    leases, _ = manager()
    granted = lease()
    assert leases.grant(granted) is granted
    assert leases.current is granted


@pytest.mark.parametrize("busy", [False, True])
def test_end_without_a_lease_is_a_no_op_even_when_busy(busy):
    leases, _ = manager(busy)
    calls = []
    assert leases.end(calls.append) is None
    assert calls == []


def test_end_finalizes_while_current_then_clears():
    leases, _ = manager()
    granted = leases.grant(lease())
    seen = []

    def finalize(session):
        seen.append((session, leases.current))

    assert leases.end(finalize) is granted
    assert seen == [(granted, granted)]
    assert leases.current is None


def test_end_refuses_while_busy_before_any_side_effect():
    leases, state = manager(busy=True)
    granted = leases.grant(lease())
    calls = []
    with pytest.raises(ReleaseRefused):
        leases.end(calls.append)
    assert calls == []
    assert leases.current is granted
    state[0] = False
    assert leases.end(calls.append) is granted
    assert calls == [granted]


def test_busy_is_read_at_release_time_not_at_construction():
    leases, state = manager(busy=False)
    leases.grant(lease())
    state[0] = True
    with pytest.raises(ReleaseRefused):
        leases.end(lambda session: None)


def test_failed_finalize_keeps_the_lease():
    leases, _ = manager()
    granted = leases.grant(lease())

    def fail(session):
        raise RuntimeError("store failure")

    with pytest.raises(RuntimeError):
        leases.end(fail)
    assert leases.current is granted


@pytest.mark.parametrize(
    ("deadline", "until", "expected"),
    [
        pytest.param(100.0, 50.0, 50.0, id="earlier-moves"),
        pytest.param(100.0, 150.0, 100.0, id="later-never-extends"),
        pytest.param(100.0, 100.0, 100.0, id="equal"),
    ],
)
def test_shorten_never_extends(deadline, until, expected):
    session = lease(deadline)
    LeaseManager.shorten(session, until)
    assert session.deadline == expected


@pytest.mark.parametrize(
    ("deadline", "until", "cap", "expected"),
    [
        pytest.param(100.0, 160.0, None, 160.0, id="extends"),
        pytest.param(100.0, 40.0, None, 40.0, id="replaces-even-if-earlier"),
        pytest.param(100.0, 160.0, 130.0, 130.0, id="capped-by-work-session"),
        pytest.param(100.0, 120.0, 130.0, 120.0, id="cap-not-reached"),
    ],
)
def test_renew_replaces_the_deadline_capped_by_work(deadline, until, cap, expected):
    session = lease(deadline)
    LeaseManager.renew(session, until, cap)
    assert session.deadline == expected


def test_shorten_and_renew_write_the_given_session_even_if_not_current():
    leases, _ = manager()
    current = leases.grant(lease(100.0))
    other = lease(100.0)
    leases.shorten(other, 10.0)
    leases.renew(other, 20.0)
    assert (current.deadline, other.deadline) == (100.0, 20.0)


def test_broker_session_reads_and_overwrites_through_the_manager(tmp_path):
    broker = Broker(Settings(state_dir=tmp_path / "state", allow_mock_execution=True))
    assert broker.session is None and broker.leases.current is None
    granted = lease()
    broker.session = granted  # test seam
    assert broker.leases.current is granted


# --- structure: nobody else writes the lease or its deadline -----------------------------


def _writes(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            owner = ast.unparse(target.value)
            # Other modules' own `self.session` attributes (app client, DERBY) are
            # unrelated; in broker.py and on any broker reference it is the lease.
            if target.attr == "session" and (owner != "self" or path.name == "broker.py"):
                found.append(f"{path.name}:{node.lineno} {owner}.session")
            if target.attr == "deadline" and "session" in ast.unparse(target.value):
                found.append(f"{path.name}:{node.lineno} {ast.unparse(target)}")
    return found


def test_only_the_lease_manager_assigns_the_lease_or_its_deadline():
    offenders = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "lease_manager.py":
            continue
        offenders.extend(_writes(path))
    assert offenders == []


def test_overwrite_is_only_the_test_seam():
    callers = [
        f"{path.name}:{number}"
        for path in sorted(SRC.glob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if ".overwrite(" in line
    ]
    assert len(callers) == 1 and callers[0].startswith("broker.py:")


# --- the deliberate re-entrancy of maintenance_tick --------------------------------------


async def test_job_lease_grant_runs_one_nested_maintenance_tick(tmp_path):
    """Pinned: _grant_job_lease -> prepare/execute -> _lease -> _expire re-enters the tick."""
    clock = Clock()
    async with running(tmp_path, clock) as broker:
        original = broker.maintenance_tick
        depth = [0]
        nested = []

        def tick():
            depth[0] += 1
            try:
                if depth[0] > 1:
                    nested.append(broker.session)
                original()
            finally:
                depth[0] -= 1

        broker.maintenance_tick = tick
        broker._expire = lambda: broker.maintenance_tick()
        granted = promote_waiting_job(broker)
        # prepare and execute each check the lease once, under the new job lease.
        assert nested == [granted, granted]
        assert broker.session is granted
