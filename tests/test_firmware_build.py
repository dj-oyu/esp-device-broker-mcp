"""Dependency-free unit tests; also collected by pytest in Windows/CI."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.firmware_build import FirmwareBuild, parse_profile, source_fingerprint


def request(cursor=None, levels=("error", "warning"), context=2, limit=40):
    return SimpleNamespace(cursor=cursor, levels=levels, context=context, limit=limit)


class BuildTests(unittest.TestCase):
    def test_profile_strict_filter(self):
        env = parse_profile(
            "IDF_PATH=C:/idf\nIDF_PYTHON_ENV_PATH=C:/python\nPATH=C:/tools\nTOKEN=private\n"
        )
        self.assertNotIn("TOKEN", env)
        for value in ("noise", "IDF_PATH=x\nIDF_PATH=y", "KEY=bad\x00value", "PATH=x"):
            with self.assertRaises(ValueError):
                parse_profile(value)

    def test_version_registration_no_fallback(self):
        try:
            from cardputer_mcp.config import BuildProject
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        with self.assertRaises(ValueError):
            BuildProject(
                project_dir=Path("/project"), trusted_profile=Path("/profile"), expected_idf="6.1"
            )
        with self.assertRaises(ValueError):
            BuildProject(project_dir=Path("relative"), trusted_profile=Path("/profile"))
        with self.assertRaises(ValueError):
            BuildProject(
                project_dir=Path("/project"),
                trusted_profile=Path("/profile"),
                profile_execution_policy="Bypass",
            )

    def test_logs_multireader_filters_context_redaction(self):
        logs = BuildLogs("alice")
        for line in [
            "source.cpp",
            "warning: trouble token=private",
            "note: here",
            "unknown text",
            "[4/5] compiling",
        ]:
            logs.append(line, "stderr")
        first = logs.read("alice", request())
        self.assertEqual(first, logs.read("alice", request()))
        self.assertEqual(first["entries"][0]["level"], "unknown")
        self.assertEqual(first["entries"][1]["level"], "warning")
        self.assertNotIn("private", str(first))
        self.assertIn("note: here", str(first))
        with self.assertRaises(ValueError):
            logs.read("bob", request())
        cursor = SimpleNamespace(**first["next_cursor"])
        self.assertEqual([], logs.read("alice", request(cursor))["entries"])

    def test_large_output_gap_expiry_restart(self):
        logs = BuildLogs("alice", max_lines=8, max_bytes=1000)
        epoch = logs.epoch
        for _ in range(10000):
            logs.append("error: " + "x" * 500)
        self.assertLessEqual(logs.bytes, 1000)
        self.assertLessEqual(len(logs.entries), 8)
        self.assertTrue(
            logs.read("alice", request(SimpleNamespace(epoch=epoch, sequence=0)))["gap"]
        )
        self.assertTrue(
            logs.read("alice", request(SimpleNamespace(epoch="other", sequence=0)))["gap"]
        )
        logs.deadline = 0
        self.assertTrue(logs.read("alice", request())["expired"])

    def test_fingerprint_changes_and_build_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "CMakeLists.txt").write_text("one")
            before = source_fingerprint(root)
            (root / "build").mkdir()
            (root / "build" / "junk").write_text("ignored")
            self.assertEqual(before, source_fingerprint(root, excluded_roots=(root / "build",)))
            (root / "CMakeLists.txt").write_text("two")
            self.assertNotEqual(before, source_fingerprint(root))

    def test_registered_plan_no_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "CMakeLists.txt").write_text("source")
            profile = root / "profile.ps1"
            profile.write_text("return")
            settings = SimpleNamespace(
                build_projects={
                    "cardputer": SimpleNamespace(
                        project_dir=root,
                        trusted_profile=profile,
                        expected_idf="6.0.1",
                        target="esp32s3",
                    )
                }
            )
            plan = FirmwareBuild(settings).prepare("cardputer", "alice")
            self.assertNotIn(temp, str(plan))
            self.assertEqual("6.0.1", plan["expected_idf"])
            with self.assertRaises(ValueError):
                FirmwareBuild(settings).prepare("unknown", "alice")

    @unittest.skipIf(sys.platform == "win32", "POSIX runner; Windows uses owned job API")
    def test_process_bounded_streams_and_stderr_not_error(self):
        process = BuildProcess()
        lines = []
        script = 'import sys;print("x"*100000);print("note: text",file=sys.stderr)'
        code = process.run(
            [sys.executable, "-c", script],
            {},
            Path("/tmp"),
            lambda line, stream: lines.append((line, stream)),
            timeout=10,
        )
        self.assertEqual(code, 0)
        self.assertFalse(process.active)
        self.assertLessEqual(max((len(line) for line, stream in lines)), 4096)
        self.assertIn(("note: text", "stderr"), lines)

    @unittest.skipIf(sys.platform == "win32", "POSIX runner; Windows uses owned job API")
    def test_interruption_stops_owned_process(self):
        process = BuildProcess()
        failures = []

        def run():
            try:
                process.run(
                    [sys.executable, "-c", "import time;time.sleep(20)"],
                    {},
                    Path("/tmp"),
                    lambda *_: None,
                    timeout=30,
                )
            except ValueError as error:
                failures.append(str(error))

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.1)
        process.stop_requested.set()
        worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertFalse(process.active)
        self.assertEqual(["build_interrupted_needs_review"], failures)


class WindowsOwnedBuildTests(unittest.TestCase):
    def test_leader_exit_does_not_release_live_descendants(self):
        import os
        from unittest.mock import patch

        from cardputer_mcp import build_process as module

        class FakeAPI:
            last = None

            def __init__(self, out, err):
                self.out = out
                self.err = err
                self.children = 2
                self.configured = False
                self.terminated = False
                FakeAPI.last = self

            def create_job(self):
                return 101

            def configure_job(self, job):
                self.configured = True

            def create_suspended(self, job, args, env, cwd, handles):
                assert self.configured and job == 101
                assert env == {"PATH": "trusted"}
                handles.process = 202
                handles.thread = 303
                os.write(self.out, b"[1/1] build\n")
                os.write(self.err, b"note: stderr\n")

            def resume_thread(self, thread):
                assert thread == 303

            def close_handle(self, handle):
                pass

            def poll_process(self, process):
                return 0

            def active_count(self, job):
                return self.children

            def terminate_job(self, job):
                self.terminated = True
                self.children = 0

        runner = BuildProcess()
        lines = []
        cwd = Path("/tmp")
        with (
            patch.object(module.os, "name", "nt"),
            patch.object(module, "PipeWindowsAPI", FakeAPI),
            patch.dict(sys.modules, {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: fd)}),
            patch.object(module.os, "set_handle_inheritable", lambda *_: None, create=True),
        ):
            code = runner.run(
                ["trusted-python", "idf.py", "build"],
                {"PATH": "trusted"},
                cwd,
                lambda line, stream: lines.append((line, stream)),
            )
        self.assertEqual(code, 0)
        self.assertTrue(FakeAPI.last.terminated)
        self.assertFalse(runner.active)
        self.assertIsNone(runner.owner)
        self.assertIn(("note: stderr", "stderr"), lines)

    def test_shutdown_failure_latches_ownership(self):
        from unittest.mock import patch

        from cardputer_mcp import build_process as module

        class FakeAPI:
            def __init__(self, *_):
                pass

            def create_job(self):
                return 101

            def configure_job(self, job):
                raise RuntimeError("synthetic")

            def active_count(self, job):
                raise RuntimeError("synthetic")

        runner = BuildProcess()
        cwd = Path("/tmp")
        with (
            patch.object(module.os, "name", "nt"),
            patch.object(module, "PipeWindowsAPI", FakeAPI),
            patch.dict(sys.modules, {"msvcrt": SimpleNamespace(get_osfhandle=lambda fd: fd)}),
            patch.object(module.os, "set_handle_inheritable", lambda *_: None, create=True),
        ):
            with self.assertRaisesRegex(ValueError, "build_tree_stop_unconfirmed"):
                runner.run(["trusted"], {}, cwd, lambda *_: None)
        self.assertTrue(runner.active)
        self.assertTrue(runner.uncertain)
        self.assertIsNotNone(runner.owner)


class FirmwareVerticalTests(unittest.TestCase):
    def _exercise_build(
        self,
        cancel_at=None,
        policy="inherit",
        profile_denied=False,
        toolchain_failed=False,
        idf_failed=False,
        mutate_during_diagnostics=False,
        node_mode=None,
        source_mode=False,
        cache_mode=None,
    ):
        try:
            import filelock
        except ModuleNotFoundError as error:
            self.skipTest(str(error))
        del filelock
        from unittest.mock import patch

        from cardputer_mcp import firmware_build as module

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            (project / "CMakeLists.txt").write_text("source")
            profile = root / "profile.ps1"
            profile.write_text("return")
            system = root / "system"
            powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
            powershell.parent.mkdir(parents=True)
            powershell.write_bytes(b"ps")
            idf = root / "idf"
            idf_py = idf / "tools/idf.py"
            idf_py.parent.mkdir(parents=True)
            idf_py.write_bytes(b"idf")
            python_env = root / "python"
            python = python_env / "Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            compiler = root / "xtensa-esp32s3-elf-gcc.exe"
            compiler.write_bytes(b"gcc")
            cache = root / "ccache.exe"
            cache.write_bytes(b"cache")
            settings = SimpleNamespace(
                state_dir=root / "state",
                build_projects={
                    "cardputer": SimpleNamespace(
                        project_dir=project,
                        trusted_profile=profile,
                        expected_idf="6.0.1",
                        target="esp32s3",
                    )
                },
            )
            settings.build_projects["cardputer"].profile_execution_policy = policy
            settings.build_projects["cardputer"].build_root = root / "output"
            node = root / "additional-tools/node.exe"
            if node_mode:
                node.parent.mkdir()
                node.write_bytes(b"node")
                if node_mode != "unregistered":
                    settings.build_projects["cardputer"].node_tool = SimpleNamespace(
                        executable=node, sha256=module.hash_file(node), version="v22.0.0"
                    )
            builder = FirmwareBuild(settings)
            source_receipt = None
            if source_mode:
                import test_source_snapshot as fixture

                from cardputer_mcp.config import BuildProject, SourceRepository
                from cardputer_mcp.source_snapshot import SourceSnapshots, file_receipt, tree_hash

                registered = settings.build_projects["cardputer"]
                settings.build_projects["cardputer"] = BuildProject(
                    **vars(registered),
                    source_repository=SourceRepository(
                        github_repository=fixture.REPOSITORY,
                        cache_root=root / "source-cache",
                        private_acl_confirmed=True,
                    ),
                )
                tree = tree_hash(
                    {name: file_receipt(data, "100644") for name, data in fixture.FILES.items()}
                )

                def transport(url, *args):
                    if url.startswith("https://api.github.com/"):
                        return json.dumps({"sha": fixture.COMMIT, "tree": {"sha": tree}}).encode()
                    return fixture.archive()

                snapshots = SourceSnapshots(settings, transport)
                fetch_plan = snapshots.prepare("cardputer", fixture.COMMIT, "alice") | {
                    "job_id": "fetch"
                }
                source_receipt = snapshots.fetch(fetch_plan, lambda: True, lambda _: None)[
                    "source_receipt"
                ]
            plan = builder.prepare("cardputer", "alice", source_receipt) | {"job_id": "job"}
            calls = []
            environments = []
            statistics_reads = 0

            class FakeProcess:
                stop_requested = threading.Event()

                def run(self, args, env, cwd, emit, timeout=1800):
                    nonlocal statistics_reads
                    calls.append(args)
                    environments.append(dict(env))
                    self_test.assertEqual("AMD64", env["PROCESSOR_ARCHITECTURE"])
                    self_test.assertEqual("AMD64", env["PROCESSOR_ARCHITEW6432"])
                    self_test.assertNotIn("UNTRUSTED_TOKEN", env)
                    if "-e" in args:
                        if profile_denied:
                            emit("synthetic PSSecurityException private-path", "stderr")
                            return 1
                        for line in (
                            f"IDF_PATH={idf}",
                            f"IDF_PYTHON_ENV_PATH={python_env}",
                            f"PATH={root}",
                            "UNTRUSTED_TOKEN=secret",
                            *(("IDF_CCACHE_ENABLE=1",) if cache_mode else ()),
                        ):
                            emit(line, "stdout")
                    elif args[-1] == "--print-stats":
                        for name, value in (
                            ("direct_cache_hit", 2),
                            ("preprocessed_cache_hit", 1),
                            ("cache_miss", 3),
                        ):
                            emit(f"{name}\t{value if statistics_reads else 0}", "stdout")
                        statistics_reads += 1
                    elif args[-1] == "--version":
                        if args[0] == str(cache):
                            emit(
                                "ccache version " + ("4.0" if cache_mode == "old" else "4.12.1"),
                                "stdout",
                            )
                            return 1 if cache_mode == "broken" else 0
                        if idf_failed and args[1] == str(idf_py):
                            emit("error: Windows- unsupported token=hidden", "stderr")
                            emit("Bearer synthetic-secret-value", "stdout")
                            emit("x" * 10000, "stderr")
                            return 1
                        if mutate_during_diagnostics and args[0] == str(compiler):
                            (project / "CMakeLists.txt").write_text("unapproved changed source")
                        if toolchain_failed and args[0] == str(compiler):
                            emit("private native diagnostic", "stderr")
                            return 1
                        if args[0] == str(node):
                            emit("v21.0.0" if node_mode == "wrong_version" else "v22.0.0", "stdout")
                        else:
                            emit(
                                "ESP-IDF v6.0.1" if args[1] == str(idf_py) else "version", "stdout"
                            )
                    else:
                        if cache_mode == "compile_failed":
                            return 1
                        directory = Path(args[args.index("-B") + 1])
                        directory.mkdir(parents=True, exist_ok=True)
                        (directory / "project_description.json").write_text('{"target":"esp32s3"}')
                        (directory / "firmware.bin").write_bytes(b"firmware")
                        (directory / "config").mkdir(exist_ok=True)
                        (directory / "config/sdkconfig.json").write_text(
                            '{"IDF_TARGET":"esp32s3","SECURE_BOOT":false,'
                            '"SECURE_FLASH_ENC_ENABLED":false,"BOOTLOADER_APP_ANTI_ROLLBACK":false}'
                        )
                        (directory / "flasher_args.json").write_text(
                            '{"extra_esptool_args":{"chip":"esp32s3"},'
                            '"flash_files":{"0x10000":"firmware.bin"}}'
                        )
                        emit("warning: synthetic token=hidden", "stderr")
                    return 0

            self_test = self
            builder.process = FakeProcess()
            original_prepare = builder.prepare
            original_hash = module.hash_file
            prepare_calls = 0

            def prepare_hook(*args):
                nonlocal prepare_calls
                prepare_calls += 1
                value = original_prepare(*args)
                if cancel_at == "fingerprint" and prepare_calls == 3:
                    builder.request_stop("job")
                return value

            def hash_hook(path):
                value = original_hash(path)
                if cancel_at == "artifact" and path.name == "firmware.bin":
                    builder.request_stop("job")
                return value

            def which(name, path=None):
                if name == "xtensa-esp32s3-elf-gcc.exe":
                    return str(compiler)
                if name == "node.exe" and node_mode:
                    return str(compiler if node_mode == "ambiguous" else node)
                if name == "ccache.exe" and cache_mode != "missing":
                    return str(cache)
                return None

            with (
                patch.object(
                    module,
                    "os",
                    SimpleNamespace(
                        name="nt",
                        environ={
                            "SYSTEMROOT": str(system),
                            "UNTRUSTED_TOKEN": "secret",
                            "PROCESSOR_ARCHITECTURE": "AMD64",
                            "PROCESSOR_ARCHITEW6432": "AMD64",
                        },
                    ),
                ),
                patch.object(module.shutil, "which", side_effect=which),
                patch.object(builder, "prepare", side_effect=prepare_hook),
                patch.object(module, "hash_file", side_effect=hash_hook),
            ):
                if cache_mode in ("broken", "compile_failed"):
                    code = ("compiler_cache_diagnostic_failed" if cache_mode == "broken"
                            else "idf_build_failed_needs_review")
                    with self.assertRaisesRegex(ValueError, code):
                        builder._execute(plan, lambda _: None)
                    self.assertFalse(builder.manifests)
                    self.assertEqual(sum(args[-1] == "build" for args in calls),
                                     1 if cache_mode == "compile_failed" else 0)
                    return
                if node_mode in ("wrong_version", "ambiguous", "unregistered"):
                    code = {
                        "wrong_version": "registered_tool_version_mismatch",
                        "ambiguous": "registered_tool_resolution_ambiguous",
                        "unregistered": "node_requires_explicit_local_registration",
                    }[node_mode]
                    with self.assertRaisesRegex(ValueError, code):
                        builder._execute(plan, lambda _: None)
                    self.assertFalse(builder.manifests)
                    self.assertFalse(any(command[-1] == "build" for command in calls))
                    return
                if mutate_during_diagnostics:
                    with self.assertRaisesRegex(ValueError, "build_inputs_changed"):
                        builder._execute(plan, lambda _: None)
                    self.assertEqual(4, len(calls))
                    self.assertFalse(builder.manifests)
                    return
                if idf_failed:
                    from cardputer_mcp.firmware_build import BuildPreflightFailure

                    with self.assertRaises(BuildPreflightFailure) as failure:
                        builder._execute(plan, lambda _: None)
                    self.assertEqual(failure.exception.code, "idf_version_diagnostic_failed")
                    report = builder.read_logs(
                        SimpleNamespace(job_id="job", **vars(request())), "alice"
                    )
                    self.assertIn("Windows- unsupported", str(report))
                    self.assertNotIn("hidden", str(report))
                    self.assertNotIn("synthetic-secret-value", str(report))
                    self.assertTrue(all(len(item["text"]) <= 4096 for item in report["entries"]))
                    self.assertFalse(builder.manifests)
                    self.assertEqual(2, len(calls))
                    return
                if toolchain_failed:
                    with self.assertRaisesRegex(ValueError, "external_toolchain_diagnostic_failed"):
                        builder._execute(plan, lambda _: None)
                    self.assertEqual(4, len(calls))
                    self.assertFalse(builder.manifests)
                    return
                if profile_denied:
                    with self.assertRaisesRegex(ValueError, "profile_export_rejected_or_failed"):
                        builder._execute(plan, lambda _: None)
                    self.assertNotIn(
                        "private-path",
                        str(
                            builder.read_logs(
                                SimpleNamespace(job_id="job", **vars(request())), "alice"
                            )
                        ),
                    )
                    return
                if cancel_at:
                    with self.assertRaisesRegex(ValueError, "build_interrupted_needs_review"):
                        builder._execute(plan, lambda _: None)
                    self.assertNotIn("job", builder.manifests)
                    self.assertEqual([], list(root.rglob("broker-manifest.json")))
                    return
                result = builder._execute(plan, lambda _: None)
            self.assertEqual(result, "firmware_build_succeeded_no_flash")
            if policy == "inherit":
                self.assertEqual(calls[0][1:3], ["-NoProfile", "-File"])
                self.assertNotIn("-ExecutionPolicy", calls[0])
            else:
                self.assertEqual(
                    calls[0][1:5], ["-NoProfile", "-ExecutionPolicy", "RemoteSigned", "-File"]
                )
            build_call = next(args for args in calls if args[-1] == "build")
            build_env = next(env for args, env in zip(calls, environments, strict=True)
                             if args[-1] == "build")
            self.assertEqual(len(calls), (6 if node_mode else 5)
                             + (3 if cache_mode == "on" else 1 if cache_mode == "old" else 0))
            report = builder.read_logs(SimpleNamespace(job_id="job", **vars(request())), "alice")
            self.assertFalse(report["manifest"]["flash_authorized"])
            receipt = report["manifest"]["compiler_cache"]
            self.assertEqual(receipt["enabled"], cache_mode == "on")
            self.assertIn("--ccache" if receipt["enabled"] else "--no-ccache", build_call)
            if cache_mode == "on":
                self.assertEqual(
                    receipt["counter_delta"],
                    {"direct_cache_hit": 2, "preprocessed_cache_hit": 1, "cache_miss": 3},
                )
                self.assertTrue(Path(build_env["CCACHE_CONFIGPATH"]).is_file())
                self.assertIn("/broker-build", build_env["CFLAGS"])
            if source_mode:
                self.assertEqual(
                    report["manifest"]["source_provenance"]["source_receipt"], source_receipt
                )
                self.assertEqual(report["manifest"]["source_provenance"]["commit"], fixture.COMMIT)
                self.assertEqual(build_env["IDF_COMPONENT_MANAGER"], "0")
                config_arg = next(arg for arg in build_call if arg.startswith("SDKCONFIG="))
                self.assertEqual(Path(config_arg.split("=", 1)[1]).name, "sdkconfig")
                self.assertEqual((project / "CMakeLists.txt").read_text(), "source")
            self.assertEqual(report["manifest"]["idf_version"], "6.0.1")
            self.assertEqual(report["manifest"]["flash_layout"]["name"], "flasher_args.json")
            self.assertEqual(report["manifest"]["flash_security"]["name"], "config/sdkconfig.json")
            if node_mode:
                self.assertEqual(
                    report["manifest"]["registered_tool_versions"]["node"]["version"], "v22.0.0"
                )
                self.assertEqual(build_env["PATH"].split(";")[0], str(node.parent))
                self.assertNotIn("SYSTEM_PATH", environments[-1])
            self.assertNotIn("hidden", str(report))
            with self.assertRaises(ValueError):
                builder.read_logs(SimpleNamespace(job_id="job", **vars(request())), "bob")

    def test_compiler_cache_preserves_source_artifact_and_config_identity(self):
        self._exercise_build(cache_mode="on", source_mode=True)

    def test_missing_and_old_cache_use_explicit_uncached_build(self):
        for mode in ("missing", "old"):
            with self.subTest(mode=mode):
                self._exercise_build(cache_mode=mode)

    def test_cache_probe_or_compile_failure_never_retries_uncached(self):
        for mode in ("broken", "compile_failed"):
            with self.subTest(mode=mode):
                self._exercise_build(cache_mode=mode)

    def test_source_receipt_build_records_provenance_and_keeps_config_out_of_source(self):
        self._exercise_build(source_mode=True)

    def test_profile_diagnostics_build_manifest_and_reader_boundary(self):
        self._exercise_build()

    def test_cancel_during_post_build_fingerprint(self):
        self._exercise_build("fingerprint")

    def test_cancel_during_artifact_hash(self):
        self._exercise_build("artifact")

    def test_local_policy_opt_in_only(self):
        self._exercise_build(policy="RemoteSigned")

    def test_profile_policy_rejection_has_fixed_private_diagnostic(self):
        self._exercise_build(profile_denied=True)

    def test_idf_failure_retains_bounded_redacted_diagnostics(self):
        self._exercise_build(idf_failed=True)

    def test_toolchain_failure_prevents_build(self):
        self._exercise_build(toolchain_failed=True)

    def test_diagnostic_time_source_change_prevents_build(self):
        self._exercise_build(mutate_during_diagnostics=True)

    def test_registered_node_version_recorded(self):
        self._exercise_build(node_mode="registered")

    def test_wrong_registered_node_version_blocks_build(self):
        self._exercise_build(node_mode="wrong_version")

    def test_ambiguous_node_resolution_blocks_build(self):
        self._exercise_build(node_mode="ambiguous")

    def test_unregistered_node_on_profile_path_blocks_build(self):
        self._exercise_build(node_mode="unregistered")


class BuildLifecycleRegressionTests(unittest.TestCase):
    def test_expired_poll_preserves_live_redaction(self):
        builder = FirmwareBuild(SimpleNamespace())
        logs = BuildLogs("alice")
        logs.redactions = ("abcdefgh-secret",)
        logs.deadline = 0
        builder.logs["job"] = logs
        req = SimpleNamespace(job_id="job", **vars(request()))
        self.assertTrue(builder.read_logs(req, "alice")["expired"])
        logs.append("warning: abcdefgh-secret")
        self.assertNotIn("abcdefgh-secret", str(builder.read_logs(req, "alice")))

    def test_preexisting_stop_never_launches(self):
        from unittest.mock import patch

        from cardputer_mcp import build_process as module

        runner = BuildProcess()
        runner.stop_requested.set()
        with (
            patch.object(module.subprocess, "Popen") as launch,
            patch.object(module, "PipeWindowsAPI") as win_api,
        ):
            with self.assertRaisesRegex(ValueError, "build_interrupted_needs_review"):
                runner.run(["trusted"], {}, Path("/tmp"), lambda *_: None)
        launch.assert_not_called()
        win_api.assert_not_called()
        self.assertFalse(runner.active)

    def test_build_named_source_is_hashed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "CMakeLists.txt").write_text("include(build-helpers/config.cmake)")
            helper = root / "build-helpers/config.cmake"
            helper.parent.mkdir()
            helper.write_text("first code")
            before = source_fingerprint(root)
            helper.write_text("changed code")
            self.assertNotEqual(before, source_fingerprint(root))
            nested = root / "components/build-helper/CMakeLists.txt"
            nested.parent.mkdir(parents=True)
            nested.write_text("nested code")
            before = source_fingerprint(root)
            nested.write_text("changed nested code")
            self.assertNotEqual(before, source_fingerprint(root))

    def test_identical_checkouts_have_distinct_build_fingerprints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            profile = root / "profile.ps1"
            profile.write_text("return")
            registrations = {}
            for name in ("first", "second"):
                project = root / name
                project.mkdir()
                (project / "CMakeLists.txt").write_text("identical bytes")
                registrations[name] = SimpleNamespace(
                    project_dir=project,
                    trusted_profile=profile,
                    expected_idf="6.0.1",
                    target="esp32s3",
                )
            builder = FirmwareBuild(SimpleNamespace(build_projects=registrations))
            first = builder.prepare("first", "alice")["fingerprint"]
            second = builder.prepare("second", "alice")["fingerprint"]
            self.assertNotEqual(first, second)
            # Even the same registration ID in another broker config cannot alias roots.
            other = FirmwareBuild(
                SimpleNamespace(build_projects={"first": registrations["second"]})
            )
            self.assertNotEqual(first, other.prepare("first", "alice")["fingerprint"])


if __name__ == "__main__":
    unittest.main()
