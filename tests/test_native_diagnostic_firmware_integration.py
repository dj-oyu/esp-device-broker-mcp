"""Offline cross-implementation tests against exact public firmware C blobs.

Set FIRMWARE_NATIVE_SOURCE_DIR to an already admitted source directory. Tests
never fetch source, connect to hardware or modify project/permission settings.
"""

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from cardputer_mcp.native_diagnostic_protocol import ZERO_BOOT, ZERO_ID, command, parse

FIRMWARE_COMMIT = "cddaec23ce52f739b2b03e5c77efdb2b2632b06c"
PINS = {
    "diagnostic_control.c": "954ac02ef8e87e036b37978c29e3b2c0fac9c851",
    "diagnostic_control.h": "c83b885f4ff9968b28899ba0de27336251f9d0d3",
    "diagnostic_wire.c": "c776f8565440548cde4459b2a1d11b81586daf03",
    "diagnostic_wire.h": "fee6f5c8b57c095d8719ca4309c43d26caa7ebaa",
    "diagnostic_session.h": "3c11e8b37c2f49308d0d6f67885bc7a073b880b5",
    "diagnostic_lifecycle.h": "1f465379b3ff9d2f6d0a98f7368983f730db5863",
    "diagnostic_command.h": "e5684b6243d9815958301d993e65c58c3af94baf",
}
BOOT = "b" * 16
ID = "a" * 32
FIXTURES = Path(__file__).parent / "fixtures"
VECTORS = json.loads((FIXTURES / "native_diagnostic_v1.json").read_text())


class Firmware:
    def __init__(self, lib):
        self.lib = lib
        assert lib.bridge_init(1)

    def feed(self, raw):
        count = self.lib.bridge_feed(raw, len(raw))
        return ctypes.string_at(self.lib.bridge_output(), count)

    def operation(self, op, identity=ID, boot=BOOT):
        return parse(self.feed(command(op, identity, boot)))

    def pump(self, admitted=True, fail=0):
        return self.lib.bridge_pump(admitted, fail)

    def during(self, raw):
        self.lib.bridge_during(raw, len(raw))


@pytest.fixture(scope="module")
def compiled(tmp_path_factory):
    configured = os.environ.get("FIRMWARE_NATIVE_SOURCE_DIR")
    if not configured:
        pytest.skip("exact firmware source not supplied; no automatic network fetch")
    root = Path(configured) / "main/ui"
    for name, expected in PINS.items():
        raw = (root / name).read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        assert actual == expected, f"firmware source identity mismatch: {name}"
    compiler = shutil.which("cc")
    if compiler is None or os.name != "posix":
        pytest.skip("POSIX host C compiler required for shared-library integration")
    out = tmp_path_factory.mktemp("native-fw") / "firmware.so"
    subprocess.run(
        [
            compiler,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wpedantic",
            "-O2",
            "-fPIC",
            "-shared",
            "-I" + str(root),
            str(root / "diagnostic_control.c"),
            str(root / "diagnostic_wire.c"),
            str(FIXTURES / "native_diagnostic_firmware_bridge.c"),
            "-o",
            str(out),
        ],
        check=True,
    )
    lib = ctypes.CDLL(str(out))
    lib.bridge_feed.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.bridge_feed.restype = ctypes.c_size_t
    lib.bridge_output.restype = ctypes.c_void_p
    lib.bridge_during.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
    lib.bridge_boot.argtypes = [ctypes.c_int, ctypes.c_uint64]
    return lib


@pytest.fixture
def fw(compiled):
    return Firmware(compiled)


def vector(name):
    return next(row for row in VECTORS["responses"] if row["name"] == name)


def test_actual_firmware_request_and_reply_bytes_match_shared_vectors(fw):
    bootstrap = next(row for row in VECTORS["requests"] if row["name"] == "bootstrap")
    assert fw.feed(bytes.fromhex(bootstrap["hex"])) == bytes.fromhex(vector("bootstrap")["hex"])
    request = next(row for row in VECTORS["requests"] if row["name"] == "request")
    assert fw.feed(bytes.fromhex(request["hex"])) == bytes.fromhex(vector("pending")["hex"])
    fw.pump()
    raw = fw.feed(command("STATUS", ID, BOOT))
    assert raw == bytes.fromhex(vector("completed")["hex"])
    assert parse(raw).completed
    # The same immutable ID can neither rerun work nor lose its terminal receipt.
    assert fw.operation("REQUEST").completed
    assert fw.operation("CANCEL").completed
    assert fw.lib.bridge_work_count() == 1


def test_actual_firmware_disabled_nonce_unknown_and_reserved_ids(fw):
    assert fw.operation("STATUS", ZERO_ID, ZERO_BOOT).state == "REFUSED"
    for raw in (
        command("STATUS", ID, BOOT),
        command("CANCEL", ID, BOOT),
        command("REQUEST", ID, "c" * 16),
        f"\x1dD1 REQUEST {ZERO_ID} {BOOT}\n".encode(),
        f"\x1dD1 CANCEL {ZERO_ID} {BOOT}\n".encode(),
    ):
        actual = parse(fw.feed(raw))
        assert (actual.state, actual.flags) == ("REFUSED", 0)
    assert fw.feed(command("STATUS", ID, BOOT)) == bytes.fromhex(vector("unknown")["hex"])
    assert not fw.lib.bridge_busy() and fw.lib.bridge_work_count() == 0
    assert fw.lib.bridge_init(0)
    raw = fw.feed(command("REQUEST", ID, BOOT))
    assert raw == bytes.fromhex(vector("disabled")["hex"])
    assert not parse(raw).enabled


def test_actual_eight_slot_capacity_busy_refusals_and_no_eviction(fw):
    assert fw.operation("REQUEST").state == "PENDING"
    for number in range(1, 8):
        receipt = fw.operation("REQUEST", f"{number:032x}")
        assert (receipt.state, receipt.flags) == ("REFUSED", 0x20)
    fw.pump()
    assert fw.operation("STATUS").completed
    assert fw.operation("REQUEST", "f" * 32).state == "REFUSED"
    assert fw.operation("REQUEST", "0" * 31 + "1").state == "REFUSED"
    assert fw.operation("REQUEST").completed
    assert fw.lib.bridge_work_count() == 1


def test_actual_new_boot_refuses_old_nonce_without_restarting_old_request(fw):
    fw.operation("REQUEST")
    fw.pump()
    assert fw.operation("STATUS").completed
    assert fw.lib.bridge_boot(1, int("c" * 16, 16))
    reply = parse(fw.feed(command("REQUEST", ID, BOOT)))
    assert (reply.state, reply.flags, reply.boot) == ("REFUSED", 0, "c" * 16)
    assert fw.lib.bridge_work_count() == 0 and not fw.lib.bridge_busy()


@pytest.mark.parametrize("cancel", [False, True])
def test_actual_pre_work_cancel_and_admission_refusal(fw, cancel):
    fw.operation("REQUEST")
    if cancel:
        receipt = fw.operation("CANCEL")
        assert (receipt.state, receipt.flags) == ("CANCEL_REQUESTED", 0x20)
    fw.pump(admitted=cancel)
    receipt = fw.operation("STATUS")
    raw = fw.feed(command("STATUS", ID, BOOT))
    assert raw == bytes.fromhex(vector("cancelled_before_work" if cancel else "refused")["hex"])
    if cancel:
        assert receipt.cancelled and receipt.flags == 0x3E
        assert fw.lib.bridge_release_count() == 1
    else:
        assert (receipt.state, receipt.flags) == ("REFUSED", 0x20)
    assert fw.lib.bridge_work_count() == 0


@pytest.mark.parametrize(
    "operation,expected", [("STATUS", "RUNNING"), ("CANCEL", "CANCEL_REQUESTED")]
)
def test_actual_wire_is_responsive_inside_synchronous_work_callback(fw, operation, expected):
    fw.operation("REQUEST")
    fw.during(command(operation, ID, BOOT))
    fw.pump()
    raw = ctypes.string_at(
        fw.lib.bridge_output(),
        len(
            bytes.fromhex(vector("running" if operation == "STATUS" else "cancel_requested")["hex"])
        ),
    )
    during = parse(raw)
    assert raw == bytes.fromhex(
        vector("running" if operation == "STATUS" else "cancel_requested")["hex"]
    )
    assert during.state == expected and during.flags == 0
    terminal = fw.operation("STATUS")
    raw = fw.feed(command("STATUS", ID, BOOT))
    assert raw == bytes.fromhex(
        vector("completed" if operation == "STATUS" else "cancelled_after_work")["hex"]
    )
    assert terminal.completed if operation == "STATUS" else terminal.cancelled
    assert terminal.flags == 0x1F


@pytest.mark.parametrize("failure", [2, 3, 4])
def test_actual_lifecycle_failure_quarantines_without_releasing_owner(fw, failure):
    fw.operation("REQUEST")
    fw.pump(fail=failure)
    receipt = fw.operation("STATUS")
    if failure == 3:
        assert fw.feed(command("STATUS", ID, BOOT)) == bytes.fromhex(vector("quarantined")["hex"])
    assert receipt.state == "QUARANTINED" and not receipt.completed and not receipt.cancelled
    assert fw.lib.bridge_busy() and fw.lib.bridge_release_count() == 0
    assert fw.operation("REQUEST").state == "QUARANTINED"
    assert fw.lib.bridge_work_count() <= 1


@pytest.mark.parametrize(
    "raw",
    [
        b"\x1dD1 NOPE x\n",
        b"\x1dD1 REQUEST " + b"x" * 100 + b"\n",
        command("REQUEST", ID, BOOT).replace(b"\n", b"\r\n"),
    ],
)
def test_actual_wire_rejects_malformed_frames_then_resynchronizes(fw, raw):
    assert fw.feed(raw) == b""
    assert fw.operation("STATUS", ZERO_ID, ZERO_BOOT).flags == 0
    assert fw.lib.bridge_work_count() == 0


@pytest.mark.parametrize(
    "scenario",
    ["complete", "cancel", "quarantined", "timeout", "disabled", "authority", "close_failure"],
)
async def test_production_broker_runner_against_actual_firmware_wire_and_controller(fw, scenario):
    from test_native_diagnostic_serial import fixture

    from cardputer_mcp.serial_backend import SerialFailure

    backend, port, plan = fixture("close_failure" if scenario == "close_failure" else "complete")
    if scenario == "disabled":
        assert fw.lib.bridge_init(0)

    def write(raw):
        port.writes.append(raw)
        _, operation, identity, _ = raw.decode().removesuffix("\n").split(" ")
        if operation == "STATUS" and identity != ZERO_ID:
            fw.pump(fail=3 if scenario == "quarantined" else 0)
        response = fw.feed(raw)
        if response and not (
            scenario == "timeout" and identity != ZERO_ID and operation == "STATUS"
        ):
            port.incoming.append(response)
        if operation == "REQUEST":
            port.request_id = identity
            if scenario == "cancel":
                backend.request_native_cancel()
        return len(raw)

    port.write = write

    def live():
        return scenario != "authority" or port.request_id is None

    failures = {
        "quarantined": "native_diagnostic_quarantined",
        "timeout": "native_diagnostic_timeout",
        "disabled": "native_diagnostic_not_enabled",
        "authority": "operation_interrupted_review_required",
        "close_failure": "serial_close_unconfirmed",
    }
    if scenario in failures:
        with pytest.raises(SerialFailure, match="^" + failures[scenario] + "$"):
            await backend.execute_native(plan, lambda _: None, live)
        assert backend.ownership_uncertain is (scenario != "disabled")
    else:
        result = await backend.execute_native(plan, lambda _: None, live)
        assert result.code == (
            "native_diagnostic_completed"
            if scenario == "complete"
            else "native_diagnostic_cancelled"
        )
        if scenario == "cancel":
            assert result.native_diagnostic.flags == 0x3E
            assert fw.lib.bridge_work_count() == 0
        else:
            assert result.native_diagnostic.flags == 0x1F
            assert fw.lib.bridge_work_count() == 1
        assert not backend.ownership_uncertain and backend.active == 0
    assert port.closed.is_set()
    assert sum(b" REQUEST " in raw for raw in port.writes) <= 1
    if scenario == "authority":
        assert not any(b" CANCEL " in raw for raw in port.writes)
        assert sum(b" STATUS " in raw for raw in port.writes) == 1
