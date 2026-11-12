"""Blackbox checks through the declared fleet-plan CLI.

Run with: python3 -m pytest -q tests/lane/test_0159_dependency_lint_contract.py
No implementation imports or private-function calls are used.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def plan_text(dependencies):
    return (
        '---\n'
        'id: 9999-lane-dependency-validation\n'
        'status: ready\n'
        'repo: operator/eunomia\n'
        'zone: public\n'
        'tier: 3\n'
        'spec_impact: "none: dependency validation fixture"\n'
        'paths:\n'
        '  - "docs/plan-dependencies.md"\n'
        'depends_on: ' + json.dumps(dependencies) + '\n'
        '---\n\n'
        '# Dependency validation fixture\n\n'
        '## 1. Goal\n\n'
        'Validate dependency names through the public lint command.\n\n'
        '## 2. Deliverables\n\n'
        '### D1. Dependency validation\n\n'
        'Reject owner dependency names outside the declared grammar.\n\n'
        '## 3. Boundaries\n\n'
        '- **Do not dispatch this plan**, because it is a lint fixture only.\n\n'
        '## 4. Definition of done\n\n'
        '1. The lint command accepts the dependency-free control.\n'
        '2. The lint command rejects invalid owner dependency names.\n\n'
        '## 5. Handoff\n\n'
        'The test runner records the command exit status.\n\n'
        '## 6. Resources\n\n'
        '- docs/plan-dependencies.md\n'
    )


def lint(path, home):
    # Avoid inheriting operator tokens or fleet configuration.
    env = {
        'PATH': os.environ.get('PATH', os.defpath),
        'HOME': str(home),
        'LANG': 'C',
    }
    return subprocess.run(
        [sys.executable, str(ROOT / 'bin/fleet-plan'), 'lint', str(path)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def diagnostic(result):
    return f'exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


@pytest.mark.covers('D3')
@pytest.mark.parametrize('name', [
    'nested/check.sh',
    '../check.sh',
    'check..sh',
    'check step.sh',
    'check;true.sh',
    'check$(true).sh',
    '-check.sh',
    'Check.sh',
    'check',
    '',
])
def test_lint_rejects_invalid_owner_names(tmp_path, name):
    home = tmp_path / 'home'
    home.mkdir()
    plan = tmp_path / '9999-lane-dependency-validation.md'

    # A successful control prevents unrelated fixture validation errors
    # from masquerading as rejection of the dependency under test.
    plan.write_text(plan_text([]), encoding='utf-8')
    control_bytes = plan.read_bytes()
    control = lint(plan, home)
    assert control.returncode == 0, diagnostic(control)
    assert plan.read_bytes() == control_bytes

    plan.write_text(plan_text(['owner:' + name]), encoding='utf-8')
    invalid_bytes = plan.read_bytes()
    result = lint(plan, home)
    assert result.returncode == 1, diagnostic(result)
    assert (result.stdout + result.stderr).strip(), 'Lint must report a finding'
    assert plan.read_bytes() == invalid_bytes
