"""CI test selectors must resolve to real regression tests after refactors."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("workflow", sorted((ROOT / ".github/workflows").glob("*.yml")))
def test_workflow_test_file_selectors_exist(workflow):
    selectors = re.findall(r"tests/test[\w.*-]+\.py", workflow.read_text(encoding="utf-8"))
    missing = sorted({selector for selector in selectors if not list(ROOT.glob(selector))})
    assert missing == [], f"{workflow.name} references missing tests: {missing}"
