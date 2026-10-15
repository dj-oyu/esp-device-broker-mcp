"""CI-only source checks for the receipt producer; no hardware or user config."""

import os
import subprocess
import sys
from pathlib import Path


def check() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv/Scripts/python.exe"
    subprocess.run(
        [sys.executable, "-m", "uv", "sync", "--frozen", "--python", str(python)],
        cwd=root,
        check=True,
    )
    paths = [
        "src/cardputer_mcp/firmware_build.py",
        "tests/test_firmware_build.py",
        "tests/test_build_flash_layout.py",
        "src/cardputer_mcp/build_security.py",
        "tests/test_build_security.py",
        "src/cardputer_mcp/build_locations.py",
        "tests/test_build_locations.py",
        "src/cardputer_mcp/config.py",
        "src/cardputer_mcp/broker.py",
        "tests/test_build_approval_integration.py",
        "src/cardputer_mcp/profile_jobs.py",
        "src/cardputer_mcp/firmware_source.py",
        "src/cardputer_mcp/source_snapshot.py",
        "src/cardputer_mcp/build_artifacts.py",
        "src/cardputer_mcp/models.py",
        "src/cardputer_mcp/server.py",
        "src/cardputer_mcp/store.py",
        "src/cardputer_mcp/operator_timeline.py",
        "src/cardputer_mcp/gui_timeline.py",
        "tests/test_source_http.py",
        "tests/test_profile_jobs.py",
    ]
    diagnostics = [
        [str(python), "-m", "ruff", "format", "--check", *paths],
        [str(python), "-m", "ruff", "check", *paths],
        [str(python), "-m", "mypy", "--platform", "linux", "src"],
    ]
    results = [subprocess.run(command, cwd=root, check=False) for command in diagnostics]
    if any(result.returncode for result in results):
        raise RuntimeError("receipt_source_diagnostics_failed")
    subprocess.run(
        [str(python), "-m", "unittest", "discover", "-s", "tests", "-p", "*build*.py", "-v"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        [str(python), "-m", "pytest", "-q", "tests/test_source_http.py", "tests/test_profile_jobs.py"],
        cwd=root,
        check=True,
    )
    print("PASS: receipt source format/lint/Linux type/build/profile-loop checks", flush=True)


if __name__ == "__main__":
    check()
