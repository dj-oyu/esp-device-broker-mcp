"""Actual operator HTTP approval and MCP transport with a synthetic ROM driver."""

import asyncio
import hashlib
import json
import shutil
import threading
from dataclasses import replace

import httpx2
import test_flash_manifest_contract as manifest_tests
from test_flash_lifecycle import FakeDriver
from test_flash_lkg_geometry import table as partition_table
from test_registry_http import registry_service as registry_service

from cardputer_mcp.broker import digest_plan
from cardputer_mcp.build_locations import build_location, claim_build_directory
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject
from cardputer_mcp.flash_models import FlashDevice


async def test_operator_approval_then_mcp_backup_flash_restore_and_cancel(
    registry_service, tmp_path
):
    service = registry_service
    key = service.keys["second"]
    principal = key.metadata.principal_id
    fixture = manifest_tests.FlashManifestTests("test_verified_layout_is_inert_and_manifest_bound")
    fixture.setUp()
    elf = b"synthetic ELF contract"
    elf_hash = hashlib.sha256(elf).hexdigest()
    (fixture.root / "app.elf").write_bytes(elf)
    fixture.manifest["artifacts"].append(fixture.receipt("app.elf", elf))
    table_bytes = partition_table()
    (fixture.root / "partition_table.bin").write_bytes(table_bytes)
    fixture.manifest["artifacts"].append(fixture.receipt("partition_table.bin", table_bytes))
    fixture.layout["bootloader"] = {"offset": "0x0", "file": "boot.bin"}
    fixture.layout["partition_table"] = {"offset": "0x8000", "file": "partition_table.bin"}
    fixture.layout["flash_files"]["0x8000"] = "partition_table.bin"
    fixture.save_layout()

    registration = FlashDevice(
        selector={"vid": 1, "pid": 2, "serial_number": "synthetic"},
        flash_bytes=8388608,
        project_id="fixture",
        python_tool={
            "executable": tmp_path / "python.exe",
            "sha256": "a" * 64,
            "version": "Python 3.13.16",
        },
        esptool_directory=tmp_path / "tools",
        esptool_sha256="b" * 64,
        backup_directory=tmp_path / "backups",
        private_backup_acl_confirmed=True,
        original_elf_sha256=elf_hash,
        allow_backup=True,
        allow_flash=True,
        allow_restore=True,
    )
    service.resident.settings = service.resident.settings.model_copy(
        update={
            "flash_devices": {"fixture": registration},
            "build_projects": {
                "fixture": BuildProject(
                    project_dir=fixture.root,
                    trusted_profile=tmp_path / "profile.ps1",
                    build_root=tmp_path / "short",
                    build_min_free_bytes=0,
                )
            },
        }
    )
    service.resident.firmware_build.settings = service.resident.settings
    output = claim_build_directory(
        service.resident.settings.state_dir,
        tmp_path / "short",
        fixture.manifest["fingerprint"],
        fixture.manifest["environment_fingerprint"],
    )
    shutil.copytree(fixture.root, output, dirs_exist_ok=True)
    fixture.manifest["build_location"] = build_location(
        fixture.manifest["fingerprint"],
        fixture.manifest["environment_fingerprint"],
        tmp_path / "short",
    )
    (output / "broker-manifest.json").write_text(json.dumps(fixture.manifest))
    service.resident.firmware_flash.settings = service.resident.settings
    driver = FakeDriver()
    driver.data = bytearray(b"x" * registration.flash_bytes)
    driver.data[0x8000:0x8000 + len(table_bytes)] = table_bytes
    driver.security = replace(driver.security, flash_bytes=registration.flash_bytes)
    driver.summary["elf_sha256_prefix"] = elf_hash[:16]
    service.resident.firmware_flash.driver_factory = lambda *args: driver
    store = service.resident._store()
    build_plan = {"test": "firmware_build", "build_principal": principal, "project_id": "fixture"}
    digest = digest_plan(build_plan)
    store.put_plan(digest, build_plan)
    store.add_job("build-one", digest)
    store.set_job("build-one", "succeeded")
    service.resident.firmware_build.artifacts.publish(
        "build-one", principal, output, fixture.manifest
    )
    release = threading.Event()
    try:
        async with (
            httpx2.AsyncClient(
                base_url=service.url.removesuffix("/mcp"),
                trust_env=False,
                headers={"Authorization": "Bearer " + "a" * 64},
            ) as http,
            connect(service.url, key.token) as remote,
        ):
            advertised = await remote.list_tools()
            execute = next(tool for tool in advertised.tools if tool.name == "execute_test")
            assert execute.annotations.destructive_hint is True
            lease = await call(remote, "acquire_session", {"owner": "remote_mcp"})
            session = lease["session"]

            async def approve(job, action, **references):
                prepared = await call(
                    remote,
                    "prepare_test",
                    {
                        "session": session,
                        "job_id": job,
                        "test": action,
                        "device_id": "fixture",
                        **references,
                    },
                )
                assert prepared["ok"], prepared
                assert prepared["approval_required"]
                denied = await call(
                    remote, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
                )
                assert denied["error"] == "operator_approval_required"
                detail = (
                    await http.post("/operator/plan", json={"plan_hash": prepared["plan_hash"]})
                ).json()
                attestation = (
                    "REGISTERED DEVICE VERIFIED FOR THIS BACKUP READ"
                    if action == "firmware_backup"
                    else "DEVICE AND BACKUP VERIFIED FOR THIS EXACT FLASH PLAN"
                )
                response = await http.post(
                    "/operator/approve-plan",
                    json={
                        "plan_hash": prepared["plan_hash"],
                        "review_revision": detail["review_revision"],
                        "attestation": attestation,
                    },
                )
                assert response.status_code == 200 and response.json()["ok"], response.text
                return prepared

            backup = await approve("backup-one", "firmware_backup")
            executed = await call(
                remote, "execute_test", {"session": session, "plan_hash": backup["plan_hash"]}
            )
            assert executed["ok"], executed
            await service.resident.queue.join()
            assert service.resident.job("backup-one")["state"] == "succeeded"
            for job, action, references in (
                (
                    "flash-one",
                    "firmware_flash",
                    {"build_job_id": "build-one", "backup_job_id": "backup-one"},
                ),
                ("restore-one", "firmware_restore", {"backup_job_id": "backup-one"}),
            ):
                prepared = await approve(job, action, **references)
                result = await call(
                    remote, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
                )
                assert result["ok"], result
                await service.resident.queue.join()
                assert service.resident.job(job)["state"] == "succeeded"
                logs = await call(remote, "read_build_logs", {"job_id": job})
                assert logs["ok"] and logs["flash_summary"]["boot_observed"], logs
                replay = await call(
                    remote, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
                )
                assert replay["deduplicated"]
            current = store.firmware_current("fixture", principal)
            assert current["kind"] == "full_backup"
            # First full restore made its exact rescue target current. A new
            # successful flash replaces it with the immutable firmware bundle.
            prepared = await approve("flash-two", "firmware_flash", build_job_id="build-one")
            await call(remote, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]})
            await service.resident.queue.join()
            assert service.resident.job("flash-two")["state"] == "succeeded"
            current = store.firmware_current("fixture", principal)
            assert current["kind"] == "artifact_bundle"
            selected = current["receipt"]
            # Build retention cannot remove the private recovery bytes.
            service.resident.firmware_build.artifacts._records.clear()
            prepared = await approve("restore-bundle", "firmware_restore", lkg_receipt=selected)
            await call(remote, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]})
            await service.resident.queue.join()
            assert service.resident.job("restore-bundle")["state"] == "succeeded"
            assert store.firmware_current("fixture", principal)["receipt"] == selected
            prepared = await approve("cancel-one", "firmware_restore", lkg_receipt=selected)
            entered = threading.Event()

            def block_read(phase):
                if phase == "read":
                    entered.set()
                    assert release.wait(5)

            driver.callback = block_read
            await call(
                remote, "execute_test", {"session": session, "plan_hash": prepared["plan_hash"]}
            )
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), 4)
            cancelled = await call(
                remote, "cancel_job", {"session": session, "job_id": "cancel-one"}
            )
            assert cancelled["ok"], cancelled
            release.set()
            await service.resident.queue.join()
            assert service.resident.job("cancel-one")["state"] == "needs_review"
            assert driver.closed and not service.resident.firmware_flash.active
            assert store.firmware_current("fixture", principal)["receipt"] == selected
    finally:
        release.set()
        fixture.doCleanups()
