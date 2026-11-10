"""Fixed frame bounds, fragmentation and terminal proofs; no serial/device IO."""

import pytest

from cardputer_mcp.native_diagnostic_protocol import (
    DEVICE_LIMIT,
    HOST_LIMIT,
    ZERO_BOOT,
    ZERO_ID,
    Decoder,
    command,
    parse,
)

ID = "a" * 32
BOOT = "b" * 16


def frame(state="RUNNING", flags="00", request_id=ID, boot=BOOT, enabled="1"):
    return f"\x1dND1 {request_id} {state} {flags} {boot} {enabled}\n".encode()


@pytest.mark.parametrize("operation", ["REQUEST", "STATUS", "CANCEL"])
def test_fixed_commands_fit_input_task_budget(operation):
    raw = command(operation, ID, BOOT)
    assert len(raw) <= HOST_LIMIT
    assert raw == f"\x1dD1 {operation} {ID} {BOOT}\n".encode()


def test_bootstrap_query_is_the_only_zero_identity_command():
    assert command("STATUS", ZERO_ID, ZERO_BOOT).startswith(b"\x1dD1 STATUS ")
    for operation, request_id, boot in (
        ("REQUEST", ZERO_ID, ZERO_BOOT),
        ("CANCEL", ZERO_ID, ZERO_BOOT),
        ("STATUS", ZERO_ID, BOOT),
        ("REQUEST", ID, ZERO_BOOT),
        ("status", ID, BOOT),
        ("REQUEST x", ID, BOOT),
        ("REQUEST", "A" * 32, BOOT),
        ("REQUEST", ID + "\n", BOOT),
    ):
        with pytest.raises(ValueError, match="^native_diagnostic_protocol_invalid$"):
            command(operation, request_id, boot)


@pytest.mark.parametrize(
    "state,flags", [("COMPLETED", "1f"), ("CANCELLED", "1f"), ("CANCELLED", "3e")]
)
def test_only_explicit_work_drain_home_and_release_proofs_complete(state, flags):
    snapshot = parse(frame(state, flags))
    assert snapshot.terminal
    assert snapshot.completed is (state == "COMPLETED")
    assert snapshot.cancelled is (state == "CANCELLED")


@pytest.mark.parametrize(
    "state,flags",
    [
        ("PENDING", "00"),
        ("RUNNING", "00"),
        ("CANCEL_REQUESTED", "00"),
        ("REFUSED", "20"),
        ("REFUSED", "00"),
        ("QUARANTINED", "07"),
    ],
)
def test_cancel_admission_refusal_and_quarantine_are_not_success(state, flags):
    snapshot = parse(frame(state, flags))
    assert not snapshot.completed and not snapshot.cancelled


@pytest.mark.parametrize(
    "raw",
    [
        frame("COMPLETED", "01"),
        frame("CANCELLED", "20"),
        frame("RUNNING", "21"),
        frame("REFUSED", "1f"),
        frame(boot=ZERO_BOOT),
        frame(enabled="true"),
        frame().replace(b"ND1", b"ND2"),
        frame().replace(b"\n", b"\r\n"),
        b"x" * (DEVICE_LIMIT + 1),
    ],
)
def test_invalid_or_contradictory_proof_is_refused(raw):
    with pytest.raises(ValueError, match="^native_diagnostic_protocol_invalid$"):
        parse(raw)


def test_response_fragmentation_is_bounded_at_every_split_and_noise_is_ignored():
    raw = frame("CANCEL_REQUESTED")
    assert len(raw) <= DEVICE_LIMIT
    for split in range(len(raw) + 1):
        decoder = Decoder()
        assert decoder.feed(b"untrusted ordinary log\n") == []
        actual = decoder.feed(raw[:split]) + decoder.feed(raw[split:])
        assert actual == [parse(raw)]
        assert not decoder.pending


def test_decoder_never_retains_unbounded_console_or_reserved_bytes():
    decoder = Decoder()
    assert decoder.feed(b"x" * 100_000) == []
    assert len(decoder.pending) == 0
    with pytest.raises(ValueError, match="^native_diagnostic_protocol_invalid$"):
        decoder.feed(b"\x1dND1 " + b"x" * 100_000)
    assert len(decoder.pending) == 0
    assert decoder.feed(frame("COMPLETED", "1f")) == [parse(frame("COMPLETED", "1f"))]


def test_next_reserved_frame_resynchronizes_a_partial_prefix():
    decoder = Decoder()
    assert decoder.feed(b"\x1dND1 partial") == []
    assert decoder.feed(frame()) == [parse(frame())]


def test_canonical_vectors_match_the_shared_firmware_contract():
    import json
    from pathlib import Path

    vectors = json.loads((Path(__file__).parent / "fixtures/native_diagnostic_v1.json").read_text())
    for vector in vectors["requests"]:
        assert command(vector["operation"], vector["id"], vector["boot"]).hex() == vector["hex"]
    for vector in vectors["responses"]:
        actual = parse(bytes.fromhex(vector["hex"]))
        assert (actual.request_id, actual.state, actual.flags, actual.boot, actual.enabled) == (
            vector["id"],
            vector["state"],
            int(vector["flags"], 16),
            vector["boot"],
            bool(vector["enabled"]),
        )
