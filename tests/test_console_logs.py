"""Console delivery is sanitized, bounded, lossy and independent of job success."""

import json
import subprocess
import sys
import threading
from types import SimpleNamespace

from cardputer_mcp import build_logs, store
from cardputer_mcp.build_logs import BuildLogs
from cardputer_mcp.console_logs import ConsoleOutput
from cardputer_mcp.store import Store


def test_console_uses_redacted_entries_and_throttles_progress_without_consuming_logs(monkeypatch):
    now = [100.0]
    emitted = []
    monkeypatch.setattr(build_logs, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(build_logs, "console_line", lambda job, text: emitted.append((job, text)))
    logs = BuildLogs("principal", job_id="compile-one")
    logs.redactions = ("synthetic-private-root", "ambient-sensitive-value")
    for number in range(100):
        logs.append(f"[{number}/100] building")
    logs.append("warning: token=synthetic synthetic-private-root ambient-sensitive-value")
    logs.append("error: Bearer synthetic-auth")
    assert len(emitted) == 3
    assert len(logs.entries) == 102
    assert all(job == "compile-one" for job, _ in emitted)
    assert "synthetic-private-root" not in str(emitted)
    assert "ambient-sensitive-value" not in str(emitted)
    assert "synthetic-auth" not in str(emitted)
    assert "token=synthetic" not in str(emitted)
    now[0] += 2
    logs.append("info: ROM read 128/256 bytes; elapsed 2 s")
    assert len(emitted) == 4 and logs.entries[-1]["progress"]
    assert len(logs.entries) == 103


def test_slow_console_never_blocks_producers_and_queue_is_bounded():
    entered, release, published = threading.Event(), threading.Event(), threading.Event()

    def blocked_writer(message):
        entered.set()
        release.wait(5)

    sink = ConsoleOutput(blocked_writer, capacity=2)
    try:
        sink.emit("first")
        assert entered.wait(2)

        def produce():
            for number in range(1000):
                sink.emit(f"progress {number}")
            published.set()

        producer = threading.Thread(target=produce, daemon=True)
        producer.start()
        assert published.wait(2)
        producer.join(2)
        assert not producer.is_alive()
        assert sink._queue.qsize() <= 2 and sink.dropped > 0
    finally:
        release.set()


def test_closed_console_is_silent_and_does_not_raise(capsys):
    def closed_writer(message):
        raise OSError("synthetic-private-console-error")

    sink = ConsoleOutput(closed_writer, capacity=2)
    sink.emit("one")
    sink._thread.join(2)
    assert sink._disabled
    sink.emit("two")
    assert "synthetic-private-console-error" not in capsys.readouterr().err


def test_console_escapes_terminal_controls_and_caps_messages():
    emitted, ready = [], threading.Event()

    def writer(message):
        emitted.append(message)
        ready.set()

    sink = ConsoleOutput(writer)
    sink.emit("warning: safe\x1b[2J\r\nnext" + "x" * 5000)
    assert ready.wait(2)
    assert "\x1b" not in emitted[0] and "\n" not in emitted[0] and "\r" not in emitted[0]
    assert "\\x1b" in emitted[0]
    assert len(emitted[0]) <= 2048


def test_job_lifecycle_is_after_commit_and_never_prints_raw_result(tmp_path, monkeypatch):
    now, emitted = [10.0], []
    history = Store(tmp_path / "history.sqlite3")

    def record(job, message):
        assert not history.db.in_transaction
        emitted.append((job, message))

    monkeypatch.setattr(store, "monotonic", lambda: now[0])
    monkeypatch.setattr(store, "console_line", record)
    try:
        history.put_plan("a" * 64, {"job_id": "build-one"})
        history.add_job("build-one", "a" * 64)
        history.set_job("build-one", "running")
        now[0] = 13.5
        history.set_job("build-one", "failed", "synthetic-private-native-error")
        assert emitted == [
            ("build-one", "started"), ("build-one", "failed; elapsed 3.5s")
        ]
        history.add_job("flash-one", "a" * 64)
        history.set_job("flash-one", "running")
        try:
            history.finish_firmware("flash-one", "sensitive-result", None, lambda: False)
        except ValueError:
            pass
        assert emitted[-1] == ("flash-one", "started")
        history.finish_firmware("flash-one", "sensitive-result", None, lambda: True)
        assert emitted[-1] == ("flash-one", "succeeded; elapsed 0.0s")
        assert "sensitive-result" not in str(emitted)
        assert history.job("flash-one")["state"] == "succeeded"
    finally:
        history.close()


def test_broken_diagnostic_callback_cannot_change_a_committed_job(tmp_path, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("synthetic console failure")

    monkeypatch.setattr(store, "console_line", unavailable)
    history = Store(tmp_path / "history.sqlite3")
    try:
        history.add_job("job-one", "a" * 64)
        history.set_job("job-one", "running")
        history.set_job("job-one", "succeeded", "done")
        assert history.job("job-one")["state"] == "succeeded"
    finally:
        history.close()


def test_diagnostics_use_stderr_and_stdout_remains_json():
    script = (
        "import json; from cardputer_mcp.console_logs import _CONSOLE, console_line; "
        "console_line('stdio-job', 'warning: sanitized diagnostic'); "
        "_CONSOLE._queue.join(); print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=10, check=True
    )
    assert json.loads(result.stdout) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert "[job stdio-job] warning: sanitized diagnostic" in result.stderr
    assert "[job" not in result.stdout


def test_full_unread_stderr_pipe_cannot_hold_process_shutdown():
    script = (
        "import json; from cardputer_mcp.console_logs import console_line; "
        "[console_line('pipe-job', 'warning: ' + 'x' * 2000) for _ in range(1000)]; "
        "print(json.dumps({'completed': True}))"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Deliberately do not drain stderr until AFTER the process has exited.
        child.wait(timeout=10)
        stdout, _ = child.communicate(timeout=2)
        assert child.returncode == 0 and json.loads(stdout) == {"completed": True}
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
