"""Blackbox checks for the declared event transport used by plan 0146.

Credits-specific event registration is a contract gap; these tests exercise
only the existing, unambiguous public transport contract.
"""

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cli(tmp_path):
    fleet = tmp_path / 'fleet'
    fleet.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    env = {
        'PATH': os.environ.get('PATH', os.defpath),
        'HOME': str(home),
        'USER': 'lane-contract',
        'LOGNAME': 'lane-contract',
        'EUNOMIA_FLEET_DIR': str(fleet),
        'EUNOMIA_SESSION': 'lane-contract',
        'FLEET_DB': str(tmp_path / 'fleet.db'),
    }

    def invoke(program, *args):
        return subprocess.run(
            [str(ROOT / 'bin' / program), *args],
            cwd=tmp_path, env=env, input='', text=True,
            capture_output=True, timeout=20, check=False,
        )

    return invoke


def successful(result):
    assert result.returncode == 0, (result.returncode, result.stdout, result.stderr)
    return result.stdout


def read_events(cli, *filters):
    output = successful(cli('fleet-events', '--json', *filters))
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def emit_setting(cli, value):
    detail = {'key': 'lane_run_cap', 'old': None, 'new': value}
    result = cli(
        'fleet-emit', 'fleet-setting-changed',
        '--session', 'lane-contract', '--detail-json', json.dumps(detail),
    )
    record = json.loads(successful(result))
    assert set(record) == {
        'ts', 'actor', 'type', 'repo', 'pr', 'sha', 'lease', 'detail'
    }
    assert isinstance(record['ts'], str) and record['ts']
    assert record['type'] == 'fleet-setting-changed'
    assert record['lease'] is None
    assert record['detail'] == detail
    return record


def test_event_round_trip_and_filter_preserve_records(cli):
    first = emit_setting(cli, 1)
    second = emit_setting(cli, 2)
    assert read_events(cli) == [first, second]
    assert read_events(cli, '--type', 'fleet-setting-changed') == [first, second]
    assert read_events(cli, '--type', 'review-requeued') == []
    assert read_events(cli) == [first, second]


@pytest.mark.parametrize('payload', [
    '{"private-marker-0146":',
    '["private-marker-0146"]',
    '"private-marker-0146"',
    'null',
])
def test_invalid_detail_is_refused_without_appending_or_echoing(cli, payload):
    original = emit_setting(cli, 1)
    result = cli(
        'fleet-emit', 'fleet-setting-changed',
        '--session', 'lane-contract', '--detail-json', payload,
    )
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert 'private-marker-0146' not in result.stdout + result.stderr
    assert read_events(cli) == [original]


def test_unknown_event_type_is_rejected_by_writer_and_reader(cli):
    original = emit_setting(cli, 1)
    unknown = 'lane-contract-undefined-event-0146'
    writer = cli('fleet-emit', unknown, '--session', 'lane-contract')
    assert writer.returncode == 1, (writer.stdout, writer.stderr)
    reader = cli('fleet-events', '--json', '--type', unknown)
    assert reader.returncode == 1, (reader.stdout, reader.stderr)
    assert read_events(cli) == [original]
