"""Blackbox CLI checks supported by the supplied contract.

Feature acceptance assertions await resolution of the gaps reported with
this file. These parser checks do not claim D1 or D2 coverage.
"""

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def invoke(tmp_path):
    env = os.environ.copy()
    env['FLEET_DB'] = str(tmp_path / 'fleet.db')
    env['EUNOMIA_FLEET_DIR'] = str(tmp_path / 'fleet')
    env['USER'] = 'lane-contract-test'

    def run(*args):
        return subprocess.run(
            [str(ROOT / 'bin' / 'fleet-models'), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    return run


@pytest.mark.covers()
@pytest.mark.parametrize('position', [
    'plan_reviewer', 'code_reviewer', 'style_reviewer', 'lane_reviewer',
])
def test_reviewers_requires_json_flag(invoke, position):
    result = invoke('reviewers', 'default', position)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert '--json' in result.stderr, result.stderr


@pytest.mark.covers()
@pytest.mark.parametrize('position', [
    'implementer', 'tester', 'mediator', 'impl_supervisor', 'test_supervisor',
])
def test_reviewers_rejects_nonreviewer_positions(invoke, position):
    result = invoke('reviewers', 'default', position, '--json')
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert position in result.stderr, result.stderr
