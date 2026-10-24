"""Compiler cache policy and real cross-slot GCC contracts; no SDK/device."""

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cardputer_mcp.build_ccache import (
    CONFIG,
    CONTRACT,
    ccache_statistics,
    configure_ccache,
    probe_ccache,
)


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
        for arguments in ({"setting": "garbage"}, {"code": 1}, {"mutate": True}):
            with self.assertRaisesRegex(ValueError, "compiler_cache_"):
                self.probe(**arguments)


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
            with patch("cardputer_mcp.build_ccache.shutil.which", return_value=cache):
                cold_counters = ccache_statistics(second[1], run, second[0])
            self.assertIsNotNone(cold_counters)
            # Forced recache has its own counter; do not assume it is a normal miss.
            self.assertEqual(
                cold_counters["direct_cache_hit"] + cold_counters["preprocessed_cache_hit"], 1
            )
            changed = compile_generation(3, value=43)
            self.assertEqual(changed[3]["cache_miss"], cold_counters["cache_miss"] + 1)
            self.assertNotEqual((changed[0] / "unit.o").read_bytes(), warm)
            other_mode = compile_generation(4, dma="ON")
            self.assertNotEqual(second[4]["namespace"], other_mode[4]["namespace"])
            self.assertEqual(other_mode[3]["cache_miss"], changed[3]["cache_miss"] + 1)
