"""Tests for plan 0142: a lane test says which plan items it covers."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_meet import (PLAN, ROOT, _seams, load_doc, meet, mod as meet_mod,  # noqa: E402,F401
                             world)
from test_fleet_lane import _bundle, _plan, _prompt_worktree, mod as lane_mod  # noqa: E402

HEADINGS = "## 2. Deliverables\n\n### D1. First thing\n\ntext\n\n### D2. Second thing\n\n## 3. Boundaries\n\n### D9. not an item\n"
LEADS = "## 2. Deliverables\n\n**D1:** the lead one\n\n- **D3:** a bulleted lead\n\n**D1:** a repeat\n"
DOD = "## 4. Definition of done\n\n1. passes\n2. is recorded\n\n## 5. Handoff\n\n1. not an item\n"


def test_ids_from_headings():
    assert lane_mod.plan_items(HEADINGS) == [("D1", "First thing"), ("D2", "Second thing")]


def test_ids_from_bold_leads_deduplicated():
    assert lane_mod.plan_items(LEADS) == [("D1", "the lead one"), ("D3", "a bulleted lead")]


def test_ids_from_the_definition_of_done_list():
    assert lane_mod.plan_items(DOD) == [("DoD1", "passes"), ("DoD2", "is recorded")]


def test_a_plan_with_no_items_has_none_and_no_block():
    assert lane_mod.plan_item_ids("# plan\n\nnothing numbered\n") == []
    assert lane_mod._covers_block("# plan\n") == ""


def test_the_prompt_names_every_id_with_its_first_line_in_order_inside_the_budget(tmp_path):
    wt = _prompt_worktree(tmp_path)
    plan = _plan(HEADINGS + "\n" + DOD)
    budget = 100_000
    p = lane_mod.build_prompt(plan, wt, _bundle(), budget=budget)
    assert len(p.encode()) <= budget
    spots = [p.index(f"  {i}: {t}\n") for i, t in
             [("D1", "First thing"), ("D2", "Second thing"), ("DoD1", "passes"), ("DoD2", "is recorded")]]
    assert spots == sorted(spots)
    assert p.startswith(lane_mod.LANE_PREAMBLE)
    assert spots[-1] < p.index("--- PLAN ---") or p.index("--- PLAN ITEMS ---") < p.index("--- PLAN ---")
    assert "pytest.mark.covers" in p[:p.index("--- PLAN ---")]


def test_the_block_counts_toward_the_budget(tmp_path):
    wt = _prompt_worktree(tmp_path)
    plan = _plan(HEADINGS)
    with pytest.raises(lane_mod.Stop):
        lane_mod.build_prompt(plan, wt, _bundle(), budget=len(lane_mod.LANE_PREAMBLE) + len(plan.encode()) + 10)


COVERED = '''\
import pytest


@pytest.mark.covers("D1", "DoD1")
def test_tagged():
    pass


@pytest.mark.covers("D1")
@pytest.mark.covers("D77")
def test_invented():
    pass


def test_untagged():
    pass
'''


def test_a_meeting_records_covers_per_test_and_unknown_ids(world):
    plan_file = f"---\nid: {PLAN}\n---\n\n" + HEADINGS + "\n" + DOD
    world.impl(files={f"plans/{PLAN}.md": plan_file})
    world.lane({"tests/lane/test_c.py": COVERED})
    assert meet(world) == 0
    (ln,) = load_doc(world)["lanes"]
    by = {t["id"].split("::")[-1]: t for t in ln["tests"]}
    assert by["test_tagged"]["covers"] == ["D1", "DoD1"] and by["test_tagged"]["covers_unknown"] == []
    assert sorted(by["test_invented"]["covers"]) == ["D1", "D77"] and by["test_invented"]["covers_unknown"] == ["D77"]
    assert by["test_untagged"]["covers"] == [] and by["test_untagged"]["covers_unknown"] == []
    assert all(t["outcome"] == "pass" for t in ln["tests"])


def test_covers_unknown_is_absent_when_the_plan_is_unreadable(world):
    world.impl()
    world.lane({"tests/lane/test_c.py": COVERED})
    assert meet(world) == 0
    (ln,) = load_doc(world)["lanes"]
    assert all("covers_unknown" not in t and "covers" in t for t in ln["tests"])


def test_pytest_emits_no_unknown_marker_warning_for_covers(tmp_path):
    scratch = meet_mod._new_scratch()
    try:
        (tmp_path / "test_w.py").write_text(COVERED)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:randomly",
             "-p", "meet_report", "-W", "error", "--strict-markers", "-q", "--rootdir", str(tmp_path),
             str(tmp_path / "test_w.py")],
            cwd=tmp_path, capture_output=True, text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(scratch / "plugins")})
        assert r.returncode == 0, r.stdout + r.stderr
        assert "PytestUnknownMarkWarning" not in r.stdout + r.stderr
    finally:
        __import__("shutil").rmtree(scratch, ignore_errors=True)


def test_an_old_document_without_the_fields_still_validates():
    schema = json.loads((ROOT / "config" / "meeting-findings.schema.json").read_text())
    item = schema["properties"]["lanes"]["items"]["properties"]["tests"]["items"]
    assert "covers" not in item["required"] and "covers_unknown" not in item["required"]
