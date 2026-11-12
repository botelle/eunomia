"""Exercise the named check harness; workflow routing remains a spec gap."""

from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "target",
    [
        "tests/test_ci_workflow.py",
        "tests/test_adr_status.py",
        "tests/test_orchestrator_session.py::test_the_zone_rule_matches_the_documented_rule",
    ],
)
def test_named_check_runs_and_passes(target):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rs", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output
    assert re.search(r"\b[1-9][0-9]* passed\b", output), output
    assert not re.search(r"\b[1-9][0-9]* (?:skipped|xfailed|xpassed|deselected)\b", output), output
