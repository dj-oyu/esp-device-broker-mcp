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
