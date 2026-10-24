"""Public fixed fixture and synthetic serial transport; never opens a USB port."""

import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from test_derby_smoke import Wire
from test_serial_http import serial_service as serial_service

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.client import call, connect
from cardputer_mcp.derby_fixed import (
    FIXTURE_DETAILS,
    MANIFEST,
    FixedCollector,
    parse_collected,
    parse_trial,
)
from cardputer_mcp.ports import PortInfo
from cardputer_mcp.serial_backend import SerialBackend

ROWS = (Path(__file__).parent / "fixtures" / "derby_fixed_v1.log").read_bytes()
EXIT = b"DERBY_DETAIL_EXIT v=1 session=1 clean=1\n"
TEARDOWN = b"I (0) app: APP_STOPPED\nI (0) shell: HOME_READY\n"
FIXTURE = ROWS + EXIT + TEARDOWN
SHA = hashlib.sha256(FIXTURE).hexdigest()


def replace_row(raw: bytes, row: int, old: bytes, new: bytes) -> bytes:
    lines = raw.splitlines(keepends=True)
    assert old in lines[row]
    lines[row] = lines[row].replace(old, new, 1)
    return b"".join(lines)


def test_public_fixture_and_known_work_are_bound_to_complete_trial():
    result = parse_trial(FIXTURE)
    assert result["transport_sha256"] == SHA
    assert (
        hashlib.sha256(
            json.dumps(FIXTURE_DETAILS, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == MANIFEST
    )
    assert result["fixture"]["id"] == "oval-pan8-390-510-v1"
    assert result["format_valid"] and result["work_verified"]
    assert [row["rep"] for row in result["repetitions"]] == ["1", "2", "3"]


@pytest.mark.parametrize("variant", ["native", "batch"])
def test_variant_counters_and_bridge_calls_are_verified(variant):
    raw = FIXTURE.replace(b"variant=baseline", f"variant={variant}".encode())
    if variant == "native":
        raw = raw.replace(b"c4=7735", b"c4=120")
        raw = raw.replace(b"c5=0", b"c5=7735")
        raw = raw.replace(b"c7=0", b"c7=120")
    else:
        raw = raw.replace(b"c8=0", b"c8=120")
        raw = raw.replace(b"c9=0", b"c9=960")
        raw = raw.replace(b":2086", b":1246")
    assert parse_trial(raw)["variant"] == variant
    with pytest.raises(ValueError):
        parse_trial(raw.replace(b"c3=7735", b"c3=7734", 1))


def test_public_transport_receipt_requires_exact_bytes_and_quality():
    receipt = {
        "complete": True,
        "bytes": len(FIXTURE),
        "duration_ms": 180000,
        "sha256": SHA,
        **dict.fromkeys(
            ("gaps", "overflow", "truncated", "partial_tail", "disconnect", "reset", "refused"),
            False,
        ),
    }
    assert parse_collected(FIXTURE, receipt)["work_verified"] is True
    with pytest.raises(ValueError):
        parse_collected(FIXTURE, receipt | {"gaps": True})
    with pytest.raises(ValueError):
        parse_collected(FIXTURE, receipt | {"bytes": len(FIXTURE) - 1})


@pytest.mark.parametrize(
    "damage",
    [
        lambda raw: raw[:-1],
        lambda raw: b"\xff\n" + raw,
        lambda raw: raw.replace(b"DERBY_FIXED_END", b"DERBY_NOT_END"),
        lambda raw: raw + raw.splitlines(keepends=True)[2],
        lambda raw: replace_row(raw, 2, b"session=1", b"session=2"),
        lambda raw: replace_row(raw, 2, b"rep=2", b"rep=3"),
        lambda raw: replace_row(raw, 2, b"variant=baseline", b"variant=native"),
        lambda raw: replace_row(raw, 2, b"c1=120", b"c1=0"),
        lambda raw: replace_row(raw, 2, b"c3=7735", b"c3=0"),
        lambda raw: replace_row(raw, 2, b"s1=5352:2086", b"s1=5352:0"),
        lambda raw: replace_row(raw, 2, b"valid=1", b"valid=0"),
        lambda raw: replace_row(raw, 2, b"finish=5DC86E18", b"finish=00000000"),
        lambda raw: replace_row(raw, 2, b"wall_us=973111", b"wall_us=1"),
        lambda raw: raw + b"DERBY_FIXED fake=" + b"x" * 1024 + b"\n",
    ],
)
def test_any_bad_or_partial_repetition_invalidates_the_whole_trial(damage):
    with pytest.raises(ValueError):
        parse_trial(damage(FIXTURE))


def test_host_collector_builds_receipt_from_wire_bytes_not_device_claims():
    collector = FixedCollector(1.0)
    raw = b"APP_ID local.derby\nDERBY READY tones=true\n" + FIXTURE
    for at in range(0, len(raw), 17):
        part = raw[at : at + 17]
        collector.feed(part)
        for line in part.splitlines():
            if line == b"APP_STOPPED":
                collector.observe_line(line)
    for line in raw.splitlines():
        if line in (b"APP_ID local.derby", b"DERBY READY tones=true"):
            collector.observe_line(line)
        elif line.startswith((b"DERBY_FIXED_END", b"DERBY_DETAIL_EXIT")):
            collector.observe_line(line)
        elif line.endswith(b"APP_STOPPED"):
            collector.observe_line(b"APP_STOPPED")
        elif line.endswith(b"HOME_READY"):
            collector.observe_line(b"HOME_READY")
    trial = collector.snapshot(181.0, complete=True)
    assert trial.valid and trial.receipt == {
        "complete": True,
        "bytes": len(raw),
        "duration_ms": 180000,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "gaps": False,
        "overflow": False,
        "truncated": False,
        "partial_tail": False,
        "disconnect": False,
        "reset": False,
        "refused": False,
    }
    assert trial.parsed["transport_sha256"] == trial.receipt["sha256"]
    collector.feed(b"partial")
    failed = collector.snapshot(182.0, complete=True)
    assert not failed.valid and failed.receipt["partial_tail"]


def test_collector_rejects_complete_overlong_line_and_bounded_overflow():
    collector = FixedCollector(0.0)
    with pytest.raises(ValueError, match="fixed_line_limit"):
        collector.feed(b"x" * 1024 + b"\n")
    assert collector.snapshot(1.0).receipt["overflow"] is True
    collector = FixedCollector(0.0)
    with pytest.raises(ValueError, match="fixed_transport_overflow"):
        collector.feed(b"x" * 65537)
    trial = collector.snapshot(1.0)
    assert len(trial.raw) == 65536 and trial.receipt["overflow"] is True
    assert trial.receipt["complete"] is False


def test_reset_and_disconnect_are_collector_owned_failure_facts():
    collector = FixedCollector(0.0)
    collector.feed(FIXTURE)
    collector.observe_line(b"HOME_READY")
    trial = collector.snapshot(1.0, complete=True, disconnect=True)
    assert not trial.valid
    assert trial.receipt["reset"] and trial.receipt["disconnect"]


class FixedWire(Wire):
    def __init__(
        self,
        raw: bytes = FIXTURE,
        *,
        partial: bool = False,
        post_home: bytes = b"",
        payload_chunk: int = 27,
    ):
        super().__init__(phases=False)
        self.raw, self.partial, self.sent = raw, partial, False
        self.post_home, self.payload_chunk = post_home, payload_chunk

    def read(self, size=1):
        if self.running and not self.queue and not self.sent:
            self.sent = True
            ending = b"I (0) app: APP_STOPPED\nI (0) shell: HOME_READY\n"
            payload = self.raw if self.partial else self.raw + self.post_home
            if not self.partial and not self.raw.rstrip(b"\r\n").endswith(b"HOME_READY"):
                payload = self.raw + ending + self.post_home
            self.queue.extend(
                payload[at : at + self.payload_chunk]
                for at in range(0, len(payload), self.payload_chunk)
            )
        if self.partial and self.sent and not self.queue:
            self.now += 201
            return b""
        return super().read(size)


class CancelWire(FixedWire):
    def __init__(self):
        super().__init__()
        self.stop = None
        self.work_reads = 0

    def read(self, size=1):
        data = super().read(size)
        if self.running and self.sent and data:
            self.work_reads += 1
            if self.work_reads == 4:
                assert self.stop is not None
                self.stop()
        return data


def backend_for(wire: FixedWire, config):
    return SerialBackend(
        config,
        inventory=lambda: [
            PortInfo(
                port="COM42",
                vid=config.selector.vid,
                pid=config.selector.pid,
                serial_number=config.selector.serial_number,
            )
        ],
        factory=lambda: wire,
        clock=lambda: wire.now,
    )


async def test_fixed_workload_uses_explicit_owned_http_path_and_private_raw(serial_service):
    url, resident, _ = serial_service
    wire = FixedWire()
    resident.backend = backend_for(wire, resident.settings.serial)
    async with connect(url, "a" * 64) as owner, connect(url, "b" * 64) as other:
        lease = await call(
            owner, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 300}
        )
        prepared = await call(
            owner,
            "submit",
            {"session": lease["session"], "job_id": "fixed-one", "kind": "derby_fixed"},
        )
        assert prepared["ok"] and prepared["plan"]["effects"]
        persisted = resident._store().plan(prepared["plan_hash"])
        assert digest_plan(persisted) == prepared["plan_hash"], [
            key for key in prepared["plan"] if prepared["plan"][key] != persisted[key]
        ]
        assert wire.writes == [] and not wire.is_open
        stolen = await call(
            other,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        assert stolen["error"] == "authenticated_owner_mismatch"
        assert digest_plan(resident._store().plan(prepared["plan_hash"])) == prepared["plan_hash"]
        assert wire.writes == []
        executed = await call(
            owner,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        assert executed["ok"], executed
        await resident.queue.join()
        owned = await call(owner, "status", {"view": "job", "job_id": "fixed-one"})
        shared = await call(other, "status", {"view": "job", "job_id": "fixed-one"})
    assert owned["state"] == shared["state"] == "succeeded"
    assert owned["result"] == "derby_fixed_trial_verified"
    assert owned["fixed_trial"]["valid"] is True
    binding = owned["fixed_trial"]["binding"]
    assert binding["job_id"] == "fixed-one"
    assert binding["plan_hash"] == prepared["plan_hash"]
    assert binding["principal_id"] == resident.session.principal_id
    assert binding["broker_epoch"] == prepared["plan"]["broker_epoch"]
    assert binding["device_fingerprint"] == prepared["plan"]["device_fingerprint"]
    assert (
        binding["configured_expected_firmware_sha256"]
        == prepared["plan"]["expected_firmware_sha256"]
    )
    assert binding["device_reported_session"] == "1"
    assert binding["installed_boot_source_image"] == "unverified"
    assert (
        owned["fixed_trial"]["receipt"]["sha256"]
        == hashlib.sha256(base64.b64decode(owned["fixed_trial"]["raw_base64"])).hexdigest()
    )
    assert "fixed_trial" not in shared and "raw_base64" not in json.dumps(shared)
    with sqlite3.connect(resident.settings.state_dir / "history.sqlite3") as db:
        saved = db.execute(
            "SELECT raw,sha256,principal_id,plan_hash FROM derby_fixed_trials WHERE job_id=?",
            ("fixed-one",),
        ).fetchone()
    assert saved == (
        base64.b64decode(owned["fixed_trial"]["raw_base64"]),
        owned["fixed_trial"]["receipt"]["sha256"],
        resident.session.principal_id,
        prepared["plan_hash"],
    )
    assert wire.closed and wire.writes == [b"\x1b", b"a", *([b"u"] * 16), *([b"d"] * 12), b"e"]
    assert resident.backend.calls == 1 and resident.backend.active == 0


@pytest.mark.parametrize("ending", [b"\r\n", b"\r\r\n"])
async def test_crlf_serial_trial_preserves_exact_raw_bytes(serial_service, ending):
    url, resident, _ = serial_service
    expected_raw = FIXTURE.replace(b"\n", ending)
    wire = FixedWire(expected_raw)
    resident.backend = backend_for(wire, resident.settings.serial)
    async with connect(url, "a" * 64) as owner:
        lease = await call(
            owner, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 300}
        )
        prepared = await call(
            owner,
            "submit",
            {"session": lease["session"], "job_id": "fixed-crlf", "kind": "derby_fixed"},
        )
        await call(
            owner,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        await resident.queue.join()
        owned = await call(owner, "status", {"view": "job", "job_id": "fixed-crlf"})
    assert owned["state"] == "succeeded"
    raw = base64.b64decode(owned["fixed_trial"]["raw_base64"])
    assert expected_raw in raw
    assert owned["fixed_trial"]["receipt"]["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("chunk_size", [27, 4096], ids=["quiet_drain", "coalesced"])
async def test_same_app_relaunch_after_home_invalidates_trial(serial_service, chunk_size):
    url, resident, _ = serial_service
    relaunch = b"APP_ID local.derby\nDERBY READY tones=true\n"
    wire = FixedWire(post_home=relaunch, payload_chunk=chunk_size)
    resident.backend = backend_for(wire, resident.settings.serial)
    async with connect(url, "a" * 64) as owner, connect(url, "b" * 64) as other:
        lease = await call(
            owner, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 300}
        )
        prepared = await call(
            owner,
            "submit",
            {"session": lease["session"], "job_id": "fixed-relaunch", "kind": "derby_fixed"},
        )
        await call(
            owner,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        await resident.queue.join()
        owned = await call(owner, "status", {"view": "job", "job_id": "fixed-relaunch"})
        shared = await call(other, "status", {"view": "job", "job_id": "fixed-relaunch"})
    assert owned["state"] == shared["state"] == "needs_review"
    assert owned["fixed_trial"]["valid"] is False
    assert owned["fixed_trial"]["receipt"]["reset"] is True
    assert relaunch in base64.b64decode(owned["fixed_trial"]["raw_base64"])
    assert "fixed_trial" not in shared
    assert wire.writes == [b"\x1b", b"a", *([b"u"] * 16), *([b"d"] * 12), b"e"]


@pytest.mark.parametrize("partial", [False, True])
async def test_bad_work_or_partial_transport_keeps_review_and_private_raw(serial_service, partial):
    url, resident, _ = serial_service
    raw = FIXTURE if partial else replace_row(FIXTURE, 2, b"c1=120", b"c1=0")
    wire = FixedWire(raw, partial=partial)
    resident.backend = backend_for(wire, resident.settings.serial)
    async with connect(url, "a" * 64) as owner, connect(url, "b" * 64) as other:
        lease = await call(
            owner, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 300}
        )
        prepared = await call(
            owner,
            "submit",
            {"session": lease["session"], "job_id": "fixed-bad", "kind": "derby_fixed"},
        )
        await call(
            owner,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        await resident.queue.join()
        owned = await call(owner, "status", {"view": "job", "job_id": "fixed-bad"})
        shared = await call(other, "status", {"view": "job", "job_id": "fixed-bad"})
    assert owned["state"] == shared["state"] == "needs_review"
    assert owned["fixed_trial"]["valid"] is False
    assert owned["fixed_trial"]["receipt"]["complete"] is (not partial)
    assert raw[:100] in base64.b64decode(owned["fixed_trial"]["raw_base64"])
    assert "fixed_trial" not in shared
    assert wire.closed and resident.backend.calls == 1 and resident.backend.active == 0


async def test_stop_during_transport_retains_bounded_private_partial_raw(serial_service):
    url, resident, _ = serial_service
    wire = CancelWire()
    backend = backend_for(wire, resident.settings.serial)
    wire.stop = backend.smoke_stop.set
    resident.backend = backend
    async with connect(url, "a" * 64) as owner, connect(url, "b" * 64) as other:
        lease = await call(
            owner, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 300}
        )
        prepared = await call(
            owner,
            "submit",
            {"session": lease["session"], "job_id": "fixed-stop", "kind": "derby_fixed"},
        )
        await call(
            owner,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        await resident.queue.join()
        owned = await call(owner, "status", {"view": "job", "job_id": "fixed-stop"})
        shared = await call(other, "status", {"view": "job", "job_id": "fixed-stop"})
    assert owned["state"] == "needs_review" and shared["state"] == "needs_review"
    assert owned["fixed_trial"]["valid"] is False
    assert owned["fixed_trial"]["receipt"]["complete"] is False
    assert owned["fixed_trial"]["receipt"]["bytes"] > 0
    assert "fixed_trial" not in shared
    assert backend.calls == 1 and backend.active == 0 and wire.closed


_FAULTS = [b"InternalError: interrupted\n", b"APP ERROR\n", b"stop hook did not finish\n"]
_WHERES = ["after_end", "after_stop", "after_home"]
# Every fault x position pair is kept; the chunking axis (marker split across reads
# versus one read) alternates across the pairs so both transports hit every fault
# and every position at least once.
_FAULT_CASES = [
    pytest.param(
        fault,
        where,
        17 if (fi + wi) % 2 == 0 else 4096,
        id=f"{where}-f{fi}-{'fragmented' if (fi + wi) % 2 == 0 else 'coalesced'}",
    )
    for fi, fault in enumerate(_FAULTS)
    for wi, where in enumerate(_WHERES)
]


@pytest.mark.parametrize("fault,where,chunk_size", _FAULT_CASES)
async def test_fault_after_workload_end_never_reports_terminal_success(
    serial_service, fault, where, chunk_size
):
    url, resident, _ = serial_service
    if where == "after_end":
        raw, post_home = ROWS + fault + EXIT, b""
    elif where == "after_stop":
        raw, post_home = ROWS + EXIT + b"APP_STOPPED\n" + fault, b""
    else:
        raw, post_home = FIXTURE, fault
    wire = FixedWire(raw, post_home=post_home, payload_chunk=chunk_size)
    resident.backend = backend_for(wire, resident.settings.serial)
    async with connect(url, "a" * 64) as owner, connect(url, "b" * 64) as other:
        lease = await call(
            owner, "session", {"action": "open", "owner": "local_cli", "lease_seconds": 300}
        )
        prepared = await call(
            owner,
            "submit",
            {"session": lease["session"], "job_id": "fixed-fault", "kind": "derby_fixed"},
        )
        await call(
            owner,
            "execute",
            {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
        )
        await resident.queue.join()
        owned = await call(owner, "status", {"view": "job", "job_id": "fixed-fault"})
        shared = await call(other, "status", {"view": "job", "job_id": "fixed-fault"})
    assert owned["state"] == shared["state"] == "needs_review"
    assert owned["result"] == "serial_fault_marker"
    assert owned["diagnostic"]["device_fault_category"] in ("vm_exception", "device_fault")
    assert owned["diagnostic"]["serial_close_confirmed"] is True
    assert owned["diagnostic"]["workload_end_observed"] is True
    assert owned["diagnostic"]["partial_workload_is_terminal_success"] is False
    assert owned["fixed_trial"]["valid"] is False
    assert owned["fixed_trial"]["receipt"]["complete"] is False
    assert "fixed_trial" not in shared
    assert "InternalError" not in json.dumps(shared)
    assert wire.closed and resident.backend.active == 0
