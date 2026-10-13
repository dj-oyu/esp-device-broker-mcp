"""Authenticated MCP and local approval with synthetic source transport."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
import pytest
from test_registry_http import registry_service as registry_service
from test_source_snapshot import COMMIT, FILES, REPOSITORY, archive

from cardputer_mcp import firmware_build as build_module
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject, SerialSettings, SourceRepository
from cardputer_mcp.ports import DeviceSelector
from cardputer_mcp.source_snapshot import SourceError, file_receipt, tree_hash


@pytest.mark.parametrize("backend_name", ["mock", "serial"])
@pytest.mark.parametrize("with_submodule", [False, True])
async def test_explicit_source_approval_then_owned_receipt_build_plan(
    registry_service, tmp_path, monkeypatch, backend_name, with_submodule
):
    service = registry_service
    monkeypatch.setattr(service.backend, "name", backend_name)
    monkeypatch.setattr(
        service.backend,
        "device",
        "mock-cardputer" if backend_name == "mock" else "configured-cardputer",
    )
    key = service.keys["second"]
    profile = tmp_path / "profile.ps1"
    profile.write_text("synthetic")
    project = BuildProject(
        project_dir=tmp_path / "user-worktree",
        trusted_profile=profile,
        build_root=tmp_path / "builds",
        build_min_free_bytes=0,
        source_repository=SourceRepository(
            github_repository=REPOSITORY,
            cache_root=tmp_path / "source-cache",
            private_acl_confirmed=True,
            submodule_repositories=["lieff/minimp3"] if with_submodule else [],
        ),
    )
    serial = (
        SerialSettings(selector=DeviceSelector(vid=0xCAFE, pid=1, serial_number="SYNTHETIC"))
        if backend_name == "serial"
        else None
    )
    if serial is not None:
        monkeypatch.setattr(service.backend, "config", serial, raising=False)
    settings = service.resident.settings.model_copy(
        update={"backend": backend_name, "serial": serial, "build_projects": {"fixture": project}}
    )
    service.resident.settings = settings
    service.resident.firmware_build.settings = settings
    service.resident.firmware_source.settings = settings
    service.resident.firmware_source.snapshots.settings = settings
    calls = []
    root_files = dict(FILES)
    child_commit = "b" * 40
    child_files = {"minimp3.h": b"synthetic dependency"}
    if with_submodule:
        root_files[".gitmodules"] = (
            b'[submodule "minimp3"]\npath = third_party/minimp3\n'
            b'url = https://github.com/lieff/minimp3.git\n'
        )
    receipts = {name: file_receipt(data, "100644") for name, data in root_files.items()}
    if with_submodule:
        receipts["third_party/minimp3"] = {"mode": "160000", "git_sha1": child_commit}
    tree = tree_hash(receipts)
    child_tree = tree_hash(
        {name: file_receipt(data, "100644") for name, data in child_files.items()}
    )

    def transport(url, deadline, live, limit):
        calls.append(url)
        if not live():
            raise SourceError("source_fetch_interrupted")
        if "/git/trees/" in url:
            return json.dumps({
                "sha": tree, "truncated": False,
                "tree": [{"path": "third_party/minimp3", "sha": child_commit,
                          "mode": "160000", "type": "commit"}],
            }).encode()
        child = "lieff/minimp3" in url
        if "/git/commits/" in url:
            return json.dumps({
                "sha": child_commit if child else COMMIT,
                "tree": {"sha": child_tree if child else tree},
            }).encode()
        return archive(
            child_files if child else root_files,
            repository="lieff/minimp3" if child else REPOSITORY,
            commit=child_commit if child else COMMIT,
        )

    service.resident.firmware_source.snapshots.transport = transport
    async with (
        httpx2.AsyncClient(
            base_url=service.url.removesuffix("/mcp"),
            trust_env=False,
            headers={"Authorization": "Bearer " + "a" * 64},
        ) as http,
        connect(service.url, key.token) as remote,
    ):
        catalog = await remote.list_tools()
        schema = next(tool.input_schema for tool in catalog.tools if tool.name == "prepare_test")
        assert "firmware_fetch_source" in json.dumps(schema)
        assert "source_commit" in json.dumps(schema) and "source_receipt" in json.dumps(schema)
        lease = await call(remote, "acquire_session", {"owner": "remote_mcp", "lease_seconds": 300})
        prepared = await call(
            remote,
            "prepare_test",
            {
                "session": lease["session"],
                "job_id": "fetch-one",
                "test": "firmware_fetch_source",
                "project_id": "fixture",
                "source_commit": COMMIT,
            },
        )
        assert prepared["ok"] and prepared["approval_required"], prepared
        assert prepared["storage"]["source_cache"]["snapshots"] == 0
        assert not calls
        execution = {"session": lease["session"], "plan_hash": prepared["plan_hash"]}
        assert (await call(remote, "execute_test", execution))[
            "error"
        ] == "operator_approval_required"
        detail = (
            await http.post("/operator/plan", json={"plan_hash": prepared["plan_hash"]})
        ).json()
        assert detail.get("attestation") == "none", detail
        approved = await http.post(
            "/operator/approve-plan",
            json={
                "plan_hash": prepared["plan_hash"],
                "review_revision": detail["review_revision"],
                "attestation": "",
            },
        )
        assert approved.status_code == 200 and approved.json()["ok"], approved.text
        assert (await call(remote, "execute_test", execution))["ok"]
        await service.resident.queue.join()
        job = service.resident.job("fetch-one")
        assert job["state"] == "succeeded", json.dumps(job)
        logs = await call(remote, "read_build_logs", {"job_id": "fetch-one"})
        assert logs["source"]["commit"] == COMMIT
        assert logs["source_cache"]["snapshots"] == 1
        receipt = logs["source"]["source_receipt"]
        assert service.backend.calls == 0 and not service.resident.firmware_build.manifests
        assert not project.project_dir.exists()
        built = await call(
            remote,
            "prepare_test",
            {
                "session": lease["session"],
                "job_id": "build-one",
                "test": "firmware_build",
                "project_id": "fixture",
                "source_receipt": receipt,
            },
        )
        assert built["ok"] and built["approval_required"], built
        assert built["plan"]["source_provenance"]["commit"] == COMMIT
        sdk = tmp_path / "sdk"
        idf_py = sdk / "idf/tools/idf.py"
        python = sdk / "python/Scripts/python.exe"
        compiler = sdk / "bin/xtensa-esp32s3-elf-gcc.exe"
        system = sdk / "system"
        powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
        for tool in (idf_py, python, compiler, powershell):
            tool.parent.mkdir(parents=True, exist_ok=True)
            tool.write_bytes(b"synthetic executable; never invoked")
            tool.chmod(0o755)

        class FakeIdf:
            active = uncertain = False
            stop_requested = threading.Event()

            def run(self, args, env, cwd, emit, timeout=1800):
                if "-e" in args:
                    for line in (
                        "IDF_PATH=" + str(sdk / "idf"),
                        "IDF_PYTHON_ENV_PATH=" + str(sdk / "python"),
                        "PATH=" + str(sdk / "bin"),
                    ):
                        emit(line, "stdout")
                elif args[-1] == "--version":
                    emit("ESP-IDF v6.0.1" if str(idf_py) in args else "version", "stdout")
                elif args[-1] == "build":
                    directory = build_module.Path(args[args.index("-B") + 1])
                    assert env["IDF_COMPONENT_MANAGER"] == "0"
                    assert "SDKCONFIG=" + str(directory / "sdkconfig") in args
                    (directory / "project_description.json").write_text('{"target":"esp32s3"}')
                    (directory / "firmware.bin").write_bytes(b"synthetic firmware")
                    (directory / "flasher_args.json").write_text(
                        json.dumps(
                            {
                                "flash_files": {"0x10000": "firmware.bin"},
                                "extra_esptool_args": {"chip": "esp32s3"},
                            }
                        )
                    )
                    (directory / "config").mkdir(exist_ok=True)
                    (directory / "config/sdkconfig.json").write_text(
                        json.dumps(
                            {
                                "IDF_TARGET": "esp32s3",
                                "SECURE_BOOT": False,
                                "SECURE_FLASH_ENC_ENABLED": False,
                                "BOOTLOADER_APP_ROLLBACK_ENABLE": False,
                            }
                        )
                    )
                else:
                    raise AssertionError("unexpected SDK command")
                return 0

        service.resident.firmware_build.process = FakeIdf()
        native = SimpleNamespace(name="nt", environ={"SYSTEMROOT": str(system)})
        with patch.object(build_module, "os", native):
            detail = (
                await http.post("/operator/plan", json={"plan_hash": built["plan_hash"]})
            ).json()
            approved = await http.post(
                "/operator/approve-plan",
                json={
                    "plan_hash": built["plan_hash"],
                    "review_revision": detail["review_revision"],
                    "attestation": "",
                },
            )
            assert approved.json()["ok"], approved.text
            executed = await call(
                remote,
                "execute_test",
                {
                    "session": lease["session"],
                    "plan_hash": built["plan_hash"],
                },
            )
            assert executed["ok"], executed
            await service.resident.queue.join()
            job = service.resident.job("build-one")
            assert job["state"] == "succeeded", json.dumps(job)
            report = await call(remote, "read_build_logs", {"job_id": "build-one"})
            assert report["manifest"]["source_provenance"]["source_receipt"] == receipt
            assert report["manifest"]["source_provenance"]["commit"] == COMMIT
            assert (
                report["manifest"]["source_provenance"]["source_sha256"]
                == logs["source"]["source_sha256"]
            )
            if with_submodule:
                provenance = report["manifest"]["source_provenance"]
                assert provenance["submodules"] == logs["source"]["submodules"]
                assert provenance["submodules"][0]["commit"] == child_commit
                assert provenance["dependency_policy"] == "gitlinks_fixed_commits_no_fetch"
            assert report["manifest"]["flash_authorized"] is False
            assert service.backend.calls == 0 and not project.project_dir.exists()
        source, _ = service.resident.firmware_source.snapshots.resolve(
            "fixture", receipt, key.metadata.principal_id
        )
        (source / "CMakeLists.txt").write_bytes(b"modified after prepare")
        changed = await call(
            remote,
            "prepare_test",
            {
                "session": lease["session"],
                "job_id": "changed-build",
                "test": "firmware_build",
                "project_id": "fixture",
                "source_receipt": receipt,
            },
        )
        assert not changed["ok"]
