"""Plan 0163 D3: the lane seat's expected accounts, one helper for the orchestrator
and fleet-meet."""
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


@pytest.fixture
def orch(monkeypatch):
    mod = _module("orch_lane_review", BIN / "orchestrator")
    monkeypatch.setattr(mod.lib, "agent_roles", lambda: {
        "reviewer": type("R", (), {"account": "Revbot"})(),
        "second_reviewer": type("R", (), {"account": "rev2bot"})()})
    return mod


def _fake(monkeypatch, mod, seats):
    """seats: {position: (returncode, [runners])}; records the positions asked."""
    asked = []

    def run(argv, **kw):
        pos = argv[argv.index("reviewers") + 2]
        asked.append(pos)
        rc, rows = seats.get(pos, (3, []))
        import json
        return subprocess.CompletedProcess(argv, rc, json.dumps(
            [{"ordinal": i + 1, "runner": r} for i, r in enumerate(rows)]), "")
    monkeypatch.setattr(mod.subprocess, "run", run)
    return asked


CODE = (0, ["a", "b"])


@pytest.mark.parametrize("rows,want", [(["x"], ["revbot"]),
                                       (["x", "y"], ["revbot", "rev2bot"])])
def test_lane_reviewer_is_chosen_at_one_row_and_two(orch, monkeypatch, rows, want):
    asked = _fake(monkeypatch, orch, {"lane_reviewer": (0, rows), "code_reviewer": CODE})
    assert orch.lane_expected_reviewers("operator/x") == want
    assert "code_reviewer" not in asked


@pytest.mark.parametrize("rc", [2, 3])
def test_code_reviewer_on_exit_2_and_3_with_one_log_line(orch, monkeypatch, capsys, rc):
    _fake(monkeypatch, orch, {"lane_reviewer": (rc, []), "code_reviewer": CODE})
    assert orch.lane_expected_reviewers("operator/x") == ["revbot", "rev2bot"]
    lines = [l for l in capsys.readouterr().err.splitlines() if "lane_reviewer" in l]
    assert len(lines) == 1 and "operator/x" in lines[0] and f"exit {rc}" in lines[0]


def test_any_other_failure_is_a_stop(orch, monkeypatch):
    _fake(monkeypatch, orch, {"lane_reviewer": (1, []), "code_reviewer": CODE})
    with pytest.raises(orch.Stop):
        orch.lane_expected_reviewers("operator/x")


def test_fleet_meet_resolves_the_same_accounts(orch, monkeypatch):
    _fake(monkeypatch, orch, {"lane_reviewer": (0, ["x"]), "code_reviewer": CODE})
    meet = _module("fleet_meet_lane_review", BIN / "fleet-meet")
    monkeypatch.setattr(meet, "_orch", lambda: orch)
    assert meet.expected_reviewers("operator/x") == orch.lane_expected_reviewers("operator/x")
    assert meet.expected_reviewers("operator/x") == ["revbot"]
