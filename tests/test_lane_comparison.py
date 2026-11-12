"""Tests for plan 0143: two lanes are compared, item by item."""
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(m)
    return m


meet = _load("fleet_meet_for_comparison", ROOT / "bin" / "fleet-meet")
triage = _load("fleet_triage_for_comparison", ROOT / "bin" / "fleet-triage")
SCHEMA = json.loads((ROOT / "config" / "meeting-findings.schema.json").read_text())
SHA = "a" * 40
H = "b" * 64
ITEMS = [("D1", "one"), ("D2", "two"), ("D3", "three"), ("D4", "four"), ("D5", "five")]


def T(name, outcome, covers=None):
    t = {"id": name, "outcome": outcome, "pytest_outcome": {"pass": "passed", "fail": "failed",
         "skip": "skipped", "error": "error"}[outcome], "reason": None if outcome == "pass" else "why",
         "reason_redacted": [], "source_path": "tests/lane/x.py", "duration_s": 0.0}
    if covers is not None:
        t["covers"] = covers
    return t


def lane(runner, pr, tests, gaps=None):
    return {"runner": runner, "pr": pr, "tests": tests, "_gaps": gaps}


def build(lanes, items=ITEMS, not_run=(), dispatched=()):
    return meet.build_comparison(lanes, items, list(not_run), list(dispatched),
                                 {l["runner"]: l["_gaps"] for l in lanes})


def verdicts(c):
    return {i["id"]: i["verdict"] for i in c["items"]}


def test_each_of_the_five_verdicts():
    a = lane("sub-a", 2, [T("a1", "pass", ["D1"]), T("a2", "fail", ["D2"]), T("a3", "pass", ["D3"]),
                          T("a4", "pass", ["D4"])])
    b = lane("sub-b", 3, [T("b1", "pass", ["D1"]), T("b2", "error", ["D2"]), T("b3", "fail", ["D3"])])
    c = build([a, b])
    assert verdicts(c) == {"D1": "agree-pass", "D2": "agree-fail", "D3": "disagree",
                           "D4": "one-lane", "D5": "uncovered"}
    d3 = next(i for i in c["items"] if i["id"] == "D3")
    assert d3["lanes"]["sub-b"] == {"tests": 1, "pass": 0, "fail": 1, "skip": 0}
    assert c["partial"] is False


def test_skips_only_are_inconclusive_not_agreement():
    a = lane("sub-a", 2, [T("a", "skip", ["D1"])])
    b = lane("sub-b", 3, [T("b", "fail", ["D1"])])
    assert verdicts(build([a, b], [("D1", "one")])) == {"D1": "inconclusive"}


def test_untagged_tests_are_listed_per_lane():
    a = lane("sub-a", 2, [T("a1", "pass"), T("a2", "fail", [])])
    b = lane("sub-b", 3, [T("b1", "pass", ["D1"])])
    c = build([a, b])
    assert c["untagged"] == {"sub-a": [{"id": "a1", "outcome": "pass"}, {"id": "a2", "outcome": "fail"}],
                             "sub-b": []}


def test_a_missing_lane_makes_it_partial_and_names_its_reason():
    a = lane("sub-a", 2, [T("a", "pass", ["D1"])])
    b = lane("sub-b", 3, [T("b", "pass", ["D1"])])
    c = build([a, b], not_run=[{"pr": 9, "reason": "no approval at its head", "runner": "sub-c"}],
              dispatched=["sub-a", "sub-b", "sub-c", "sub-d"])
    assert c["partial"] is True
    assert c["lanes_missing"] == [
        {"runner": "sub-c", "pr": 9, "reason": "no approval at its head"},
        {"runner": "sub-d", "pr": None,
         "reason": "dispatched, but no lane pull request is in this meeting"}]


def test_lanes_with_different_prompt_hashes_have_no_comparison():
    def ln(h):
        return {"pr": 1, "runner": "r", "prompt_sha256": h, "prompt_sha256_verified": True}
    comparable, why = meet.compare([ln("c" * 64), ln("d" * 64)])
    assert comparable is False and why
    # run() writes `comparison` only when compare() said True.
    src = (ROOT / "bin" / "fleet-meet").read_text()
    assert "if comparable:\n        comparison = build_comparison(" in src


def test_parse_gaps_reads_the_rendered_block():
    body = "x\n\ngaps:\n```\n> cannot test a\n>\n> or b\n```\n"
    assert meet.parse_gaps(body) == ["cannot test a", "", "or b"]
    assert meet.parse_gaps("no block") is None


# ------------------------------------------------------------ the document
def document(partial=False):
    a = lane("sub-a", 2, [T("tests/a.py::t1", "pass", ["D1"]), T("tests/a.py::t3", "pass", ["D3"]),
                          T("tests/a.py::u", "skip")], gaps=["a gap | with <!-- x"])
    b = lane("sub-b", 3, [T("tests/b.py::t1", "pass", ["D1"]), T("tests/b.py::t3", "fail", ["D3"])],
             gaps=[])
    c = build([a, b], items=ITEMS[:3], not_run=[{"pr": 9, "reason": "refused: bad path", "runner": "sub-c"}]
              if partial else [])
    lanes = []
    for l in (a, b):
        lanes.append({"pr": l["pr"], "head_sha": SHA, "branch": f"agent/tests/p/{l['runner']}",
                      "runner": l["runner"], "dispatch_sha": SHA, "prompt_sha256": H,
                      "prompt_sha256_source": "pr-body", "prompt_sha256_verified": True,
                      "prompt_sha256_reason": None, "prompt_sha256_lane_run": H, "review_id": 1,
                      "read_by": "operator", "read_at": "t", "independent_reader": False, "files": [],
                      "run": {"outcome": "ran", "pytest_exit": 1, "duration_s": 1.0, "reason": None},
                      "counts": meet.counts_of(l["tests"]), "tests": l["tests"]})
    return {"schema": "meeting-findings/1", "repo": "operator/x", "plan": "p",
            "generated_at": "2026-10-18T00:00:00Z", "impl": {"pr": 1, "head_sha": SHA}, "lanes": lanes,
            "not_run": [], "comparable": True, "comparability": None, "all_passed": False,
            "comparison": c,
            "confinement": {"profile_sha256": H,
                            "network": {"outbound": "this-host-only", "bind": "any-local-address",
                                        "denied_ports": [8200]},
                            "env_keys": [], "timeout_seconds": 1}}


def test_the_document_validates_against_the_schema():
    for partial in (False, True):
        assert meet.lane.validate_schema(document(partial), SCHEMA) == []
    doc = document()
    del doc["comparison"]
    assert meet.lane.validate_schema(doc, SCHEMA) == []
    doc = document()
    doc["comparison"]["items"][0]["verdict"] = "bogus"
    assert meet.lane.validate_schema(doc, SCHEMA) != []


def render(doc, good=()):
    return "\n".join(triage.render_comparison(doc, good))


def test_the_comment_renders_table_untagged_missing_lane_and_gaps():
    doc = document(partial=True)
    good = [{"lane": "sub-b", "test": "tests/b.py::t3", "class": "undetermined", "verified": True}]
    text = render(doc, good)
    assert text.startswith("### Lanes compared")
    assert "Partial" in text
    assert "| D1 one | 1/0/0 | 1/0/0 | agree-pass |" in text
    assert "| D3 three | 1/0/0 | 0/1/0 | disagree (classified #1) |" in text
    assert "| D2 two | 0/0/0 | 0/0/0 | uncovered |" in text
    assert "untagged, `sub-a`: 1 test(s)" in text and "skip 1" in text
    assert "missing lane `sub-c` (#9): refused: bad path" in text
    assert "> a gap | with  x" in text           # fenced, comment opener removed
    assert "(none)" in text                       # sub-b wrote no gaps
    assert "<!--" not in text


def test_a_document_without_a_comparison_renders_nothing():
    doc = document()
    del doc["comparison"]
    assert triage.render_comparison(doc) == []


def test_the_section_is_the_same_whatever_the_triager_says():
    doc = document()
    assert render(doc) == render(doc, good=[])
    # no model text enters it: every line is a table row, a count, or a fenced gap.
    for line in render(doc).splitlines():
        assert (not line or line.startswith(("|", "-", ">", "`", "###", "Gaps", "Partial"))), line
