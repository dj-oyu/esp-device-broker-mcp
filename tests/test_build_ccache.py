"""Compiler cache policy and real cross-slot GCC contracts; no SDK/device."""

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from cardputer_mcp.build_ccache import (
    CONFIG,
    CONTRACT,
    ccache_statistics,
    configure_ccache,
    probe_ccache,
)
from cardputer_mcp.firmware_build import parse_profile


class CompilerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = self.root / "ccache.exe"
        self.tool.write_bytes(b"synthetic cache executable")

    def probe(self, setting="1", version="4.12.1", code=0, mutate=False):
        env = {"PATH": str(self.root), "IDF_CCACHE_ENABLE": setting}

        def run(args, env, cwd, emit, **kwargs):
            self.assertEqual(args, [str(self.tool), "--version"])
            emit("ccache version " + version, "stdout")
            if mutate:
                self.tool.write_bytes(b"changed")
            return code

        with patch("cardputer_mcp.build_ccache.shutil.which", return_value=str(self.tool)):
            return probe_ccache(env, run, self.root), env

    def test_profile_only_forwards_existing_idf_switch(self):
        env = parse_profile(
            "IDF_PATH=C:/idf\nIDF_PYTHON_ENV_PATH=C:/python\nPATH=C:/tools\n"
            "IDF_CCACHE_ENABLE=1\nCCACHE_SLOPPINESS=time_macros\nCCACHE_PREFIX=arbitrary\n"
        )
        self.assertEqual(env["IDF_CCACHE_ENABLE"], "1")
        self.assertNotIn("CCACHE_SLOPPINESS", env)
        self.assertNotIn("CCACHE_PREFIX", env)

    def test_disabled_or_missing_cache_has_explicit_uncached_fallback(self):
        for value in ({"PATH": ""}, {"PATH": "", "IDF_CCACHE_ENABLE": "0"}):
            policy = probe_ccache(value, None, self.root)
            self.assertFalse(policy["enabled"])
            self.assertEqual(value["IDF_CCACHE_ENABLE"], "0")
        env = {"PATH": "", "IDF_CCACHE_ENABLE": "1"}
        with patch("cardputer_mcp.build_ccache.shutil.which", return_value=None):
            policy = probe_ccache(env, None, self.root)
        self.assertEqual(policy["reason"], "tool_not_found")
        self.assertEqual(env["IDF_CCACHE_ENABLE"], "0")

    def test_version_config_and_executable_bytes_are_identity_inputs(self):
        policy, env = self.probe()
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["version"], "4.12.1")
        self.assertEqual(
            policy["configuration_sha256"], hashlib.sha256(CONFIG.encode()).hexdigest()
        )
        self.assertEqual(policy["tool_sha256"], hashlib.sha256(self.tool.read_bytes()).hexdigest())
        self.assertEqual(env["IDF_CCACHE_ENABLE"], "1")
        for version in ("3.7.12", "4.0", "4.7.5", "5.0"):
            policy, env = self.probe(version=version)
            self.assertEqual(policy["reason"], "unsupported_version")
            self.assertFalse(policy["enabled"])
            self.assertEqual(env["IDF_CCACHE_ENABLE"], "0")

    def test_bad_setting_probe_or_changed_executable_is_not_silent_fallback(self):
        for arguments in (
            {"setting": "garbage"},
            {"code": 1},
            {"mutate": True},
            {"version": "bad"},
        ):
            with (
                self.subTest(arguments=arguments),
                self.assertRaisesRegex(ValueError, "compiler_cache_"),
            ):
                self.probe(**arguments)

    def test_same_environment_reuses_cache_without_reusing_build_slot(self):
        policy, _ = self.probe()
        receipts, envs = [], []
        for number in (1, 2):
            build = self.root / f"generation {number}"
            build.mkdir()
            env = {"PATH": str(self.root), "CCACHE_SLOPPINESS": "time_macros"}
            receipts.append(
                configure_ccache(
                    policy,
                    env,
                    self.root / "state",
                    build,
                    "project",
                    "owner",
                    "e" * 64,
                    {"detail": "ON"},
                )
            )
            envs.append(env)
            self.assertEqual(Path(env["CCACHE_CONFIGPATH"]).read_text(), CONFIG)
            self.assertNotIn("CCACHE_SLOPPINESS", env)
            self.assertNotIn("NOHASHDIR", str(env))
        self.assertEqual(envs[0]["CCACHE_DIR"], envs[1]["CCACHE_DIR"])
        self.assertEqual(receipts[0]["namespace"], receipts[1]["namespace"])
        self.assertNotEqual(envs[0]["CCACHE_CONFIGPATH"], envs[1]["CCACHE_CONFIGPATH"])
        self.assertNotEqual(envs[0]["CCACHE_BASEDIR"], envs[1]["CCACHE_BASEDIR"])
        self.assertIn("/broker-build", envs[0]["CFLAGS"])
        self.assertEqual(envs[0]["CFLAGS"], envs[0]["CXXFLAGS"])
        self.assertEqual(CONFIG.count("max_size = 2Gi"), 1)

    def test_windows_native_and_generic_debug_paths_are_both_normalized(self):
        policy, _ = self.probe()
        build = self.root / "build with spaces"
        build.mkdir()
        windows = PureWindowsPath("C:/build root/generation")
        env = {}
        with patch.object(Path, "resolve", return_value=windows):
            configure_ccache(
                policy, env, self.root / "state", build, "project", "owner", "e" * 64, {}
            )
        self.assertIn(str(windows), env["CFLAGS"])
        self.assertIn(windows.as_posix(), env["CFLAGS"])
        self.assertEqual(env["CFLAGS"].count("/broker-build"), 2)
        self.assertEqual((build / "broker-ccache.conf").read_bytes(), CONFIG.encode())

    def test_owner_environment_and_four_modes_are_separate_namespaces(self):
        policy, _ = self.probe()
        namespaces = set()
        build = self.root / "build"
        build.mkdir()
        for detail in ("ON", "OFF"):
            for dma in ("ON", "OFF"):
                receipt = configure_ccache(
                    policy,
                    {},
                    self.root / "state",
                    build,
                    "project",
                    "owner",
                    "e" * 64,
                    {"detail": detail, "dma": dma},
                )
                namespaces.add(receipt["namespace"])
        for owner, environment in (("other", "e" * 64), ("owner", "f" * 64)):
            receipt = configure_ccache(
                policy,
                {},
                self.root / "state",
                build,
                "project",
                owner,
                environment,
                {"detail": "ON", "dma": "ON"},
            )
            namespaces.add(receipt["namespace"])
        self.assertEqual(len(namespaces), 6)

    def test_statistics_are_optional_but_process_failure_propagates(self):
        def run(args, env, cwd, emit, **kwargs):
            for key, value in (
                ("direct_cache_hit", 2),
                ("preprocessed_cache_hit", 3),
                ("cache_miss", 4),
            ):
                emit(f"{key}\t{value}", "stdout")
            return 0

        with patch("cardputer_mcp.build_ccache.shutil.which", return_value=str(self.tool)):
            self.assertEqual(
                ccache_statistics({"PATH": ""}, run, self.root),
                {"direct_cache_hit": 2, "preprocessed_cache_hit": 3, "cache_miss": 4},
            )

            def malformed(args, env, cwd, emit, **kwargs):
                emit("x" * 1025, "stdout")
                return 0

            self.assertIsNone(ccache_statistics({"PATH": ""}, malformed, self.root))

            def process_failure(*args, **kwargs):
                raise ValueError("build_tree_stop_unconfirmed")

            with self.assertRaisesRegex(ValueError, "build_tree_stop_unconfirmed"):
                ccache_statistics({"PATH": ""}, process_failure, self.root)


@unittest.skipUnless(
    os.name == "posix" and shutil.which("ccache") and shutil.which("gcc"),
    "real Linux ccache/GCC contract fixture required",
)
class RealCompilerCacheTests(unittest.TestCase):
    def test_cross_slot_hit_header_invalidation_mode_isolation_and_cold_equivalence(self):
        cache, compiler = shutil.which("ccache"), shutil.which("gcc")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "idf" / "unchanged.c"
            source.parent.mkdir()
            source.write_text('#include "sdkconfig.h"\nint answer(void) { return VALUE; }\n')
            policy = {"contract": CONTRACT, "enabled": True, "reason": "available"}

            def run(args, env, cwd, emit, timeout=30):
                result = subprocess.run(
                    args, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout
                )
                for line in result.stdout.splitlines():
                    emit(line, "stdout")
                return result.returncode

            def compile_generation(number, value=42, dma="OFF"):
                build = root / f"generation-{number}"
                build.mkdir()
                (build / "config").mkdir()
                (build / "config" / "sdkconfig.h").write_text(f"#define VALUE {value}\n")
                env = dict(os.environ)
                receipt = configure_ccache(
                    policy,
                    env,
                    root / "state",
                    build,
                    "project",
                    "owner",
                    "e" * 64,
                    {"detail": "ON", "dma": dma},
                )
                args = [
                    cache,
                    compiler,
                    "-g",
                    "-O2",
                    *shlex.split(env["CFLAGS"]),
                    "-I",
                    str(build / "config"),
                    "-c",
                    str(source),
                    "-o",
                    str(build / "unit.o"),
                    "-MD",
                    "-MF",
                    str(build / "unit.d"),
                ]
                result = subprocess.run(args, env=env, cwd=build, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                with patch("cardputer_mcp.build_ccache.shutil.which", return_value=cache):
                    counters = ccache_statistics(env, run, build)
                self.assertIsNotNone(counters)
                return build, env, args, counters, receipt

            first = compile_generation(1)
            second = compile_generation(2)
            self.assertEqual(first[4]["namespace"], second[4]["namespace"])
            self.assertEqual(second[3]["direct_cache_hit"] + second[3]["preprocessed_cache_hit"], 1)
            self.assertEqual(first[3]["cache_miss"], second[3]["cache_miss"])
            warm = (second[0] / "unit.o").read_bytes()
            self.assertEqual((first[0] / "unit.o").read_bytes(), warm)
            self.assertNotIn(str(first[0]), (second[0] / "unit.d").read_text())
            cold_env = second[1] | {"CCACHE_RECACHE": "1"}
            result = subprocess.run(
                second[2], env=cold_env, cwd=second[0], capture_output=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((second[0] / "unit.o").read_bytes(), warm)
            changed = compile_generation(3, value=43)
            self.assertEqual(changed[3]["cache_miss"], 3)  # first, forced-cold, changed header
            self.assertNotEqual((changed[0] / "unit.o").read_bytes(), warm)
            other_mode = compile_generation(4, dma="ON")
            self.assertNotEqual(second[4]["namespace"], other_mode[4]["namespace"])
            self.assertEqual(other_mode[3]["cache_miss"], 4)
