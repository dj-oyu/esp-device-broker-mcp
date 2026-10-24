"""The cancellation dispatcher and the device-lane stop table (jobs.md section 4)."""

from types import SimpleNamespace

import pytest

from cardputer_mcp import cancellation
from cardputer_mcp.cancellation import DeviceStop, device_stop
from cardputer_mcp.home_recovery import CancelHomeRecovery
from cardputer_mcp.models import FLASH_TESTS, CancelDeviceRequest, CancelRequest
from cardputer_mcp.models import TestName as Action

RUNNING = [
    (Action.APP_SESSION.value, DeviceStop.APP_EVENT),
    (Action.SOURCE.value, DeviceStop.SOURCE),
    (Action.DERBY.value, DeviceStop.SMOKE),
    (Action.DERBY_FIXED.value, DeviceStop.SMOKE),
    (Action.BUILD.value, DeviceStop.BUILD),
    *[(test.value, DeviceStop.FLASH) for test in sorted(FLASH_TESTS)],
]
UNSTOPPABLE = sorted(
    {test.value for test in Action} - {test for test, _ in RUNNING},
)


@pytest.mark.parametrize(("test", "stop"), RUNNING)
def test_running_kinds_with_a_stop_primitive(test, stop):
    assert device_stop("running", test) is stop


@pytest.mark.parametrize("test", UNSTOPPABLE)
def test_other_running_kinds_are_refused(test):
    # hello, observe, restart, capture, diagnostics: only_queued_jobs_can_be_cancelled
    assert device_stop("running", test) is DeviceStop.REFUSE


@pytest.mark.parametrize("test", sorted(test.value for test in Action))
@pytest.mark.parametrize(
    ("state", "stop"),
    [
        ("awaiting_approval", DeviceStop.INTENT),
        ("queued", DeviceStop.QUEUED),
        ("succeeded", DeviceStop.REFUSE),
        ("failed", DeviceStop.REFUSE),
        ("cancelled", DeviceStop.REFUSE),
        ("needs_review", DeviceStop.REFUSE),
    ],
)
def test_non_running_states_do_not_depend_on_the_kind(state, stop, test):
    assert device_stop(state, test) is stop


def test_unstoppable_list_is_not_empty():
    assert Action.HELLO.value in UNSTOPPABLE


# --- dispatcher: each target reaches exactly its own primitive, result unchanged ------

LEASED = CancelRequest(job_id="job", session="c" * 64)
WAIT = CancelDeviceRequest(resume_token="e" * 64, request_id="req")
HOME = CancelHomeRecovery(job_id="job", plan_hash="a" * 64)
SCOPES = frozenset({"read", "control"})

ROUTES = [
    (cancellation.LeasedDeviceJob(LEASED), "cancel", (LEASED,), {}),
    (cancellation.JobLeaseJob("job", "key-a"), "cancel_job_only", ("job", "key-a"), {}),
    (cancellation.DeferredJob("job", "key-a"), "cancel_deferred", ("job", "key-a"), {}),
    (cancellation.Ticket("ticket", "key-a"), "cancel_ticket", ("ticket", "key-a"), {}),
    (
        cancellation.DeviceRequestWait(WAIT, "key-a", SCOPES),
        "cancel_device_request",
        (WAIT,),
        {"principal_id": "key-a", "scopes": SCOPES},
    ),
    (
        cancellation.ProfileJob(LEASED, "key-a", "remote_mcp"),
        "profile_jobs.cancel",
        (LEASED, "key-a", "remote_mcp"),
        {},
    ),
    (
        cancellation.HomeRecoveryJob(HOME, "key-a"),
        "home_recovery.cancel",
        (HOME, "key-a"),
        {},
    ),
    (cancellation.OperatorWait("req"), "operator_cancel_wait", ("req",), {}),
    (
        cancellation.OperatorProfileJob("job", "a" * 64, "b" * 32, "key-l", "local_cli"),
        "profile_jobs.operator_cancel",
        ("job", "a" * 64, "b" * 32, "key-l", "local_cli"),
        {},
    ),
]

PRIMITIVES = sorted({route for _, route, _, _ in ROUTES})


def recording_resident(calls, result):
    def primitive(name):
        def call(*args, **kwargs):
            calls.append((name, args, kwargs))
            return result

        return call

    resident = SimpleNamespace(profile_jobs=SimpleNamespace(), home_recovery=SimpleNamespace())
    for name in PRIMITIVES:
        owner, _, attr = name.rpartition(".")
        setattr(getattr(resident, owner) if owner else resident, attr, primitive(name))
    return resident


@pytest.mark.parametrize(
    ("target", "route", "args", "kwargs"),
    [pytest.param(*row, id=type(row[0]).__name__) for row in ROUTES],
)
@pytest.mark.parametrize("result", [{"cancelled": True}, None])
def test_each_target_reaches_only_its_primitive(target, route, args, kwargs, result):
    calls = []
    assert cancellation.cancel_job(recording_resident(calls, result), target) is result
    assert calls == [(route, args, kwargs)]


@pytest.mark.parametrize(
    "target",
    [pytest.param(row[0], id=type(row[0]).__name__) for row in ROUTES],
)
def test_primitive_errors_pass_through_unchanged(target):
    class Refused(ValueError):
        pass

    error = Refused("fixed_reason")

    def fail(*args, **kwargs):
        raise error

    resident = recording_resident([], None)
    for name in PRIMITIVES:
        owner, _, attr = name.rpartition(".")
        setattr(getattr(resident, owner) if owner else resident, attr, fail)
    with pytest.raises(Refused) as raised:
        cancellation.cancel_job(resident, target)
    assert raised.value is error


def test_every_target_kind_has_a_route():
    routed = {type(target) for target, _, _, _ in ROUTES}
    assert routed == set(cancellation.CancelTarget.__args__)


def test_unknown_target_is_rejected():
    with pytest.raises(TypeError):
        cancellation.cancel_job(SimpleNamespace(), object())  # type: ignore[call-overload]
