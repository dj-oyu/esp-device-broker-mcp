"""Emit public source-only integration style diagnostics for remote review."""

import json
import subprocess
import sys
from pathlib import Path


def main():
    paths = [
        Path("src/cardputer_mcp") / name
        for name in (
            "broker.py", "models.py", "config.py", "server.py", "plan_approval.py",
            "gui_approvals.py", "operator.py", "firmware_build.py",
        )
    ]
    saved = {p: p.read_text(encoding="utf-8") for p in paths}
    subprocess.run([sys.executable, "-m", "ruff", "format", *map(str, paths)], check=True)
    for path, before in saved.items():
        content = path.read_text(encoding="utf-8")
        if content != before:
            print("INTEGRATION_FORMATTED_FILE=" + json.dumps({"path": path.as_posix(), "content": content}))
    result = subprocess.run([sys.executable, "-m", "ruff", "check", "--output-format", "json", "src", "tests"], capture_output=True, text=True)
    print("INTEGRATION_STYLE_RESULTS=" + result.stdout)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
