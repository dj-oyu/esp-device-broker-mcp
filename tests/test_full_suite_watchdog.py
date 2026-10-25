"""Real disposable subprocesses prove collection/test/shutdown evidence survives."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging/full_suite.py"


def run_suite(tmp_path, source, *, phase=5, test=2, deadline=12, conftest=""):
    case = tmp_path / "test_case.py"
    case.write_text(source, encoding="utf-8")
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(conftest, encoding="utf-8")
    output = tmp_path / "diagnostics"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--phase-timeout",
            str(phase),
            "--test-timeout",
            str(test),
            "--deadline",
            str(deadline),
            "--",
            "-c",
            str(config),
            str(case),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONUTF8": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=25,
    )
    result = json.loads((output / "result.json").read_text(encoding="utf-8"))
    log = (output / "pytest.log").read_text(encoding="utf-8", errors="replace")
    return completed, result, log, output


def test_success_retains_verbose_progress_and_junit(tmp_path):
    completed, result, log, output = run_suite(tmp_path, "def test_pass(): assert True\n")
    assert completed.returncode == result["exit_code"] == 0
    assert result["cleanup"] == "confirmed"
    assert "test_pass PASSED" in log and "1 passed" in completed.stdout
    assert (output / "junit.xml").is_file()
    assert not (output / "timeout.json").exists()


def test_assertion_failure_cannot_be_replaced_by_junit_success(tmp_path):
    completed, result, log, output = run_suite(tmp_path, "def test_fail(): assert False\n")
    assert completed.returncode != 0 and result["exit_code"] != 0
    assert result["cleanup"] == "confirmed" and result["reason"] == "pytest_exit"
    assert "test_fail FAILED" in log and "1 failed" in completed.stdout
    assert (output / "junit.xml").is_file()


def test_collection_timeout_records_stack_before_parent_returns(tmp_path):
    completed, result, _, output = run_suite(
        tmp_path, "import time\ntime.sleep(30)\ndef test_never_collected(): pass\n", phase=2
    )
    assert completed.returncode != 0 and result["reason"] == "phase_timeout"
    assert json.loads((output / "timeout.json").read_text())["phase"] == "collection"
    assert "test_case.py" in (output / "stacks.log").read_text()
    assert "lifecycle timeout stacks" in completed.stdout


def test_per_test_timeout_exits_and_retains_exact_node_and_stack(tmp_path):
    completed, result, log, _ = run_suite(
        tmp_path, "import time\ndef test_stuck(): time.sleep(30)\n", test=1
    )
    assert completed.returncode != 0 and result["exit_code"] != 0
    assert result["cleanup"] == "confirmed"
    assert "test_stuck" in log and "Timeout" in log and "test_case.py" in log
    assert "Timeout" in completed.stdout


@pytest.mark.parametrize("where", ["non_daemon_thread", "unconfigure"])
def test_shutdown_hang_fails_even_after_pytest_passed_and_wrote_junit(tmp_path, where):
    if where == "non_daemon_thread":
        source = (
            "import threading,time\n"
            "def test_pass_then_hang():\n"
            "    threading.Thread(target=lambda: time.sleep(30)).start()\n"
        )
        conftest = ""
    else:
        source = "def test_pass_then_hang(): pass\n"
        conftest = "import time\ndef pytest_unconfigure(config): time.sleep(30)\n"
    completed, result, log, output = run_suite(tmp_path, source, phase=2, conftest=conftest)
    assert completed.returncode != 0 and result["reason"] == "phase_timeout"
    assert "test_pass_then_hang PASSED" in log and (output / "junit.xml").is_file()
    assert json.loads((output / "timeout.json").read_text())["phase"] == "shutdown"
    assert (output / "stacks.log").stat().st_size > 0


def test_outer_deadline_covers_missing_child_progress(tmp_path):
    completed, result, _, _ = run_suite(
        tmp_path, "import time\ntime.sleep(30)\n", phase=20, deadline=0.2
    )
    assert completed.returncode != 0 and result["reason"] == "outer_deadline"
    assert result["cleanup"] == "confirmed" and result["elapsed_seconds"] < 10


def test_timeout_marker_remains_failure_when_pytest_returns_zero(tmp_path):
    marker = tmp_path / "diagnostics" / "timeout.json"
    source = (
        "from pathlib import Path\n"
        f"def test_timeout_race(): Path({str(marker)!r}).write_text('{{}}')\n"
    )
    completed, result, log, _ = run_suite(tmp_path, source)
    assert "test_timeout_race PASSED" in log
    assert completed.returncode != 0 and result["reason"] == "phase_timeout"


@pytest.mark.parametrize("outcome", ["pass", "fail", "deadline"])
def test_blocked_console_cannot_stop_result_cleanup_or_parent_exit(tmp_path, outcome):
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        "import sys\n"
        "def pytest_sessionstart(session):\n"
        "    sys.stdout.write(('synthetic progress ' + 'x' * 200 + '\\n') * 8192)\n",
        encoding="utf-8",
    )
    case = tmp_path / "test_case.py"
    source = {
        "pass": "def test_pass(): pass\n",
        "fail": "def test_fail(): assert False\n",
        "deadline": "import time\ntime.sleep(30)\n",
    }[outcome]
    case.write_text(source, encoding="utf-8")
    output = tmp_path / "diagnostics"
    process = subprocess.Popen(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--phase-timeout",
            "20",
            "--test-timeout",
            "20",
            "--deadline",
            "2" if outcome == "deadline" else "12",
            "--",
            "-c",
            str(config),
            str(case),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Deliberately do not read stdout until the supervisor has exited.
        code = process.wait(timeout=20)
        process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=3)
    result = json.loads((output / "result.json").read_text())
    assert result["cleanup"] == "confirmed" and result["elapsed_seconds"] < 18
    log = (output / "pytest.log").read_text()
    assert len(log) > 1_000_000
    if outcome == "pass":
        assert code == result["exit_code"] == 0 and "test_pass PASSED" in log
    else:
        assert code != 0 and result["exit_code"] != 0
        assert result["reason"] == ("outer_deadline" if outcome == "deadline" else "pytest_exit")


def test_long_node_id_remains_in_artifact_and_is_bounded_in_console(tmp_path):
    source = (
        "import pytest\n"
        # pytest exports the node ID via PYTEST_CURRENT_TEST. Keep this case
        # below Windows' 32767-character environment-variable value limit.
        "@pytest.mark.parametrize('value', ['x' * 4097])\n"
        "def test_large_identifier(value): pass\n"
    )
    completed, result, log, _ = run_suite(tmp_path, source)
    assert completed.returncode == result["exit_code"] == 0, (result, log[-4096:])
    assert len(log) > 4097 and max(map(len, completed.stdout.splitlines())) < 2200
    assert "console line truncated; full text in artifact" in completed.stdout
    assert "1 passed" in completed.stdout


def test_oversized_progress_line_retains_original_artifact_bytes(tmp_path):
    source = "def test_short_identifier(): pass\n"
    conftest = (
        "import sys\n"
        "def pytest_sessionstart(session):\n"
        "    sys.stdout.write('synthetic progress ' + 'x' * 524289 + '\\n')\n"
    )
    completed, result, log, _ = run_suite(tmp_path, source, conftest=conftest)
    assert completed.returncode == result["exit_code"] == 0, (result, log[-4096:])
    assert len(log) > 524289 and max(map(len, completed.stdout.splitlines())) < 2200
    assert "x" * 524289 in log
    assert "console line truncated; full text in artifact" in completed.stdout
    assert "1 passed" in completed.stdout


@pytest.mark.parametrize(
    "character", ["é", "あ", "🧪"], ids=["two-byte", "three-byte", "four-byte"]
)
def test_console_truncation_keeps_complete_utf8_characters(tmp_path, character):
    visible = "日本語 "
    prefix = visible + "a" * (2047 - len(visible.encode("utf-8")))
    conftest = (
        "import sys\n"
        "def pytest_sessionstart(session):\n"
        f"    sys.stdout.write({prefix!r} + {character!r} * 70000 + '\\n')\n"
    )
    completed, result, log, _ = run_suite(
        tmp_path, "def test_unicode_progress(): pass\n", conftest=conftest
    )
    assert completed.returncode == result["exit_code"] == 0, (result, log[-4096:])
    assert prefix + character * 70000 in log
    assert visible in completed.stdout and "\ufffd" not in completed.stdout
    assert max(len(line.encode("utf-8")) for line in completed.stdout.splitlines()) < 2200
    assert "console line truncated; full text in artifact" in completed.stdout


def test_utf8_progress_remains_valid_across_file_read_boundaries(tmp_path):
    conftest = (
        "import sys\ndef pytest_sessionstart(session):\n    sys.stdout.write('日本語\\n' * 7000)\n"
    )
    completed, result, log, _ = run_suite(
        tmp_path, "def test_unicode_chunks(): pass\n", conftest=conftest
    )
    assert completed.returncode == result["exit_code"] == 0
    assert log.count("日本語") == completed.stdout.count("日本語") == 7000
    assert "\ufffd" not in completed.stdout


def load_full_suite():
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        import full_suite
    finally:
        sys.path.remove(str(SCRIPT.parent))
    return full_suite


@pytest.mark.parametrize("shards", [1, 2, 3, 4, 7])
def test_shards_cover_every_repository_test_file_exactly_once(shards):
    full_suite = load_full_suite()
    expected = sorted(path.name for path in (ROOT / "tests").glob("test_*.py"))
    assert full_suite.TESTS == ROOT / "tests"

    def partition():
        return [
            [path.name for path in full_suite.shard_files(full_suite.TESTS, shard, shards)]
            for shard in range(1, shards + 1)
        ]

    selected = partition()
    flattened = [name for names in selected for name in names]
    assert sorted(flattened) == expected and len(flattened) == len(set(flattened))
    assert all(selected) and "test_full_suite_watchdog.py" in flattened
    assert selected == partition()


def test_new_test_file_is_assigned_to_a_shard_automatically(tmp_path):
    full_suite = load_full_suite()
    for name, size in [("test_a.py", 50), ("test_b.py", 40), ("test_c.py", 30)]:
        (tmp_path / name).write_text("#" * size, encoding="utf-8")
    (tmp_path / "helper.py").write_text("#" * 500, encoding="utf-8")
    before = [full_suite.shard_files(tmp_path, shard, 2) for shard in (1, 2)]
    assert [[path.name for path in names] for names in before] == [
        ["test_a.py"],
        ["test_b.py", "test_c.py"],
    ]
    (tmp_path / "test_new.py").write_text("#" * 10, encoding="utf-8")
    after = [path.name for shard in (1, 2) for path in full_suite.shard_files(tmp_path, shard, 2)]
    assert sorted(after) == ["test_a.py", "test_b.py", "test_c.py", "test_new.py"]


@pytest.mark.parametrize("shard_args", [["--shard", "0"], ["--shard", "3", "--shards", "2"]])
def test_invalid_shard_is_rejected_before_any_diagnostics(tmp_path, shard_args):
    output = tmp_path / "diagnostics"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output), *shard_args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert completed.returncode == 2 and "--shard" in completed.stderr
    assert not output.exists()
