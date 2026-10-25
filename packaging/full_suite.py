"""Keep synthetic-suite progress and timeout evidence outside pytest's lifetime."""

from __future__ import annotations

import argparse
import faulthandler
import json
import math
import os
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

from ci_process_tree import ProcessTree

TIMEOUT = 124
RUNNER_ERROR = 125
CONSOLE_LINE_BYTES = 2048
TESTS = Path(__file__).resolve().parents[1] / "tests"


class ConsoleProgress:
    """Console backpressure must never stop deadlines, cleanup, or disk evidence."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._forward, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=0.5)

    @staticmethod
    def _write(data: bytes) -> None:
        # Avoid TextIO locks: a blocked daemon writer must not hold a lock that
        # interpreter shutdown later tries to acquire. This thread is best effort.
        while data:
            count = os.write(sys.stdout.fileno(), data)
            if count <= 0:
                raise OSError
            data = data[count:]

    def _forward(self) -> None:
        line = bytearray()
        truncated = False

        def finish_line() -> None:
            suffix = b" [console line truncated; full text in artifact]" if truncated else b""
            # A byte budget may end inside a UTF-8 character. Keep only the
            # complete prefix; the artifact retains the original bytes.
            prefix = bytes(line).decode("utf-8", errors="ignore").encode("utf-8")
            self._write(prefix + suffix + b"\n")

        try:
            with (self.folder / "pytest.log").open("rb") as progress:
                while True:
                    content = progress.read(65536)
                    if not content:
                        if self.stop.is_set():
                            break
                        self.stop.wait(0.1)
                        continue
                    pieces = content.split(b"\n")
                    for index, piece in enumerate(pieces):
                        remaining = CONSOLE_LINE_BYTES - len(line)
                        line.extend(piece[:remaining])
                        truncated = truncated or len(piece) > remaining
                        if index < len(pieces) - 1:
                            finish_line()
                            line.clear()
                            truncated = False
                if line or truncated:
                    finish_line()
            stacks = self.folder / "stacks.log"
            if stacks.exists():
                with stacks.open("rb") as stream:
                    data = stream.read(16384).decode("utf-8", errors="ignore").encode("utf-8")
                if data:
                    self._write(b"\n[full-suite] lifecycle timeout stacks:\n" + data)
            with (self.folder / "result.json").open("rb") as stream:
                self._write(b"\n[full-suite] " + stream.read(4096) + b"\n")
        except (OSError, ValueError):
            # Console availability is independent from the authoritative files.
            return


def positive_seconds(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("a positive finite duration is required")
    return value


def shard_files(tests: Path, shard: int, shards: int) -> list[Path]:
    """Deterministically partition test files, balanced by size, into shards.

    Every ``test_*.py`` file directly under ``tests`` lands in exactly one
    shard, so new files are included without editing a list. File size is a
    rough static weight; largest files are placed first onto the lightest
    shard (ties broken by name, then shard index).
    """
    files = sorted(tests.glob("test_*.py"), key=lambda path: path.name)
    loads = [0] * shards
    chosen: list[Path] = []
    for path in sorted(files, key=lambda item: (-item.stat().st_size, item.name)):
        target = min(range(shards), key=lambda index: (loads[index], index))
        loads[target] += path.stat().st_size
        if target == shard - 1:
            chosen.append(path)
    return sorted(chosen, key=lambda path: path.name)


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
    console = None
    started = time.monotonic()
    try:
        tree = ProcessTree()
        with log_path.open("wb", buffering=0) as output:
            console = ConsoleProgress(args.output)
            console.start()
            process = tree.start(
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
                stdout=output,
            )
            assert process.stdin is not None
            process.stdin.write(b"1")
            process.stdin.close()
            while True:
                code = process.poll()
                if code is not None:
                    result.update(exit_code=code, reason="pytest_exit")
                    break
                if time.monotonic() - started >= args.deadline:
                    result.update(exit_code=TIMEOUT, reason="outer_deadline")
                    break
                time.sleep(0.1)
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        try:
            if tree is not None:
                leaked = tree.close(process)
                result["cleanup"] = "confirmed"
                if leaked and result["exit_code"] == 0:
                    result.update(exit_code=RUNNER_ERROR, reason="descendants_remaining")
                    if sys.platform == "win32":
                        # Bounded numeric metadata from this owned job only.
                        # Retain no process names, paths, arguments or output.
                        result["ci_cleanup_probe"] = tree._cleanup_probe
        except Exception as error:
            result.update(exit_code=RUNNER_ERROR, reason="cleanup_failed", cleanup="unconfirmed")
            result["cleanup_error_type"] = type(error).__name__
        if (args.output / "timeout.json").exists():
            result.update(exit_code=TIMEOUT, reason="phase_timeout")
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (args.output / "result.json").write_text(json.dumps(result), encoding="utf-8")
        if console is not None:
            console.close()
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
    parser.add_argument("--shard", type=int, default=1, help="1-based shard index")
    parser.add_argument("--shards", type=int, default=1, help="total shard count")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.output = args.output.resolve()
    if args.pytest_args[:1] == ["--"]:
        args.pytest_args.pop(0)
    if not 1 <= args.shard <= args.shards:
        parser.error("--shard must be between 1 and --shards")
    if not args.child and args.shards > 1:
        selected = shard_files(TESTS, args.shard, args.shards)
        if not selected:
            # An empty selection would make pytest fall back to every testpath.
            parser.error("the selected shard contains no test files")
        args.pytest_args = [*map(str, selected), *args.pytest_args]
    return run_pytest(args) if args.child else supervise(args)


if __name__ == "__main__":
    raise SystemExit(main())
