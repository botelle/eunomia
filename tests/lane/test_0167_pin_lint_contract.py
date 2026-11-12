"""Blackbox checks using the declared fleet-plan CLI only."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def write_plan(path, dependency=None):
    dependencies = ''
    if dependency is not None:
        dependencies = 'depends_on:\n  - ' + json.dumps(dependency) + '\n'
    path.write_text(
        '---\n'
        'id: 9167-pin-contract\n'
        'status: draft\n'
        'repo: operator/eunomia\n'
        'zone: public\n'
        'tier: 3\n'
        'spec_impact: "none: dependency validation fixture"\n'
        'paths:\n'
        '  - "bin/fleet-watch"\n'
        + dependencies
        + '---\n\n'
        '# Pin dependency validation\n\n'
        '## 1. Goal\n\n'
        'Validate a dependency before dispatching a runtime caller.\n\n'
        '## 2. Deliverables\n\n'
        'Reject malformed pin dependencies during plan lint.\n\n'
        '## 3. Boundaries\n\n'
        'Read dependency metadata without moving any repository pin.\n\n'
        '## 4. Definition of done\n\n'
        'The lint command rejects a malformed dependency with exit code 1.\n\n'
        '## 5. Handoff\n\n'
        'Report validation findings to the plan author.\n\n'
        '## 6. Resources\n\n'
        'bin/fleet-watch and bin/fleet-plan.\n',
        encoding='utf-8',
    )


def lint(path):
    return subprocess.run(
        [sys.executable, str(ROOT / 'bin/fleet-plan'), 'lint', str(path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def report(result):
    return f'exit={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


@pytest.mark.covers('D2', 'D4')
@pytest.mark.parametrize(
    'dependency',
    [
        'pin:',
        'pin:operator/eunomia',
        'pin:operator/eunomia:',
        'pin:eunomia:bin/fleet-watch',
        'pin:operator/eunomia/extra:bin/fleet-watch',
        'pin:operator/euno mia:bin/fleet-watch',
        'pin:operator/eunomia:/bin/fleet-watch',
        'pin:operator/eunomia:../bin/fleet-watch',
        'pin:operator/eunomia:bin/../fleet-watch',
    ],
    ids=[
        'empty-entry',
        'missing-path-separator',
        'empty-path',
        'missing-owner',
        'extra-repository-component',
        'invalid-repository-character',
        'absolute-path',
        'leading-parent-segment',
        'interior-parent-segment',
    ],
)
def test_lint_rejects_malformed_pin_dependency(tmp_path, dependency):
    plan = tmp_path / '9167-pin-contract.md'
    write_plan(plan)
    baseline = lint(plan)
    assert baseline.returncode == 0, 'Dependency-free control must pass: ' + report(baseline)

    write_plan(plan, dependency)
    original = plan.read_bytes()
    rejected = lint(plan)
    assert rejected.returncode == 1, report(rejected)
    assert (rejected.stdout + rejected.stderr).strip(), 'Lint must report its validation finding'
    assert plan.read_bytes() == original, 'Lint must leave its input unchanged'
