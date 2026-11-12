"""Public CLI usage checks; meeting-sweep scenario coverage needs a harness."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ('program', 'arguments'),
    [
        ('fleet-watch', ['--not-a-declared-option']),
        ('fleet-meet', []),
        ('fleet-meet', ['example/project', '0140']),
        ('fleet-meet', ['example/project', '0140', '--impl', 'not-an-integer']),
        ('fleet-meet', ['example/project', '0140', '--impl', '923', '--lane', 'not-an-integer']),
        ('fleet-triage', []),
        ('fleet-triage', ['example/project', '0140']),
        ('fleet-triage', ['example/project', '0140', '--impl-sha']),
    ],
)
def test_meeting_commands_reject_malformed_usage(tmp_path, program, arguments):
    env = {
        'PATH': str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        'EUNOMIA_FLEET_DIR': str(tmp_path / 'fleet'),
        'FLEET_WORK_ROOT': str(tmp_path / 'work'),
        'FLEET_DB': str(tmp_path / 'fleet.db'),
    }
    result = subprocess.run(
        [str(ROOT / 'bin' / program), *arguments],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 2, (
        f'{program}: expected declared usage exit 2, got {result.returncode}\n'
        f'stdout: {result.stdout}\nstderr: {result.stderr}'
    )
    assert result.stderr.strip(), 'Malformed usage must produce an argparse diagnostic'
