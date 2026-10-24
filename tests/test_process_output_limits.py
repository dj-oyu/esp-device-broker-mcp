"""Real owned-child readers enforce finite, per-stream output line bounds."""

import os
import sys

import pytest

from cardputer_mcp.build_process import BuildProcess
from cardputer_mcp.firmware_build import OS_ENV


@pytest.mark.parametrize("limit,size", [(4096, 4096), (4096, 4097)])
def test_real_reader_line_limits_and_stderr_omission(tmp_path, limit, size):
    runner = BuildProcess()
    lines = []
    script = (
        f"import sys; sys.stdout.buffer.write(b'x'*{size}+b'\\n'); "
        "print('synthetic-private-marker'+'z'*5000,file=sys.stderr)"
    )
    code = runner.run(
        [sys.executable, "-c", script],
        {k: v for k, v in os.environ.items() if k.upper() in OS_ENV},
        tmp_path,
        lambda line, stream: lines.append((line, stream)),
        timeout=10,
        stdout_line_bytes=limit,
    )
    warning = "warning: oversized output line omitted"
    assert code == 0 and not runner.active and not runner.uncertain
    assert (warning, "stderr") in lines
    assert all("synthetic-private-marker" not in line for line, _ in lines)
    assert ("x" * size if size <= limit else warning, "stdout") in lines


@pytest.mark.parametrize("limit", [0, 256 * 1024 + 1])
def test_invalid_line_limit_is_rejected_before_any_process(tmp_path, limit):
    runner = BuildProcess()
    with pytest.raises(ValueError, match="^invalid_stdout_line_limit$"):
        runner.run(["never-execute"], {}, tmp_path, lambda *_: None, stdout_line_bytes=limit)
    assert not runner.active and runner.owner is None
