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
    subprocess.run([sys.executable, "-m", "uv", "sync", "--frozen", "--python", str(python)],
                   cwd=root, check=True)
    paths = ["src/cardputer_mcp/firmware_build.py", "tests/test_firmware_build.py",
             "tests/test_build_flash_layout.py", "src/cardputer_mcp/build_security.py",
             "tests/test_build_security.py"]
    subprocess.run([str(python), "-m", "ruff", "format", "--check", *paths],
                   cwd=root, check=True)
    subprocess.run([str(python), "-m", "ruff", "check", *paths], cwd=root, check=True)
    subprocess.run([str(python), "-m", "mypy", "--platform", "linux", "src"], cwd=root, check=True)
    subprocess.run([str(python), "-m", "unittest", "discover", "-s", "tests", "-p", "*build*.py", "-v"],
                   cwd=root, check=True)
    print("PASS: receipt source format/lint/Linux type/build-unit checks", flush=True)


if __name__ == "__main__":
    check()
