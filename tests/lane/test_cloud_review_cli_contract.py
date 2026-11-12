"""Blackbox CLI checks for plan 0130; no implementation imports.

Run with: python -m pytest -q tests/lane/test_cloud_review_cli_contract.py
Operational coverage is blocked by the harness gaps reported with this file.
"""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'bin' / 'fleet-cloud-review'


@pytest.mark.parametrize('argv', [
    ['request'],
    ['request', 'operator/lynceus'],
    ['request', 'operator/lynceus', '96'],
    ['request', 'operator/lynceus', '96', '--session'],
    ['request', 'operator/lynceus', 'not-an-integer', '--session', 'lane-session'],
    ['collect'],
    ['collect', 'operator/lynceus'],
    ['collect', 'operator/lynceus', 'not-an-integer'],
    ['prune'],
    ['not-a-subcommand'],
])
def test_malformed_invocation_exits_two(tmp_path, argv):
    # A fresh environment avoids inheriting credentials or live fleet settings.
    # Only malformed invocations are exercised: none should enter an operation.
    home = tmp_path / 'home'
    home.mkdir()
    scratch = tmp_path / 'tmp'
    scratch.mkdir()
    env = {
        'PATH': os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
        'HOME': str(home),
        'TMPDIR': str(scratch),
        'LANG': 'C',
        'LC_ALL': 'C',
    }
    result = subprocess.run(
        [str(CLI), *argv],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 2, (
        f'argv={argv!r}; exit={result.returncode}; '
        f'stdout={result.stdout!r}; stderr={result.stderr!r}'
    )
