"""Online local-operator acknowledgement and re-check routes; fakes only, no hardware."""

import json
from types import SimpleNamespace

import pytest
from test_home_recovery import request as home_request
from test_home_recovery import running as serial_running
from test_pending_execution import running

from cardputer_mcp import operator_review
from cardputer_mcp.broker import BrokerError, digest_plan
from cardputer_mcp.models import Origin
from cardputer_mcp.operator_api import OperatorAPI
from cardputer_mcp.store import Store

STATES = ("queued", "running", "succeeded", "failed", "cancelled", "needs_review")


def add_request(store, request_id="restart-request", state="needs_review", reviewed=0):
    store.db.execute(
        "INSERT INTO device_requests(id,session_id,lease_id,state,lease_seconds,reason,reviewed) "
        "VALUES(?,?,?,?,?,?,?)",
        (request_id, "s" * 32, None, state, 60, "broker_restart_no_replay", reviewed),
    )
    store.db.commit()


def row(store, table, key):
    return dict(store.db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone())


def add_job(broker, job_id="review-row", state="needs_review"):
    store = broker._store()
    plan = {"version": 1, "job_id": job_id, "test": "firmware_observe"}
    digest = digest_plan(plan)
    store.put_plan(digest, plan)
    store.add_job(job_id, digest)
    store.set_job(job_id, state, "serial_close_unconfirmed", {"code": "synthetic"})
    return digest


# --- persistence --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["waiting", "owning", "released", "needs_review"])
@pytest.mark.parametrize("reviewed", [0, 1])
def test_store_acknowledges_only_unreviewed_needs_review_requests(tmp_path, state, reviewed):
    store = Store(tmp_path / "history.sqlite3")
    try:
        add_request(store, state=state, reviewed=reviewed)
        before = row(store, "device_requests", "restart-request")
        written = store.acknowledge_request("restart-request")
        assert written is (state == "needs_review" and reviewed == 0)
        after = row(store, "device_requests", "restart-request")
        assert after["reviewed"] == (1 if written else reviewed)
        assert {k: v for k, v in after.items() if k != "reviewed"} == {
            k: v for k, v in before.items() if k != "reviewed"
        }
        assert store.acknowledge_request("missing") is False
    finally:
        store.close()


def test_write_probe_writes_nothing(tmp_path):
    store = Store(tmp_path / "history.sqlite3")
    try:
        before = list(store.db.iterdump())
        assert store.write_probe() is True
        assert list(store.db.iterdump()) == before and not store.db.in_transaction
    finally:
        store.close()


# --- acknowledgement ----------------------------------------------------------------------


@pytest.mark.parametrize("state", STATES)
async def test_job_acknowledgement_sets_only_reviewed(tmp_path, state):
    async with running(tmp_path) as (broker, _):
        digest = add_job(broker, state=state)
        store = broker._store()
        before = row(store, "jobs", "review-row")
        calls, lease = broker.backend.calls, broker.session
        if state != "needs_review":
            with pytest.raises(BrokerError, match="^review_not_required$"):
                operator_review.acknowledge_job(broker, "review-row", digest, broker.epoch)
            assert row(store, "jobs", "review-row") == before
            return
        first = operator_review.acknowledge_job(broker, "review-row", digest, broker.epoch)
        assert first == {
            "broker_epoch": broker.epoch,
            "job_id": "review-row",
            "plan_hash": digest,
            "state": "needs_review",
            "reviewed": True,
            "already_reviewed": False,
            "replayed": False,
        }
        again = operator_review.acknowledge_job(broker, "review-row", digest, broker.epoch)
        assert again["already_reviewed"] is True
        after = row(store, "jobs", "review-row")
        assert after["reviewed"] == 1
        assert {k: v for k, v in after.items() if k != "reviewed"} == {
            k: v for k, v in before.items() if k != "reviewed"
        }
        assert broker.backend.calls == calls and broker.session is lease
        assert broker.queue.empty() and not store.needs_review()


@pytest.mark.parametrize(
    ("job", "plan", "epoch", "code"),
    [
        ("missing", "a" * 64, None, "review_target_not_found"),
        ("review-row", "b" * 64, None, "review_target_not_found"),
        ("review-row", None, "0" * 32, "stale_operator_snapshot"),
    ],
)
async def test_job_acknowledgement_refusals(tmp_path, job, plan, epoch, code):
    async with running(tmp_path) as (broker, _):
        digest = add_job(broker)
        with pytest.raises(BrokerError, match=f"^{code}$"):
            operator_review.acknowledge_job(broker, job, plan or digest, epoch or broker.epoch)
        assert broker._store().needs_review()


@pytest.mark.parametrize("state", ["waiting", "owning", "needs_review"])
async def test_request_acknowledgement_sets_only_reviewed(tmp_path, state):
    async with running(tmp_path) as (broker, _):
        store = broker._store()
        add_request(store, state=state)
        before = row(store, "device_requests", "restart-request")
        if state != "needs_review":
            with pytest.raises(BrokerError, match="^review_not_required$"):
                operator_review.acknowledge_request(broker, "restart-request", broker.epoch)
            assert row(store, "device_requests", "restart-request") == before
            return
        assert store.needs_review()
        first = operator_review.acknowledge_request(broker, "restart-request", broker.epoch)
        assert first["reviewed"] is True and first["already_reviewed"] is False
        assert first["replayed"] is False and first["state"] == "needs_review"
        again = operator_review.acknowledge_request(broker, "restart-request", broker.epoch)
        assert again["already_reviewed"] is True
        after = row(store, "device_requests", "restart-request")
        assert {k: v for k, v in after.items() if k != "reviewed"} == {
            k: v for k, v in before.items() if k != "reviewed"
        }
        assert not store.needs_review() and store.recovery_requests() == []
        with pytest.raises(BrokerError, match="^review_target_not_found$"):
            operator_review.acknowledge_request(broker, "missing", broker.epoch)
        with pytest.raises(BrokerError, match="^stale_operator_snapshot$"):
            operator_review.acknowledge_request(broker, "restart-request", "0" * 32)


# --- HOME recovery precondition -----------------------------------------------------------


@pytest.mark.parametrize("other", ["job", "request"])
async def test_acknowledged_row_no_longer_blocks_home_recovery(tmp_path, other):
    async with serial_running(tmp_path) as e:
        store = e.b._store()
        if other == "job":
            digest = add_job(e.b, "other-job")
        else:
            add_request(store)
        # The precondition's refusal surfaces through `_validate` as unavailable.
        with pytest.raises(BrokerError, match="^home_recovery_unavailable$"):
            home_request(e)
        assert not e.port.opens
        if other == "job":
            operator_review.acknowledge_job(e.b, "other-job", digest, e.b.epoch)
        else:
            operator_review.acknowledge_request(e.b, "restart-request", e.b.epoch)
        # The refused attempt was not consumed; a new explicit request is admitted.
        prepared = home_request(e, "recover-two")
        assert prepared["state"] == "running"
        await e.m.entries[prepared["plan_hash"]].task
        assert e.b.job("recover-two")["state"] == "succeeded"


async def test_source_of_a_running_home_recovery_cannot_be_acknowledged(tmp_path):
    async with serial_running(tmp_path) as e:
        source = e.b.job("failed-derby")
        prepared = home_request(e)
        with pytest.raises(BrokerError, match="^review_target_in_use$"):
            operator_review.acknowledge_job(e.b, "failed-derby", source["plan_hash"], e.b.epoch)
        await e.m.entries[prepared["plan_hash"]].task
        assert e.b.job("recover-one")["state"] == "succeeded"


# --- routes (thin) --------------------------------------------------------------------------


def principal(origin=Origin.LOCAL, scopes=("read", "control")):
    return SimpleNamespace(principal_id="local-operator", origin=origin, scopes=frozenset(scopes))


async def post(api, path, body, *, raw=None):
    encoded = raw if raw is not None else json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": encoded, "more_body": False}

    output = []

    async def send(message):
        output.append(message)

    await api(
        {
            "type": "http",
            "path": path,
            "method": "POST",
            "query_string": b"",
            "headers": [
                (b"authorization", b"Bearer SYNTHETIC"),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
        send,
    )
    return output[0]["status"], json.loads(output[1]["body"])


async def unused_app(scope, receive, send):
    raise AssertionError("operator route was not handled")


@pytest.mark.parametrize(
    ("actor", "status", "error"),
    [
        (None, 401, "authentication_required"),
        (principal(origin=Origin.REMOTE), 403, "local_operator_required"),
        (principal(scopes=("read",)), 403, "local_operator_required"),
    ],
)
@pytest.mark.parametrize("path", ["/operator/review", "/operator/recheck"])
async def test_routes_refuse_remote_read_only_and_anonymous(tmp_path, actor, status, error, path):
    async with running(tmp_path) as (broker, _):
        digest = add_job(broker)
        broker._latch_execution("job_final_commit_failed")
        api = OperatorAPI(unused_app, broker, lambda _header: actor)
        body = (
            {"job_id": "review-row", "plan_hash": digest, "broker_epoch": broker.epoch}
            if path == "/operator/review"
            else {"broker_epoch": broker.epoch}
        )
        assert await post(api, path, body) == (status, {"ok": False, "error": error})
        assert broker._store().needs_review() and broker.execution_uncertain
        broker.execution_uncertain = False


async def test_local_control_review_and_recheck_routes(tmp_path):
    async with running(tmp_path) as (broker, _):
        digest = add_job(broker)
        add_request(broker._store())
        api = OperatorAPI(unused_app, broker, lambda _header: principal())
        job = {"job_id": "review-row", "plan_hash": digest, "broker_epoch": broker.epoch}
        status, reply = await post(api, "/operator/review", job)
        assert status == 200 and reply["ok"] and reply["already_reviewed"] is False
        status, reply = await post(api, "/operator/review", job)
        assert status == 200 and reply["ok"] and reply["already_reviewed"] is True
        status, reply = await post(
            api,
            "/operator/review",
            {"request_id": "restart-request", "broker_epoch": broker.epoch},
        )
        assert status == 200 and reply["ok"] and reply["request_id"] == "restart-request"
        assert not broker._store().needs_review()
        broker._latch_execution("job_final_commit_failed")
        status, reply = await post(api, "/operator/recheck", {"broker_epoch": broker.epoch})
        assert status == 200 and reply["ok"] and reply["execution_uncertain"] is False
        assert reply["checked"] == [
            {"flag": "execution", "outcome": "cleared", "reason": "lane_idle_state_consistent"}
        ]
        status, reply = await post(api, "/operator/recheck", {"broker_epoch": "0" * 32})
        assert reply == {"ok": False, "error": "stale_operator_snapshot"}
        assert broker.job("review-row")["state"] == "needs_review"


@pytest.mark.parametrize(
    "body",
    [
        {"job_id": "review-row", "request_id": "x", "plan_hash": "a" * 64},
        {"job_id": "review-row"},
        {"plan_hash": "a" * 64},
        {},
        {"request_id": "x", "extra": 1},
        {"request_id": 7},
    ],
)
async def test_review_route_requires_exactly_one_target(tmp_path, body):
    async with running(tmp_path) as (broker, _):
        add_job(broker)
        api = OperatorAPI(unused_app, broker, lambda _header: principal())
        status, reply = await post(api, "/operator/review", {**body, "broker_epoch": broker.epoch})
        assert (status, reply) == (400, {"ok": False, "error": "invalid_request"})
        assert broker._store().needs_review()


async def test_review_route_rejects_oversized_and_duplicate_key_bodies(tmp_path):
    async with running(tmp_path) as (broker, _):
        api = OperatorAPI(unused_app, broker, lambda _header: principal())
        too_large = await post(api, "/operator/review", None, raw=b"{" + b" " * 4096 + b"}")
        assert too_large == (413, {"ok": False, "error": "request_too_large"})
        duplicate = b'{"request_id":"a","request_id":"b","broker_epoch":"' + b"0" * 32 + b'"}'
        assert await post(api, "/operator/review", None, raw=duplicate) == (
            400,
            {"ok": False, "error": "invalid_request"},
        )
