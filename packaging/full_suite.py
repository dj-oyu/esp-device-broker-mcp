"""Keep synthetic-suite progress and timeout evidence outside pytest's lifetime."""

from __future__ import annotations

import argparse
import faulthandler
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

from ci_process_tree import ProcessTree

TIMEOUT = 124
RUNNER_ERROR = 125


def positive_seconds(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("a positive finite duration is required")
    return value


class PhaseWatchdog:
    """A separate timer survives pytest cancelling its own per-test fault timer."""

    def __init__(self, folder: Path, seconds: float) -> None:
        self.folder, self.seconds = folder, seconds
        self.stacks = (folder / "stacks.log").open("wb", buffering=0)
        self.lock = threading.Lock()
        self.generation = 0
        self.expired = False
        self.timer: threading.Timer | None = None

    def arm(self, phase: str) -> None:
        with self.lock:
            if self.expired:
                return
            self.generation += 1
            if self.timer is not None:
                self.timer.cancel()
            (self.folder / "phase.json").write_text(json.dumps({"phase": phase}), encoding="utf-8")
            self.timer = threading.Timer(self.seconds, self._expire, (self.generation, phase))
            self.timer.daemon = True
            self.timer.start()

    def cancel_for_tests(self) -> None:
        with self.lock:
            self.generation += 1
            if self.timer is not None:
                self.timer.cancel()
            (self.folder / "phase.json").write_text(
                json.dumps({"phase": "tests"}), encoding="utf-8"
            )

    def _expire(self, generation: int, phase: str) -> None:
        with self.lock:
            if generation != self.generation or self.expired:
                return
            self.expired = True
            try:
                (self.folder / "timeout.json").write_text(
                    json.dumps({"phase": phase, "timeout": True}), encoding="utf-8"
                )
            except OSError:
                pass
        try:
            faulthandler.dump_traceback(file=self.stacks, all_threads=True)
        finally:
            os._exit(TIMEOUT)


def run_pytest(args: argparse.Namespace) -> int:
    # The parent attaches the Windows job before opening this gate. Importing
    # pytest or test modules before it could create unowned descendants.
    if sys.stdin.buffer.read(1) != b"1":
        return RUNNER_ERROR
    watchdog = PhaseWatchdog(args.output, args.phase_timeout)
    watchdog.arm("collection")
    import pytest

    class Lifecycle:
        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_collection_finish(self) -> Generator[None, object, object]:
            result = yield
            watchdog.cancel_for_tests()
            return result

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_sessionfinish(self) -> Generator[None, object, object]:
            watchdog.arm("shutdown")
            return (yield)

    code = int(
        pytest.main(
            [
                "-vv",
                "--tb=short",
                "-o",
                f"faulthandler_timeout={args.test_timeout}",
                "-o",
                "faulthandler_exit_on_timeout=true",
                f"--junitxml={args.output / 'junit.xml'}",
                *args.pytest_args,
            ],
            plugins=[Lifecycle()],
        )
    )
    # Keep the watchdog and stack descriptor alive through unconfigure and
    # interpreter shutdown, including non-daemon thread joins and atexit hooks.
    with watchdog.lock:
        return TIMEOUT if watchdog.expired else code


def supervise(args: argparse.Namespace) -> int:
    args.output.mkdir(parents=True, exist_ok=False)
    log_path = args.output / "pytest.log"
    result = {"exit_code": RUNNER_ERROR, "reason": "runner_error", "cleanup": "not_started"}
    process = None
    tree = None
    offset = 0
    started = time.monotonic()
    try:
        tree = ProcessTree()
        with log_path.open("wb", buffering=0) as output, log_path.open("rb") as progress:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(Path(__file__).resolve()),
                    "--child",
                    "--output",
                    str(args.output),
                    "--phase-timeout",
                    str(args.phase_timeout),
                    "--test-timeout",
                    str(args.test_timeout),
                    "--",
                    *args.pytest_args,
                ],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
            )
            tree.attach(process)
            assert process.stdin is not None
            process.stdin.write(b"1")
            process.stdin.close()
            while True:
                content = progress.read(65536)
                if content:
                    sys.stdout.write(content.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                offset = progress.tell()
                code = process.poll()
                if code is not None:
                    result.update(exit_code=code, reason="pytest_exit")
                    break
                if time.monotonic() - started >= args.deadline:
                    result.update(exit_code=TIMEOUT, reason="outer_deadline")
                    break
                time.sleep(0.1)
    except Exception as error:
        print(f"[full-suite] supervisor error: {type(error).__name__}", flush=True)
    finally:
        try:
            if tree is not None:
                leaked = tree.close(process)
                result["cleanup"] = "confirmed"
                if leaked and result["exit_code"] == 0:
                    result.update(exit_code=RUNNER_ERROR, reason="descendants_remaining")
        except Exception as error:
            result.update(exit_code=RUNNER_ERROR, reason="cleanup_failed", cleanup="unconfirmed")
            print(f"[full-suite] cleanup error: {type(error).__name__}", flush=True)
        if (args.output / "timeout.json").exists():
            result.update(exit_code=TIMEOUT, reason="phase_timeout")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (args.output / "result.json").write_text(json.dumps(result), encoding="utf-8")
        if log_path.exists():
            with log_path.open("rb") as progress:
                progress.seek(offset)
                sys.stdout.write(progress.read().decode("utf-8", errors="replace"))
        stacks = args.output / "stacks.log"
        if stacks.exists() and stacks.stat().st_size:
            print("\n[full-suite] lifecycle timeout stacks:", flush=True)
            sys.stdout.write(stacks.read_text(encoding="utf-8", errors="replace"))
        print(f"\n[full-suite] {json.dumps(result)}", flush=True)
    # Preserve the native status in JSON and normalize shell failure. Large
    # Windows crash statuses or negative POSIX statuses must not truncate to 0.
    return 0 if result["exit_code"] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("test-diagnostics"))
    parser.add_argument("--deadline", type=positive_seconds, default=720.0)
    parser.add_argument("--phase-timeout", type=positive_seconds, default=120.0)
    parser.add_argument("--test-timeout", type=positive_seconds, default=90.0)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.pytest_args[:1] == ["--"]:
        args.pytest_args.pop(0)
    return run_pytest(args) if args.child else supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
