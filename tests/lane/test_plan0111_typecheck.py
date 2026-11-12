"""Blackbox check of the typecheck command declared by the CI harness."""
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_repository_typecheck_reports_zero_errors():
    checker = ROOT / '.venv' / 'bin' / 'pyright'
    if not checker.is_file():
        pytest.skip(
            'Harness prerequisite unavailable: CI installs pyright==1.1.414 '
            'after its pytest step; run this test again after that installation.'
        )
    result = subprocess.run(
        [str(checker)],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=False,
    )
    output = result.stdout + '\n' + result.stderr
    assert result.returncode == 0, output
    assert re.search(r'\b0 errors\b', output), output
