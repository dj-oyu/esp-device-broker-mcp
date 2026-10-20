"""Authenticated unattended source/build loop with synthetic transport."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from filelock import FileLock
from test_registry_http import registry_service as registry_service
from test_source_snapshot import COMMIT, FILES, REPOSITORY, archive

from cardputer_mcp import firmware_build as build_module
from cardputer_mcp.build_locations import slot_lock_path
from cardputer_mcp.client import call, connect
from cardputer_mcp.config import BuildProject, SerialSettings, SourceRepository
from cardputer_mcp.models import BuildLogRequest
from cardputer_mcp.ports import DeviceSelector
from cardputer_mcp.source_snapshot import SourceError, file_receipt, tree_hash


@pytest.mark.parametrize("backend_name", ["mock", "serial"])
@pytest.mark.parametrize("with_submodule", [False, True])
async def test_registered_source_then_owned_receipt_build_without_operator(
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
            b"url = https://github.com/lieff/minimp3.git\n"
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
            return json.dumps(
                {
                    "sha": tree,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "third_party/minimp3",
                            "sha": child_commit,
                            "mode": "160000",
                            "type": "commit",
                        }
                    ]
                    + [
                        {
                            "path": name,
                            "sha": file_receipt(data, "100644")["git_sha1"],
                            "mode": "100644",
                            "type": "blob",
                            "size": len(data),
                        }
                        for name, data in root_files.items()
                    ],
                }
            ).encode()
        child = "lieff/minimp3" in url
        if "/git/commits/" in url:
            return json.dumps(
                {
                    "sha": child_commit if child else COMMIT,
                    "tree": {"sha": child_tree if child else tree},
                }
            ).encode()
        return archive(
            child_files if child else root_files,
            repository="lieff/minimp3" if child else REPOSITORY,
            commit=child_commit if child else COMMIT,
        )

    service.resident.firmware_source.snapshots.transport = transport
    async with connect(service.url, key.token) as remote:
        catalog = await remote.list_tools()
        schema = next(tool.input_schema for tool in catalog.tools if tool.name == "prepare_test")
        assert "firmware_fetch_source" in json.dumps(schema)
        assert "source_commit" in json.dumps(schema) and "source_receipt" in json.dumps(schema)
        prepared = await call(
            remote,
            "prepare_test",
            {
                "job_id": "fetch-one",
                "test": "firmware_fetch_source",
                "project_id": "fixture",
                "source_commit": COMMIT,
            },
        )
        assert prepared["ok"] and not prepared["approval_required"], prepared
        assert prepared["storage"]["source_cache"]["snapshots"] == 0
        assert not calls
        execution = {"plan_hash": prepared["plan_hash"]}
        assert (await call(remote, "execute_test", execution))["ok"]
        await service.resident.profile_jobs.queue.join()
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
                "job_id": "build-one",
                "test": "firmware_build",
                "project_id": "fixture",
                "source_receipt": receipt,
            },
        )
        assert built["ok"] and not built["approval_required"], built
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

        fail_build = False

        class FakeIdf:
            active = uncertain = False
            stop_requested = threading.Event()

            def run(self, args, env, cwd, emit, timeout=1800, monitor=None):
                if monitor is not None:
                    monitor()
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
                    if fail_build:
                        emit("error: synthetic compilation failure", "stderr")
                        return 1
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
            executed = await call(
                remote,
                "execute_test",
                {
                    "plan_hash": built["plan_hash"],
                },
            )
            assert executed["ok"], executed
            await service.resident.profile_jobs.queue.join()
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

            # A normal compiler failure ends safely, then the same registered
            # profile accepts a corrected new job without review/restart.
            fail_build = True
            failed_plan = await call(
                remote,
                "prepare_test",
                {
                    "job_id": "build-failed",
                    "test": "firmware_build",
                    "project_id": "fixture",
                    "source_receipt": receipt,
                },
            )
            assert failed_plan["ok"] and not failed_plan["approval_required"]
            assert (
                await call(
                    remote,
                    "execute_test",
                    {
                        "plan_hash": failed_plan["plan_hash"],
                    },
                )
            )["ok"]
            await service.resident.profile_jobs.queue.join()
            assert service.resident.job("build-failed")["state"] == "failed"
            assert not service.resident._store().needs_review()
            assert "build-failed" not in service.resident.firmware_build.manifests
            assert service.resident.firmware_build._build_lock is None
            assert not service.resident.firmware_build.output_uncertain
            lock_path = slot_lock_path(
                settings.state_dir,
                project.build_root,
                report["manifest"]["fingerprint"],
                report["manifest"]["environment_fingerprint"],
            )

            def acquire_released_slot():
                # A fresh thread must acquire the actual OS lock after both
                # success publication and ordinary compile-failure cleanup.
                with FileLock(str(lock_path), timeout=0):
                    return True

            assert await asyncio.to_thread(acquire_released_slot)
            fail_build = False
            retry = await call(
                remote,
                "prepare_test",
                {
                    "job_id": "build-corrected",
                    "test": "firmware_build",
                    "project_id": "fixture",
                    "source_receipt": receipt,
                },
            )
            assert retry["ok"]
            request = {"plan_hash": retry["plan_hash"]}
            assert (await call(remote, "execute_test", request))["ok"]
            await service.resident.profile_jobs.queue.join()
            assert service.resident.job("build-corrected")["state"] == "succeeded"
            assert (await call(remote, "execute_test", request))["deduplicated"]
            assert service.backend.calls == 0
            assert service.resident.session is None
            assert not service.resident._store().db.execute("SELECT 1 FROM approvals").fetchone()
            async with connect(service.url, service.keys["first"].token) as other:
                denied = await call(other, "execute_test", request)
                assert not denied["ok"]
                denied = await call(other, "read_build_logs", {"job_id": "build-corrected"})
                assert not denied["ok"]
                denied = await call(other, "job_status", {"job_id": "build-corrected"})
                assert not denied["ok"]
        source, _ = service.resident.firmware_source.snapshots.resolve(
            "fixture", receipt, key.metadata.principal_id
        )
        (source / "CMakeLists.txt").write_bytes(b"modified after prepare")
        changed = await call(
            remote,
            "prepare_test",
            {
                "job_id": "changed-build",
                "test": "firmware_build",
                "project_id": "fixture",
                "source_receipt": receipt,
            },
        )
        assert not changed["ok"]


@pytest.mark.parametrize(
    "failure_kind",
    ["known", "unknown", "unsafe_source_error", "commit_known", "commit_unknown", "cache_protected"],
)
async def test_failed_source_retains_safe_diagnostic_through_broker(
    registry_service, tmp_path, monkeypatch, failure_kind
):
    service = registry_service
    key = service.keys["second"]
    profile = tmp_path / "profile.ps1"
    profile.write_text("synthetic profile")
    project = BuildProject(
        project_dir=tmp_path / "unused-worktree",
        trusted_profile=profile,
        build_root=tmp_path / "unused-build",
        source_repository=SourceRepository(
            github_repository=REPOSITORY,
            cache_root=tmp_path / "private-source-cache",
            private_acl_confirmed=True,
        ),
    )
    settings = service.resident.settings.model_copy(update={"build_projects": {"fixture": project}})
    source = service.resident.firmware_source
    original_fetch = source.snapshots.fetch
    original_usage = source.snapshots.usage
    service.resident.settings = settings
    source.settings = source.snapshots.settings = settings
    secret = "synthetic-private-token-123456"
    private_path = str(tmp_path / "synthetic-private-fixture")
    native_message = secret + " https://example.test/?token=" + key.token + " " + private_path
    expected = (
        "source_cache_protected"
        if failure_kind == "cache_protected"
        else "source_git_tree_mismatch"
        if failure_kind == "known"
        else "source_final_commit_not_authorized"
        if failure_kind == "commit_known"
        else "source_fetch_failed"
    )

    def failed_fetch(plan, live, stage):
        stage("source_validate")
        if failure_kind == "cache_protected":
            raise SourceError("source_cache_protected")
        if failure_kind == "known":
            raise SourceError("source_git_tree_mismatch")
        if failure_kind == "unsafe_source_error":
            raise SourceError(native_message)
        raise RuntimeError(native_message)

    if failure_kind.startswith("commit_"):
        tree = tree_hash({name: file_receipt(data, "100644") for name, data in FILES.items()})

        def transport(url, deadline, live, limit):
            return (
                json.dumps({"sha": COMMIT, "tree": {"sha": tree}}).encode()
                if url.startswith("https://api.github.com/")
                else archive()
            )

        source.snapshots.transport = transport

        def failed_commit(plan, result, live):
            if failure_kind == "commit_known":
                raise SourceError("source_final_commit_not_authorized")
            raise RuntimeError(native_message)

        monkeypatch.setattr(source, "commit_success", failed_commit)
    else:
        monkeypatch.setattr(source.snapshots, "fetch", failed_fetch)

    async with connect(service.url, key.token) as remote:
        prepared = await call(
            remote,
            "prepare_test",
            {
                "job_id": "source-failure",
                "test": "firmware_fetch_source",
                "project_id": "fixture",
                "source_commit": COMMIT,
            },
        )
        assert prepared["ok"], prepared
        execution = {"plan_hash": prepared["plan_hash"]}
        assert (await call(remote, "execute_test", execution))["ok"]
        await service.resident.profile_jobs.queue.join()
        job = service.resident.job("source-failure")
        assert job["state"] == ("needs_review" if failure_kind.startswith("commit_") else "failed")
        assert job["result"] == expected
        expected_diagnostic = {"code": expected}
        if failure_kind == "cache_protected":
            expected_diagnostic["explanation"] = SourceError(expected).public()["explanation"]
        assert job["diagnostic"] == expected_diagnostic
        assert service.resident._store().needs_review() == failure_kind.startswith("commit_")
        report = await call(
            remote, "read_build_logs", {"job_id": "source-failure", "levels": ["error"]}
        )
        assert report["diagnostic"] == expected_diagnostic
        assert any(expected in item["text"] for item in report["entries"])
        for value in (secret, key.token, private_path, "https://example.test/"):
            assert value not in json.dumps(job | {"logs": report})
        assert not source.active and not source.results
        assert service.backend.calls == 0 and not project.project_dir.exists()
        with pytest.raises(ValueError):
            source.read_logs(
                BuildLogRequest(job_id="source-failure"),
                service.keys["first"].metadata.principal_id,
            )
        source.logs.clear()
        recovered = source.read_logs(
            BuildLogRequest(job_id="source-failure"), key.metadata.principal_id
        )
        assert recovered["expired"] and recovered["diagnostic"] == expected_diagnostic

        def unavailable_cache(project_id):
            raise RuntimeError(native_message)

        monkeypatch.setattr(source.snapshots, "usage", unavailable_cache)
        recovered = source.read_logs(
            BuildLogRequest(job_id="source-failure"), key.metadata.principal_id
        )
        assert recovered["diagnostic"] == expected_diagnostic
        assert recovered["source_cache"] == {"available": False, "code": "source_fetch_failed"}
        assert secret not in json.dumps(recovered) and private_path not in json.dumps(recovered)

        if not failure_kind.startswith("commit_"):
            # A failed fetch grants no receipt, but does not prevent a corrected
            # fresh fetch under the same registration/principal.
            monkeypatch.setattr(source.snapshots, "fetch", original_fetch)
            monkeypatch.setattr(source.snapshots, "usage", original_usage)
            tree = tree_hash({name: file_receipt(data, "100644") for name, data in FILES.items()})

            def corrected_transport(url, deadline, live, limit):
                return (
                    json.dumps({"sha": COMMIT, "tree": {"sha": tree}}).encode()
                    if url.startswith("https://api.github.com/")
                    else archive()
                )

            source.snapshots.transport = corrected_transport
            corrected = await call(
                remote,
                "prepare_test",
                {
                    "job_id": "source-corrected",
                    "test": "firmware_fetch_source",
                    "project_id": "fixture",
                    "source_commit": COMMIT,
                },
            )
            assert corrected["ok"]
            assert (
                await call(
                    remote,
                    "execute_test",
                    {
                        "plan_hash": corrected["plan_hash"],
                    },
                )
            )["ok"]
            await service.resident.profile_jobs.queue.join()
            assert service.resident.job("source-corrected")["state"] == "succeeded"
            assert not service.resident._store().needs_review()
