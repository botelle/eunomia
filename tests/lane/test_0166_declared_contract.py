"""Blackbox checks limited to the supplied, non-conflicting CLI contract."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli(tmp_path):
    fleet_dir = tmp_path / 'fleet'
    fleet_dir.mkdir()
    env = {
        'PATH': os.environ.get('PATH', os.defpath),
        'FLEET_DB': str(fleet_dir / 'fleet.db'),
        'EUNOMIA_FLEET_DIR': str(fleet_dir),
        'EUNOMIA_SESSION': 'lane-0166-contract',
        'USER': 'lane-tester',
    }

    def run(tool, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / 'bin' / tool), *args],
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    return run


@pytest.mark.covers('DoD4')
@pytest.mark.skip(reason="asserts on the private fleet's plan history, which the public cut omits")
def test_feature_plan_lints(cli):
    result = cli(
        'fleet-plan', 'lint',
        'plans/0166-a-intakebot-job-may-bill-credits-by-grant.md',
    )
    assert result.returncode == 0, (result.stdout, result.stderr)


@pytest.mark.covers('D2')
@pytest.mark.parametrize('missing', ['--repo', '--pr', '--model'])
def test_review_decide_keeps_required_arguments(cli, missing):
    values = {'--repo': 'operator/lane-contract', '--pr': '1', '--model': 'fable'}
    args = ['decide', '--peek', '--json']
    for flag, value in values.items():
        if flag != missing:
            args.extend([flag, value])
    result = cli('fleet-credits', *args)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert missing in result.stderr


@pytest.mark.covers('D2')
@pytest.mark.parametrize('missing', ['--repo', '--pr', '--model', '--usd', '--session'])
def test_review_record_keeps_required_arguments(cli, missing):
    values = {
        '--repo': 'operator/lane-contract', '--pr': '1', '--model': 'fable',
        '--usd': '0.25', '--session': 'lane-0166-record',
    }
    args = ['record', '--dry-run']
    for flag, value in values.items():
        if flag != missing:
            args.extend([flag, value])
    result = cli('fleet-credits', *args)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert missing in result.stderr


@pytest.mark.covers()
@pytest.mark.parametrize('args', [('on', '--days', '1'), ('release', '1')])
def test_existing_owner_commands_refuse_nonterminal_stdin(cli, args):
    result = cli('fleet-credits', *args)
    assert result.returncode == 1, (result.stdout, result.stderr)
