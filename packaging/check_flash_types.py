"""Compare native Windows diagnostics with the PR base under identical tools."""

import collections
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def diagnostics(root: Path):
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--no-incremental", "src"],
        cwd=root, capture_output=True, text=True,
    )
    print(result.stdout)
    errors = collections.Counter()
    for line in result.stdout.splitlines():
        match = re.match(r"(.+?):\d+: error: (.+)", line)
        if match:
            errors[(match[1].replace("\\", "/"), match[2])] += 1
    if result.returncode not in (0, 1) or (result.returncode == 1 and not errors):
        raise RuntimeError("type_check_could_not_complete")
    return errors


def main():
    root = Path.cwd()
    base = os.environ["FLASH_BASE_SHA"]
    if re.fullmatch(r"[a-f0-9]{40}", base) is None:
        raise ValueError("invalid_base_sha")
    with tempfile.TemporaryDirectory() as temp:
        checkout = Path(temp) / "base"
        subprocess.run(["git", "worktree", "add", "--detach", str(checkout), base], check=True)
        try:
            before = diagnostics(checkout)
            after = diagnostics(root)
            added = after - before
            print(f"NATIVE_TYPE_BASE={base} BASE_ERRORS={sum(before.values())} HEAD_ERRORS={sum(after.values())} NEW_ERRORS={sum(added.values())}")
            if added:
                raise RuntimeError("new_native_type_diagnostics")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(checkout)], check=True)
    sources = sorted(Path("src/cardputer_mcp").glob("flash*.py"))
    sources += [Path("src/cardputer_mcp/firmware_flash.py"), Path("src/cardputer_mcp/build_artifacts.py")]
    subprocess.run([sys.executable, "-m", "mypy", "--follow-imports=silent", *map(str, sources)], check=True)
    subprocess.run([sys.executable, "-m", "mypy", "--platform", "linux", "src"], check=True)


if __name__ == "__main__":
    main()
