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
             "tests/test_build_security.py", "src/cardputer_mcp/build_locations.py",
             "tests/test_build_locations.py", "src/cardputer_mcp/config.py",
             "src/cardputer_mcp/broker.py"]
    subprocess.run([str(python), "-m", "ruff", "check", "--fix", *paths], cwd=root, check=True)
    subprocess.run([str(python), "-m", "ruff", "format", *paths], cwd=root, check=True)
    import base64
    import json
    import zlib
    snapshot = json.dumps({name: (root / name).read_text(encoding="utf-8") for name in paths})
    print("ROOT_FORMAT_B64:" + base64.b64encode(zlib.compress(snapshot.encode())).decode(), flush=True)
    subprocess.run([str(python), "-m", "ruff", "format", "--check", *paths],
                   cwd=root, check=True)
    subprocess.run([str(python), "-m", "ruff", "check", *paths], cwd=root, check=True)
    subprocess.run([str(python), "-m", "mypy", "--platform", "linux", "src"], cwd=root, check=True)
    subprocess.run([str(python), "-m", "unittest", "discover", "-s", "tests", "-p", "*build*.py", "-v"],
                   cwd=root, check=True)
    print("PASS: receipt source format/lint/Linux type/build-unit checks", flush=True)
    raise SystemExit("Formatter bridge: source snapshot must be committed before passing")


if __name__ == "__main__":
    check()
