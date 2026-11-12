"""Blackbox checks using the CLI and database setup shown in the CI contract."""

import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLAUDE_RUNNERS = (
    'sub-sonnet', 'sub-opus', 'api-sonnet',
    'sub-fable', 'sub-opus-5-5', 'sub-sonnet-5',
)
POSITIONS = (
    'implementer', 'plan_reviewer', 'code_reviewer', 'tester',
    'mediator', 'impl_supervisor', 'test_supervisor',
)
REVIEWERS = ('plan_reviewer', 'code_reviewer')
VENDORS = ('sub-codex', 'sub-antigravity')
SPAWN_REASON = 'no review spawn path yet for its harness'


@pytest.fixture
def cli(tmp_path):
    home = tmp_path / 'home'
    fleet = tmp_path / 'fleet'
    home.mkdir()
    fleet.mkdir()
    env = {
        'PATH': os.environ.get('PATH', os.defpath),
        'HOME': str(home),
        'USER': 'lane-0153',
        'LOGNAME': 'lane-0153',
        'TMPDIR': str(tmp_path),
        'FLEET_DB': str(tmp_path / 'fleet.db'),
        'EUNOMIA_FLEET_DIR': str(fleet),
    }

    def invoke(tool, *args):
        return subprocess.run(
            [str(ROOT / 'bin' / tool), *args],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=60,
            check=False,
        )

    # The supplied talos-contract CI workflow provisions a fresh database this way.
    migrated = invoke('fleet-db-migrate')
    assert migrated.returncode == 0, (
        f'database setup failed: {migrated.stdout}\n{migrated.stderr}'
    )
    return lambda *args: invoke('fleet-models', *args)


def succeeds(result):
    assert result.returncode == 0, (
        f'exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}'
    )


def contains_name(line, name):
    return re.search(r'(?<![\w-])' + re.escape(name) + r'(?![\w-])', line)


@pytest.mark.covers('D3', 'D5')
@pytest.mark.parametrize('runner', CLAUDE_RUNNERS)
@pytest.mark.parametrize('position', POSITIONS)
def test_claude_runner_can_be_configured_and_resolved(cli, runner, position):
    result = cli('set', 'default', position, runner, '--actor', 'lane-0153')
    succeeds(result)
    assert len(result.stdout.strip().splitlines()) == 1

    resolved = cli('get', 'default')
    succeeds(resolved)
    assert any(
        contains_name(line, position) and contains_name(line, runner)
        for line in resolved.stdout.splitlines()
    ), resolved.stdout


@pytest.mark.covers('D3', 'D5')
@pytest.mark.parametrize('runner', VENDORS)
def test_non_claude_runner_is_accepted_at_tester(cli, runner):
    result = cli('set', 'default', 'tester', runner, '--actor', 'lane-0153')
    succeeds(result)
    assert len(result.stdout.strip().splitlines()) == 1

    resolved = cli('get', 'default')
    succeeds(resolved)
    assert any(
        contains_name(line, 'tester') and contains_name(line, runner)
        for line in resolved.stdout.splitlines()
    ), resolved.stdout


@pytest.mark.covers('D4', 'D5')
@pytest.mark.parametrize('runner', VENDORS)
@pytest.mark.parametrize('position', REVIEWERS)
def test_subscription_runners_hold_reviewer_seats(cli, runner, position):
    result = cli('set', 'default', position, runner, '--actor', 'lane-0153')
    assert result.returncode == 0, (
        f'exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}'
    )
    assert SPAWN_REASON not in result.stdout + result.stderr
