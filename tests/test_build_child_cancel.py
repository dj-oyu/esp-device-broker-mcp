"""Real synthetic child-tree cancellation; no compiler, broker service or USB."""

import os
import sys
import threading
import time

from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.firmware_build import OS_ENV


def test_cancel_after_configure_stops_owned_descendant_and_drains_readers(tmp_path):
    marker = tmp_path / "orphan-must-not-write"
    child = f"import time;from pathlib import Path;time.sleep(2);Path({str(marker)!r}).touch()"
    command = (
        f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);"
        "print('CMAKE_DONE',flush=True);time.sleep(60)"
    )
    runner = BuildProcess()
    configured = threading.Event()
    errors = []
    lines = []

    def emit(line, stream):
        lines.append((line, stream))
        if line == "CMAKE_DONE":
            configured.set()

    def execute():
        try:
            runner.run(
                [sys.executable, "-c", command],
                {k: v for k, v in os.environ.items() if k.upper() in OS_ENV},
                tmp_path,
                emit,
                timeout=10,
            )
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=execute)
    worker.start()
    try:
        assert configured.wait(5)
    finally:
        runner.stop_requested.set()
        worker.join(7)
    assert not worker.is_alive()
    assert len(errors) == 1 and str(errors[0]) == "build_interrupted_needs_review"
    assert ("CMAKE_DONE", "stdout") in lines
    assert not runner.active and not runner.uncertain and not runner.cleanup_pending
    assert runner.owner is None
    time.sleep(2.2)
    assert not marker.exists()
