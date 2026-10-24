import pytest

from cardputer_mcp.app_wire import (
    MAX_WIRE,
    Decoder,
    Op,
    Reply,
    Request,
    Result,
    State,
    frame,
    unframe,
)


def reply():
    return Reply(
        7,
        11,
        0xABCDEF0123456789,
        9,
        3,
        4,
        Result.CONSUMED,
        State.RUNNING,
        Op.TAP,
        arg=1,
        next_seq=4,
        ttl=1234,
        app_id="local.stress",
        title="STRESS",
    )


def test_fixed_layout_roundtrip_and_maximum_bound():
    request = Request(Op.START, 7, 11, 0xABCDEF0123456789, 0, 1, 4, 90000, "local.stress", 0)
    assert Request.decode(request.encode()) == request
    assert len(unframe(request.encode())[3]) == 60
    response = reply()
    assert Reply.decode(response.encode()) == response
    assert len(unframe(response.encode())[3]) == 120
    assert len(frame(1, 0, 0, b"x" * 480)) == MAX_WIRE
    assert unframe(frame(1, 0, 0, b"x" * 480))[3] == b"x" * 480
    with pytest.raises(ValueError):
        frame(1, 0, 0, b"x" * 481)


@pytest.mark.parametrize(
    "alter",
    [
        lambda b: b.replace(b"\x1dC", b"\x1dB", 1),
        lambda b: b[:-2] + b"0\n",
        lambda b: b[:-1],
        lambda b: b[:3] + b"Z" + b[4:],
    ],
)
def test_namespace_crc_and_truncation_rejected(alter):
    with pytest.raises(ValueError):
        Reply.decode(alter(reply().encode()))


def test_flood_and_bad_frame_are_bounded_and_recover_at_newline():
    decoder = Decoder()
    found, logs = decoder.feed(b"x" * 1000000 + b"\n")
    assert not found and len(logs[0].encode()) <= 1024
    assert logs[0].endswith(" [truncated]")
    assert decoder.dropped_bytes == 1000000 - 1024
    found, logs = decoder.feed(b"\x1dC" + b"0" * 100000 + b"\n" + reply().encode())
    assert found == [reply()]
    assert not logs
    assert len(decoder._frame) == len(decoder._line) == 0
    _, logs = decoder.feed(b"\xff" * 1024 + b"\n")
    assert len(logs[0].encode()) <= 1024


def test_legacy_bridge_never_becomes_native_control():
    native = reply().encode()
    legacy = native.replace(b"\x1dC", b"\x1dB", 1)
    decoder = Decoder()
    found, logs = decoder.feed(legacy + native)
    assert found == [reply()]
    assert logs == [legacy[:-1].decode()]
