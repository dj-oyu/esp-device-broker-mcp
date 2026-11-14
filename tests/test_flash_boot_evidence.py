"""Real boot collector to terminal broker failure to authenticated MCP log reads.

ROM operations and USB ports are synthetic; the real driver, serial reader,
flash lifecycle, broker worker, authentication and log API execute unchanged.
"""

import json
from dataclasses import replace
from datetime import datetime

import pytest
from test_flash_lifecycle import FakeDriver
from test_registry_http import registry_service as registry_service
from test_serial_restart import STARTUP, device

from cardputer_mcp.client import call, connect
from cardputer_mcp.flash_driver import NativeFlashDriver
from cardputer_mcp.flash_models import FlashDevice
from cardputer_mcp.serial_backend import fingerprint


@pytest.mark.parametrize(
    "scenario",
    [
        "elf_mismatch",
        "timeout",
        "transport_and_close",
        "invalid_and_close",
        "line_limit",
    ],
)
async def test_terminal_boot_failure_retains_owned_redacted_evidence(
    registry_service, tmp_path, monkeypatch, scenario
):
    service = registry_service
    broker = service.resident
    elf = "abcdef0123456789" + "0" * 48
    registration = FlashDevice(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        flash_bytes=2097152,
        project_id="fixture",
        python_tool={
            "executable": tmp_path / "python.exe",
            "sha256": "a" * 64,
            "version": "Python 3.13.16",
        },
        esptool_directory=tmp_path / "tools",
        esptool_sha256="b" * 64,
        backup_directory=tmp_path / "backup",
        private_backup_acl_confirmed=True,
        original_elf_sha256=elf,
        allow_backup=True,
        allow_restore=True,
    )
    broker.settings = broker.settings.model_copy(
        update={"flash_devices": {"fixture": registration}}
    )
    broker.firmware_flash.settings = broker.settings
    rom = FakeDriver()
    rom.data = bytearray(b"x" * registration.flash_bytes)
    rom.security = replace(rom.security, flash_bytes=registration.flash_bytes)
    broker.firmware_flash.driver_factory = lambda *args: rom
    owner_key = service.keys["second"]
    console = []
    monkeypatch.setattr("cardputer_mcp.build_logs.console_line", lambda *args: console.append(args))
    monkeypatch.setenv("SYNTHETIC_TEST_SECRET", "ENV_SECRET_WITHOUT_LABEL")
    async with connect(service.url, owner_key.token) as owner:
        lease = await call(
            owner, "session", {"action": "open", "owner": "remote_mcp", "lease_seconds": 30}
        )

        async def execute(job, action, **fields):
            prepared = await call(
                owner,
                "submit",
                {
                    "session": lease["session"],
                    "job_id": job,
                    "kind": action,
                    "device_id": "fixture",
                    **fields,
                },
            )
            assert prepared["ok"], prepared
            if action in {"flash", "restore"}:
                result = await call(
                    owner,
                    "flash",
                    {"session": lease["session"], "plan_hash": prepared["plan_hash"]},
                )
            else:
                result = await call(
                    owner,
                    "execute",
                    {"op": "run", "session": lease["session"], "plan_hash": prepared["plan_hash"]},
                )
            assert result["ok"], result
            await broker.queue.join()

        await execute("boot-backup", "backup")
        assert broker.job("boot-backup")["state"] == "succeeded"
        startup = STARTUP.replace(b"v5.5-test", b"v6.0.1").replace(
            b"v0.0.7-test", b"archive/20261021/ci/full-contra"
        )
        note = b"error: SYNTHETIC_BOOT_NOTE ENV_SECRET_WITHOUT_LABEL\n"
        sensitive = (
            b"token=SYNTHETIC_PRIVATE_BOOT\n-----BEGIN PRIVATE KEY-----\n"
            b"PRIVATE_PEM_BODY\n-----END PRIVATE KEY-----\n"
        )
        if scenario == "elf_mismatch":
            startup = startup.replace(b"abcdef0123456789", b"1111111111111111")
        elif scenario == "idf_mismatch":
            startup = startup.replace(b"v6.0.1", b"v6.0.2")
        elif scenario in {"invalid_version", "invalid_and_close"}:
            startup = startup.replace(b"archive/20261021/ci/full-contra", b"v1/test extra")
        elif scenario in {
            "timeout",
            "transport",
            "transport_and_close",
            "line_limit",
            "late_chunk",
        }:
            startup = b""
        data = note + sensitive + startup
        if scenario == "line_limit":
            data += b"x" * 1025 + b"\n"
        chunks = [data]
        if scenario in {"transport", "transport_and_close"}:
            chunks.append(RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION"))
        serial, ports, inventory, created = device(chunks=chunks)
        ports[0].close_error = scenario in {"transport_and_close", "invalid_and_close"}
        if scenario == "late_chunk":
            ports[0].read_seconds = 31
        drivers = []

        def factory(reg, backend, usb, work, app_hash, expected_elf, live):
            assert expected_elf == elf
            driver = NativeFlashDriver(
                reg, serial, fingerprint(inventory[0]), work, app_hash, expected_elf, live
            )
            driver._verified_port = lambda: inventory[0].port
            driver.probe, driver.read, driver.write = rom.probe, rom.read, rom.write
            drivers.append(driver)
            return driver

        broker.firmware_flash.driver_factory = factory
        await execute("boot-failed", "restore", backup_job_id="boot-backup")
        assert (await call(owner, "status", {"view": "job", "job_id": "boot-failed"}))[
            "state"
        ] == "needs_review"
        assert rom.writes == 1  # No retry or automatic restore after the explicit operation.
        assert len(created) == 1 and all(not port.writes for port in ports)
        assert ports[0].closes == 1 and ports[1].opens == 0
        assert drivers[0].last_observation is drivers[0].last_boot_attempt is None
        assert broker._store().firmware_current("fixture") is None
        assert "SYNTHETIC_BOOT_NOTE" not in "\n".join(broker._store().db.iterdump())
        request = {
            "job_id": "boot-failed",
            "limit": 100,
            "levels": ["error", "warning", "info", "debug", "unknown"],
        }
        result = await call(owner, "status", {"view": "logs", "stream": "build", **request})
        assert result["ok"] and not result["expired"]
        texts = [entry["text"] for entry in result["entries"]]
        evidence_text = next(
            line.split("boot_diagnostic ", 1)[1] for line in texts if "boot_diagnostic " in line
        )
        assert len(evidence_text) < 4096
        evidence = json.loads(evidence_text)
        observation = evidence["observation"]
        assert (
            observation["bytes_received"] > 0
            if scenario != "late_chunk"
            else observation["bytes_received"] == 0
        )
        assert (
            observation["complete_lines"] > 0
            if scenario != "late_chunk"
            else observation["capture_gap"]
        )
        assert "SYNTHETIC_BOOT_NOTE" in "\n".join(texts) if scenario != "late_chunk" else True
        assert "SYNTHETIC_PRIVATE" not in json.dumps(result)
        assert "PRIVATE_PEM_BODY" not in json.dumps(result)
        assert "ENV_SECRET_WITHOUT_LABEL" not in json.dumps(result)
        assert "SYNTHETIC_BOOT_NOTE" not in repr(console)
        assert "boot_diagnostic" not in repr(console)
        attempt = evidence["attempt"]
        assert datetime.fromisoformat(attempt["finished_utc"]) >= datetime.fromisoformat(
            attempt["started_utc"]
        )
        assert attempt["attempt_elapsed_ms"] >= 0 and attempt["observation_elapsed_ms"] >= 0
        if scenario in {"transport", "transport_and_close"}:
            assert attempt["reason"] == (
                "serial_read_failed" if scenario == "transport" else "serial_close_unconfirmed"
            )
        if scenario == "transport_and_close":
            assert attempt["primary_failure_code"] == "serial_read_failed"
        if scenario in {"invalid_version", "invalid_and_close"}:
            assert observation["firmware"]["reason"] == "invalid_startup_field"
        if scenario in {"timeout", "late_chunk"}:
            assert observation["firmware"]["reason"] == "no_startup_block"
        if scenario in {"elf_mismatch", "idf_mismatch"}:
            assert evidence["verification_reason"] == (
                "elf_sha256_mismatch" if scenario == "elf_mismatch" else "idf_version_mismatch"
            )
        if scenario == "line_limit":
            assert observation["truncated"] and observation["omitted_lines"] > 0
        async with connect(service.url, service.keys["reader"].token) as foreign:
            # Cross-principal reads: the same redacted body the owner gets.
            other = await call(foreign, "status", {"view": "logs", "stream": "build", **request})
            assert other == result
        cache = broker.firmware_flash.logs["boot-failed"]
        cache.deadline = 0
        expired = await call(owner, "status", {"view": "logs", "stream": "build", **request})
        # The RAM copy is gone; the durable copy answers with the same redacted lines.
        assert not expired["expired"] and expired["source"] == "durable_job_log"
        assert [entry["text"] for entry in expired["entries"]] == texts
        assert not cache.entries and cache.bytes == 0
        del broker.firmware_flash.logs["boot-failed"]
        lost = await call(owner, "status", {"view": "logs", "stream": "build", **request})
        assert not lost["expired"] and lost["source"] == "durable_job_log"
        assert [entry["text"] for entry in lost["entries"]] == texts
