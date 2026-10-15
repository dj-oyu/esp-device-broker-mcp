"""Enforce saved source formatting and complete source/test lint."""

import subprocess
import sys


def main():
    for command in (
        ["format", "--check", "--diff", "src", "tests"],
        ["check", "src", "tests"],
    ):
        subprocess.run([sys.executable, "-m", "ruff", *command], check=True)


if __name__ == "__main__":
    main()
