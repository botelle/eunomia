"""Blackbox usage checks; behavioural coverage is blocked by bundle gaps."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def invoke(tool, arguments, tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    env = {
        'HOME': str(home),
        'PATH': os.defpath,
        'LANG': 'C',
        'TMPDIR': str(tmp_path),
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    return subprocess.run(
        [sys.executable, str(ROOT / 'bin' / tool), *arguments],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.covers()
@pytest.mark.parametrize('arguments', [
    ['operator/eunomia', '0175'],
    ['operator/eunomia', '0175', '--impl', 'not-an-integer'],
    ['operator/eunomia', '0175', '--impl', '101', '--lane'],
    ['operator/eunomia', '0175', '--impl', '101', '--lane', 'not-an-integer'],
    ['operator/eunomia', '0175', '--impl', '101', '--lane', '102', '--lane', 'bad'],
])
def test_meet_rejects_malformed_cli(arguments, tmp_path):
    result = invoke('fleet-meet', arguments, tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)


@pytest.mark.covers()
def test_watch_rejects_meeting_job_without_arguments(tmp_path):
    result = invoke('fleet-watch', ['--meeting-job'], tmp_path)
    assert result.returncode == 2, (result.stdout, result.stderr)
