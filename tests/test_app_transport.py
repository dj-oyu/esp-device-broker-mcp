from dataclasses import replace

import pytest

from cardputer_mcp.app_transport import AppTransportError, ControlConnection
from cardputer_mcp.app_wire import Op, Reply, Request, Result, State


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def pause(self, seconds):
        self.now += seconds


class Port:
    def __init__(self, handler):
        self.handler = handler
        self.incoming = bytearray()
        self.requests = []

    def write(self, data):
        request = Request.decode(data)
        self.requests.append(request)
        self.incoming.extend(self.handler(request))
        return len(data)

    def read(self, size=1):
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data


def outcome(request, result=Result.CONSUMED, **kwargs):
    return Reply(
        request.session,
        request.command_id,
        request.boot,
        request.run,
        request.seq,
        request.fence,
        result,
        State.RUNNING,
        request.op,
        next_seq=request.seq + 1,
        **kwargs,
    )


def connection(handler, **kwargs):
    clock = Clock()
    port = Port(handler)
    return ControlConnection(port, clock=clock, pause=clock.pause, timeout=0.05, **kwargs), port


def test_consumed_is_distinct_from_sent_and_accepted():
    events = []

    def handle(request):
        if request.op == Op.STATUS:
            return replace(outcome(request), op=Op.TAP).encode()
        return outcome(request, Result.ACCEPTED).encode()

    conn, port = connection(handle, control=events.append)
    reply = conn.send(Op.TAP, 3, 7, boot=100, run=8, seq=1, fence=4, arg=1)
    assert reply.result == Result.CONSUMED
    assert [r.result for r in events] == [Result.ACCEPTED, Result.CONSUMED]
    assert [r.op for r in port.requests] == [Op.TAP, Op.STATUS]


def test_lost_ack_queries_same_id_and_never_replays_input():
    def handle(request):
        if request.op == Op.TAP:
            return b"HOME_READY\nconsumed\n"
        return replace(outcome(request), op=Op.TAP).encode()

    conn, port = connection(handle)
    assert conn.send(Op.TAP, 3, 7, boot=100, run=8, seq=1, fence=4).result == Result.CONSUMED
    assert [r.op for r in port.requests] == [Op.TAP, Op.STATUS]
    assert port.requests[1].command_id == 7
    assert port.requests[1].seq == 1


def test_unrecoverable_ack_returns_unknown_and_does_not_reopen_or_relaunch():
    conn, port = connection(lambda _: b"")
    with pytest.raises(AppTransportError, match="outcome_unknown"):
        conn.send(Op.START, 3, 7, boot=100, seq=1, fence=4, app_id="local.hello")
    assert port.requests[0].op == Op.START
    assert all(r.op == Op.STATUS for r in port.requests[1:])
    assert all(r.command_id == 7 for r in port.requests)


@pytest.mark.parametrize(
    "result",
    [
        Result.UNKNOWN_APP,
        Result.QUEUE_FULL,
        Result.BLOCKED,
        Result.CONFLICT,
        Result.OUT_OF_ORDER,
        Result.OUTCOME_UNKNOWN,
    ],
)
def test_explicit_rejections_do_not_retry(result):
    conn, port = connection(lambda r: outcome(r, result).encode())
    assert conn.send(Op.TAP, 3, 7, boot=100, run=8, seq=1, fence=4).result == result
    assert len(port.requests) == 1


@pytest.mark.parametrize("field", ["boot", "run", "fence"])
def test_stale_generation_cannot_complete_a_command(field):
    conn, port = connection(lambda r: replace(outcome(r), **{field: 999}).encode())
    with pytest.raises(AppTransportError, match="outcome_unknown"):
        conn.send(Op.TAP, 3, 7, boot=100, run=8, seq=1, fence=4)
    assert len(port.requests) == 1


@pytest.mark.parametrize(
    "field,result",
    [("boot", Result.STALE_BOOT), ("run", Result.STALE_RUN), ("fence", Result.STALE_LEASE)],
)
def test_explicit_generation_rejection_is_preserved(field, result):
    conn, port = connection(lambda r: replace(outcome(r, result), **{field: 999}).encode())
    assert conn.send(Op.TAP, 3, 7, boot=100, run=8, seq=1, fence=4).result == result
    assert len(port.requests) == 1


def test_status_is_read_only_and_accepts_original_operation():
    conn, port = connection(lambda r: replace(outcome(r), op=Op.TAP).encode())
    assert conn.send(Op.STATUS, 3, 7, boot=100, run=8, seq=1, fence=4).op == Op.TAP
    assert [r.op for r in port.requests] == [Op.STATUS]


def test_catalog_binds_boot_and_descriptor_identity():
    def handle(r):
        return Reply(
            19,
            0,
            100,
            0,
            0,
            0,
            Result.OK,
            State.NO_SESSION,
            Op.CATALOG,
            catalog_index=r.seq,
            catalog_count=2,
            flags=5,
            app_id=("local.hello", "local.stress")[r.seq],
        ).encode()

    conn, port = connection(handle)
    assert [r.app_id for r in conn.catalog()] == ["local.hello", "local.stress"]
    assert all(r.op == Op.CATALOG for r in port.requests)


def test_metadata_fingerprint_is_raw_bytes_and_exposes_hex():
    sha = "00ff1122" * 8
    metadata = Reply(
        19,
        0,
        100,
        0,
        0,
        0,
        Result.OK,
        State.NO_SESSION,
        Op.CATALOG,
        catalog_index=65535,
        catalog_count=2,
        app_id=sha,
        title="1.0",
    )
    conn, _ = connection(lambda _: metadata.encode())
    observed = conn.send(Op.CATALOG, 0, 0, seq=65535)
    assert observed == metadata
    assert observed.app_id == sha


def test_delayed_descriptor_cannot_complete_metadata_request():
    old = Reply(
        19,
        0,
        100,
        0,
        0,
        0,
        Result.OK,
        State.NO_SESSION,
        Op.CATALOG,
        catalog_index=0,
        catalog_count=2,
        app_id="local.hello",
    )
    metadata = replace(old, catalog_index=65535, app_id="00ff1122" * 8, title="1.0")
    conn, port = connection(lambda _: old.encode() + metadata.encode())
    assert conn.send(Op.CATALOG, 0, 0, seq=65535) == metadata
    assert len(port.requests) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"boot": 101},
        {"session": 20},
        {"catalog_count": 3},
        {"catalog_index": 0},
        {"result": Result.UNSUPPORTED},
        {"app_id": "local.hello"},
    ],
)
def test_catalog_rejects_inconsistent_or_duplicate_pages(changed):
    first = Reply(
        19,
        0,
        100,
        0,
        0,
        0,
        Result.OK,
        State.NO_SESSION,
        Op.CATALOG,
        catalog_index=0,
        catalog_count=2,
        app_id="local.hello",
    )

    def handle(request):
        if request.seq == 0:
            return first.encode()
        second = replace(first, catalog_index=1, app_id="local.stress")
        return replace(second, **changed).encode()

    conn, _ = connection(handle)
    with pytest.raises(AppTransportError, match="outcome_unknown"):
        conn.catalog()


def test_log_flood_does_not_hide_ack_or_become_commands():
    logs = []
    conn, port = connection(lambda r: b"stop\n" * 100 + outcome(r).encode(), log=logs.append)
    assert conn.send(Op.TAP, 3, 7, boot=100, run=8, seq=1, fence=4).result == Result.CONSUMED
    assert len(logs) == 100
    assert len(port.requests) == 1


def test_poll_only_reads_and_has_a_deadline():
    logs = []
    conn, port = connection(lambda _: b"", log=logs.append)
    port.incoming.extend(b"secret=synthetic\n")
    conn.poll(timeout=0.01)
    assert logs == ["secret=synthetic"]
    assert not port.requests
    assert 0.01 <= conn.clock() < 0.02
