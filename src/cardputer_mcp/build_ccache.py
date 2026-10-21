"""Optional IDF compiler cache; independent from immutable build/output slots."""

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .build_locations import _plain

CONTRACT = "idf-compiler-cache-v1"
MAX_SIZE = "2Gi"
NORMALIZATION = "build-debug-prefix-cmake-cache-v1"
CONFIG = (
    "compiler_check = content\n"
    "hash_dir = true\n"
    "hard_link = false\n"
    "inode_cache = false\n"
    "sloppiness =\n"
    "remote_storage =\n"
    "stats = true\n"
    f"max_size = {MAX_SIZE}\n"
)


def probe_ccache(env: dict[str, str], run: Any, cwd: Path) -> dict[str, Any]:
    """Use only the trusted profile PATH; no installation or extra registration."""
    setting = env.pop("IDF_CCACHE_ENABLE", "0")
    if setting not in {"0", "1"}:
        raise ValueError("compiler_cache_setting_invalid")
    result: dict[str, Any] = {"contract": CONTRACT, "enabled": False, "reason": "disabled"}
    env["IDF_CCACHE_ENABLE"] = "0"
    if setting == "0":
        return result
    executable = shutil.which("ccache.exe", path=env["PATH"])
    if executable is None:
        return result | {"reason": "tool_not_found"}
    tool = Path(executable)
    for component in (tool, *tool.parents):
        _plain(component)
    if not tool.is_file():
        raise ValueError("compiler_cache_diagnostic_failed")
    with tool.open("rb") as handle:
        before = hashlib.file_digest(handle, "sha256").hexdigest()
    output: list[str] = []

    def collect(line: str, stream: str) -> None:
        if len(output) >= 16 or len(line) > 1024:
            raise ValueError("compiler_cache_diagnostic_failed")
        output.append(line)

    if run([str(tool), "--version"], env, cwd, collect, timeout=30):
        raise ValueError("compiler_cache_diagnostic_failed")
    version = re.fullmatch(r"ccache version (\d+)\.(\d+)(?:\.(\d+))?", output[0] if output else "")
    with tool.open("rb") as handle:
        after = hashlib.file_digest(handle, "sha256").hexdigest()
    if not version or after != before:
        raise ValueError("compiler_cache_diagnostic_failed")
    # This contract uses the documented namespace/config/statistics interfaces
    # of the ccache 4.x generation shipped by ESP-IDF 6.0.1 on Windows.
    if int(version[1]) != 4 or int(version[2]) < 8:
        return result | {"reason": "unsupported_version"}
    env["IDF_CCACHE_ENABLE"] = "1"
    return result | {
        "enabled": True,
        "reason": "available",
        "version": version[0].removeprefix("ccache version "),
        "tool_sha256": before,
        "tool_path_sha256": hashlib.sha256(str(tool.resolve()).casefold().encode()).hexdigest(),
        "max_size": MAX_SIZE,
        "configuration_sha256": hashlib.sha256(CONFIG.encode()).hexdigest(),
        "path_normalization": NORMALIZATION,
    }


def configure_ccache(
    policy: dict[str, Any],
    env: dict[str, str],
    state_dir: Path,
    build_dir: Path,
    project_id: str,
    principal: str,
    environment_fingerprint: str,
    modes: dict[str, Any],
) -> dict[str, Any]:
    for key in tuple(env):
        if key.startswith("CCACHE_"):
            del env[key]
    if not policy["enabled"]:
        return dict(policy)
    owner = hashlib.sha256(json.dumps([project_id, principal]).encode()).hexdigest()
    root = state_dir.absolute() / "compiler-cache" / owner
    for component in (root, *root.parents):
        _plain(component)
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    namespace = hashlib.sha256(
        json.dumps(
            {
                "contract": CONTRACT,
                "owner": owner,
                "environment": environment_fingerprint,
                "modes": modes,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    # An explicit per-build config prevents ambient/system ccache options (e.g.
    # sloppiness or remote storage) from silently changing this cache contract.
    config = build_dir / "broker-ccache.conf"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=build_dir, delete=False
    ) as handle:
        pending = Path(handle.name)
        handle.write(CONFIG)
    try:
        pending.replace(config)
    finally:
        pending.unlink(missing_ok=True)
    env.update(
        CCACHE_CONFIGPATH=str(config),
        CCACHE_DIR=str(root),
        CCACHE_NAMESPACE=namespace,
        CCACHE_BASEDIR=str(build_dir.resolve()),
    )
    # Keep hash_dir enabled. Relocate only generated build-directory debug
    # paths, never suppress a key. Source paths remain significant; unchanged
    # IDF units can reuse generated config paths across otherwise isolated slots.
    # ccache compares native cwd strings; GCC may emit generic separator paths.
    # Map both spellings on Windows so hash_dir stays effective across slots.
    resolved = build_dir.resolve()
    spellings = dict.fromkeys((str(resolved), resolved.as_posix()))
    flag = subprocess.list2cmdline(
        ["-fdebug-prefix-map=" + path + "=/broker-build" for path in spellings]
    )
    for key in ("CFLAGS", "CXXFLAGS"):
        env[key] = " ".join(filter(None, (env.get(key), flag)))
    return policy | {"namespace": namespace, "path_normalization": NORMALIZATION}


def ccache_statistics(env: dict[str, str], run: Any, cwd: Path) -> dict[str, int] | None:
    """Best-effort counters only, not compilation or artifact acceptance."""
    executable = shutil.which("ccache.exe", path=env["PATH"])
    if executable is None:
        return None
    output: list[str] = []
    truncated = False

    def collect(line: str, stream: str) -> None:
        nonlocal truncated
        if len(output) >= 256 or len(line) > 1024:
            truncated = True
            return
        output.append(line)

    # Process cancellation/cleanup errors must propagate. Only malformed
    # statistics are optional; they never hide an owned-process failure.
    if run([executable, "--print-stats"], env, cwd, collect, timeout=30):
        return None
    if truncated:
        return None
    try:
        counters = {}
        for line in output:
            key, value = line.split()
            if key in {"direct_cache_hit", "preprocessed_cache_hit", "cache_miss"}:
                if not value.isdecimal():
                    return None
                counters[key] = int(value)
        return counters if len(counters) == 3 else None
    except (ValueError, OSError):
        return None
