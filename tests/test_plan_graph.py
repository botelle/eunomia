import pytest
"""Tests for `fleet-plan graph` (plan 0047).

Two families, deliberately kept apart:

* Fixture-based tests (`_repo`/`_write_plan`, mirroring `test_fleet_plan_audit.py`)
  pin behaviour independent of whatever `plans/` holds today: form tagging,
  `satisfied` computation, cross-repo `path:` flagging, cycle detection, and D5
  unreachability.

* `PLAN_47_SNAPSHOT` is a frozen copy of this repo's own `plans/` front matter
  as it stood for the hand-built prototype in plan 0047 §2 ("Run against `main`
  on 2026-09-13, before any of this was built"). Two entries are restored to
  their prototype-time values — `0004-plan-dispatcher` was still `draft`, and
  `0023-principals` carried an `external:` entry that a later plan on `main`
  has since resolved and dropped. Both are marked below. Pinning the snapshot
  rather than reading live `plans/` is what makes "13 unlinked, 12 external:
  across 7, 4 done-on-draft/abandoned plans, 5 dependents on 0004" reproducible
  forever instead of true only on the day this was written — `plans/` keeps
  moving (0004 has since shipped) and a test that read it live would already
  be broken.

`test_graph_on_main_...` is the one test that DOES read live `plans/`, because
plan 0047's Definition of done asks for it explicitly ("on `main`"); it checks
only what cannot drift — node count and that every edge carries a form.
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
FLEET_PLAN = BIN / "fleet-plan"
REPO_ROOT = BIN.parent


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_plan", str(FLEET_PLAN))
    spec = importlib.util.spec_from_loader("fleet_plan", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _repo(tmp_path, name="repo", origin=None):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    if origin:
        _git(repo, "remote", "add", "origin", origin)
    return repo


def _commit(repo, message, files):
    for relpath, content in files.items():
        p = repo / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _plan_text(pid, status, deps, repo="operator/eunomia"):
    deps_yaml = "[" + ", ".join(f'"{d}"' for d in deps) + "]"
    return f"""---
id: {pid}
status: {status}
repo: {repo}
zone: public
tier: 2
paths: []
depends_on: {deps_yaml}
---

## 1. Goal
g
"""


def _write_plan(plans_dir, pid, status, deps, repo="operator/eunomia"):
    plans_dir.mkdir(parents=True, exist_ok=True)
    p = plans_dir / f"{pid}.md"
    p.write_text(_plan_text(pid, status, deps, repo))
    return p


# ---------------------------------------------------------------- the snapshot

PLAN_47_SNAPSHOT = [
    ("0002-fleet-emit-events", "done", []),
    ("0003-fleet-claim", "done", ["0002-fleet-emit-events"]),
    # restored to 'draft': its prototype-time status (D.o.D targets 0010 as a
    # done-plan-depending-on-draft finding, which requires this).
    ("0004-plan-dispatcher", "draft", ["0002-fleet-emit-events", "external:fleet-claim",
     "external:oracle-route", "external:intake-plan-ingress", "external:spec-event-types"]),
    ("0005-heartbeat", "abandoned", ["0003-fleet-claim"]),
    ("0006-secret-guard", "done", []),
    ("0007-fleet-cr", "abandoned", ["0002-fleet-emit-events", "0003-fleet-claim"]),
    ("0008-installers", "abandoned", ["0003-fleet-claim", "0007-fleet-cr"]),
    ("0009-agent-asks-a-question", "abandoned", ["0004-plan-dispatcher"]),
    ("0010-implementer-session", "done", ["0004-plan-dispatcher"]),
    ("0011-review-loop", "done", ["0010-implementer-session"]),
    ("0012-session-pid-binding", "done", ["0005-heartbeat"]),
    ("0013-fleet-repo-enable", "draft",
     ["0004-plan-dispatcher", "path:bin/fleet-install-hooks", "0012-session-pid-binding"]),
    ("0014-fleet-pkill-session", "done", ["0012-session-pid-binding", "0002-fleet-emit-events"]),
    ("0015-ignition-gate", "draft", ["0004-plan-dispatcher"]),
    ("0016-independent-test-lane", "draft",
     ["0015-ignition-gate", "0004-plan-dispatcher", "0019-declared-behaviour",
      "external:roadmap-row-21a-lane-isolation"]),
    ("0017-extlane-flag", "draft", ["0013-fleet-repo-enable"]),
    ("0018-fleet-bundle", "done", ["external:adr-0006-blackbox-test-lanes"]),
    ("0019-declared-behaviour", "draft",
     ["0018-fleet-bundle", "external:adr-0006-blackbox-test-lanes"]),
    ("0020-fleet-orphans", "done", ["0013-fleet-repo-enable"]),
    ("0021-event-schema", "draft", []),
    ("0022-forge-module", "done", []),
    # restored: the 7th external:-bearing plan, resolved and dropped since.
    ("0023-principals", "draft", ["external:principals-review"]),
    ("0024-container-evaluation", "abandoned",
     ["external:cihost-runner-config", "external:speakhush-deploy", "external:cihost-caddy"]),
    ("0025-capability-published", "draft", []),
    ("0026-reviewer-trigger", "draft",
     ["0022-forge-module", "external:forgejo-status-webhook-confirmed"]),
    ("0027-leak-watch-entropy-floor", "draft", ["0006-secret-guard"]),
    ("0028-queue-observability", "draft", ["0026-reviewer-trigger", "0021-event-schema"]),
    ("0029-service-lease", "done", ["0003-fleet-claim", "0005-heartbeat", "0008-installers"]),
    ("0030-activation", "draft", []),
    ("0031-comment-ack-protocol", "done", ["0011-review-loop"]),
    ("0032-post-approval-freeze", "draft", ["0031-comment-ack-protocol"]),
    ("0033-container-preconditions", "draft", ["path:docs/adr/0007-container-runtime.md"]),
    ("0034-a-merge-reaches-every-session", "done", ["0029-service-lease"]),
    ("0035-a-stranded-pr-is-visible", "draft", []),
    ("0036-dispatch-state-is-queryable", "done", []),
    ("0038-an-audit-that-refuses-to-guess", "done", []),
    ("0039-a-plan-waits-for-what-it-needs", "done", []),
    ("0040-the-watcher-loads-what-was-merged", "done", ["0034-a-merge-reaches-every-session"]),
    ("0042-the-implementer-bound-is-per-repo", "done", []),
    ("0045-ci-logs-ingested-and-served", "ready", ["path:docs/fleet-db.md"]),
    ("0046-mopsus-escalates-with-a-handle", "done", ["path:docs/fleet-db.md"]),
    ("0047-the-plan-graph-is-drawable", "ready", ["path:tests/test_fleet_plan_audit.py"]),
]


def _write_snapshot(plans_dir):
    return [_write_plan(plans_dir, pid, status, deps) for pid, status, deps in PLAN_47_SNAPSHOT]


def test_prototype_findings_reproduced_from_the_json(tmp_path):
    """The four §2 findings, from `graph()`'s own JSON — not re-derived by a
    second counting method the tool doesn't use."""
    mod = _load()
    files = _write_snapshot(tmp_path / "plans")
    data = mod.graph(files, repo=tmp_path, ref="main")

    assert len(data["nodes"]) == len(PLAN_47_SNAPSHOT) == 42
    assert data["errors"] == []
    assert data["cycles"] == []

    node_status = {n["id"]: n["status"] for n in data["nodes"]}

    # 13 plans with no plan-id depends_on edge, either direction.
    has_edge = set()
    for e in data["edges"]:
        if e["form"] == "plan":
            has_edge.add(e["from"])
            has_edge.add(e["to"])
    unlinked = {n["id"] for n in data["nodes"]} - has_edge
    assert len(unlinked) == 13

    # 12 external: entries across 7 plans.
    ext_edges = [e for e in data["edges"] if e["form"] == "external"]
    assert len(ext_edges) == 12
    assert len({e["from"] for e in ext_edges}) == 7

    # 4 `done` plans depending on a `draft` or `abandoned` plan.
    bad = [e for e in data["edges"] if e["form"] == "plan"
           and node_status.get(e["from"]) == "done"
           and node_status.get(e["to"]) in ("draft", "abandoned")]
    assert {e["from"] for e in bad} == {
        "0010-implementer-session", "0012-session-pid-binding",
        "0020-fleet-orphans", "0029-service-lease"}

    # 5 dependents on 0004.
    dependents = {e["from"] for e in data["edges"]
                 if e["form"] == "plan" and e["to"] == "0004-plan-dispatcher"}
    assert dependents == {
        "0009-agent-asks-a-question", "0010-implementer-session",
        "0013-fleet-repo-enable", "0015-ignition-gate", "0016-independent-test-lane"}

    # 0016 sits behind an external: entry (its own) and is unreachable (D5).
    unreachable = {n["id"] for n in data["nodes"] if n["unreachable"]}
    assert "0016-independent-test-lane" in unreachable


@pytest.mark.skip(reason="asserts on the private fleet's plan history, which the public cut omits")
def test_graph_on_main_emits_a_node_per_plan_every_edge_tagged():
    """The one test that reads live `plans/` — the D.o.D bullet asks for it
    "on main" by name. Only assertions that cannot drift belong here.

    It said `== 42` until 2026-09-13, because plan 0047's §4 named that literal.
    Twelve drafts moved to `docs/plans-need-parsing/` in #393 and `main` went red
    — the count was a snapshot of the plan set on the afternoon the plan was
    written, and the plan set is the one thing this test exists to watch change.

    `len(files)` is the assertion that cannot drift and still says what matters:
    every plan file becomes exactly one node, none dropped and none invented."""
    mod = _load()
    files = sorted(f for f in (REPO_ROOT / "plans").glob("*.md")
                   if f.stem != "0000-template")
    data = mod.graph(files, repo=REPO_ROOT, ref="HEAD")
    assert data["errors"] == []
    assert len(files) > 1, "no plans found — the glob is wrong, not the graph"
    assert len(data["nodes"]) == len(files)
    assert data["edges"]
    assert all(e["form"] in ("plan", "path", "external") for e in data["edges"])


# ----------------------------------------------------------- form and satisfied

def test_plan_form_done_dep_is_satisfied(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "done", []),
             _write_plan(tmp_path / "plans", "0101-b", "draft", ["0100-a"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    edges = [e for e in data["edges"] if e["from"] == "0101-b"]
    assert edges == [{"from": "0101-b", "to": "0100-a", "form": "plan",
                      "satisfied": True, "reason": None}]


def test_plan_form_abandoned_dep_is_unsatisfied_with_reason(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "abandoned", []),
             _write_plan(tmp_path / "plans", "0101-b", "draft", ["0100-a"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    e = next(e for e in data["edges"] if e["from"] == "0101-b")
    assert e["satisfied"] is False
    assert e["reason"] == "abandoned"


def test_plan_form_dangling_id_is_unsatisfied_and_reported(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0101-b", "draft", ["0999-nowhere"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    e = data["edges"][0]
    assert e["to"] == "0999-nowhere"
    assert e["satisfied"] is False
    assert e["reason"] == "dangling"


def test_external_form_is_never_satisfied(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0101-b", "draft", ["external:some-human-step"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    e = data["edges"][0]
    assert e["form"] == "external"
    assert e["to"] == "some-human-step"
    assert e["satisfied"] is None
    assert e["reason"] == "external"


def test_path_form_resolves_against_own_repo(tmp_path):
    repo = _repo(tmp_path, origin="git@forge.example:operator/eunomia.git")
    _commit(repo, "seed", {"bin/real-tool": "x = 1\n"})
    p = _write_plan(repo / "plans", "0101-b", "draft", ["path:bin/real-tool"], repo="operator/eunomia")
    _commit(repo, "plan", {})
    mod = _load()
    data = mod.graph([p], repo=repo, ref="main")
    e = data["edges"][0]
    assert e["form"] == "path"
    assert e["satisfied"] is True
    assert e["reason"] is None


def test_path_form_missing_is_unmet(tmp_path):
    repo = _repo(tmp_path, origin="git@forge.example:operator/eunomia.git")
    p = _write_plan(repo / "plans", "0101-b", "draft", ["path:bin/does-not-exist"])
    _commit(repo, "plan", {})
    mod = _load()
    data = mod.graph([p], repo=repo, ref="main")
    e = data["edges"][0]
    assert e["satisfied"] is False
    assert e["reason"] == "unmet"


# ------------------------------------------------------------ same-repo boundary

def test_path_dep_against_another_repo_is_flagged_not_guessed(tmp_path):
    """`_plan_dependency_block` (fleet-watch) resolves `path:` against the
    DEPENDENT plan's own `repo:` field — never the checkout the graph runs in.
    When a plan declares `repo: operator/other` from inside eunomia's plans/,
    this checkout cannot know whether that path exists on `other`@main, and
    must say so rather than silently returning True or False (Boundaries)."""
    repo = _repo(tmp_path, origin="git@forge.example:operator/eunomia.git")
    # the path exists in THIS checkout, which is exactly the trap: a naive
    # implementation would resolve it here and report satisfied=True.
    _commit(repo, "seed", {"bin/real-tool": "x = 1\n"})
    p = _write_plan(repo / "plans", "0101-b", "draft", ["path:bin/real-tool"],
                    repo="operator/other-repo")
    _commit(repo, "plan", {})
    mod = _load()
    data = mod.graph([p], repo=repo, ref="main")
    e = data["edges"][0]
    assert e["form"] == "path"
    assert e["satisfied"] is None
    assert e["reason"] == "cross_repo"


def test_path_dep_when_own_repo_is_unknown_is_resolved_locally(tmp_path):
    """No `origin` remote at all (e.g. a fresh local checkout) must not be
    read as "every plan is cross-repo" — it degrades to resolving path:
    against this checkout, same as before this boundary existed."""
    repo = _repo(tmp_path)   # no origin
    _commit(repo, "seed", {"bin/real-tool": "x = 1\n"})
    p = _write_plan(repo / "plans", "0101-b", "draft", ["path:bin/real-tool"],
                    repo="operator/other-repo")
    _commit(repo, "plan", {})
    mod = _load()
    data = mod.graph([p], repo=repo, ref="main")
    e = data["edges"][0]
    assert e["satisfied"] is True
    assert e["reason"] is None


# --------------------------------------------------------------------- D5

def test_direct_external_dep_makes_the_node_unreachable(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0101-b", "draft", ["external:x"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    assert data["nodes"][0]["unreachable"] is True


def test_direct_abandoned_plan_dep_makes_the_node_unreachable(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "abandoned", []),
             _write_plan(tmp_path / "plans", "0101-b", "draft", ["0100-a"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    b = next(n for n in data["nodes"] if n["id"] == "0101-b")
    assert b["unreachable"] is True


def test_transitive_external_makes_a_grandchild_unreachable(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "draft", ["external:x"]),
             _write_plan(tmp_path / "plans", "0101-b", "draft", ["0100-a"]),
             _write_plan(tmp_path / "plans", "0102-c", "draft", ["0101-b"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["0100-a"]["unreachable"] is True
    assert by_id["0101-b"]["unreachable"] is True
    assert by_id["0102-c"]["unreachable"] is True


def test_missing_path_or_unmet_live_plan_does_not_make_a_node_unreachable(tmp_path):
    """A missing path can still land; a draft/ready plan dependency can still
    ship. Neither is a D5 blocker — only external: and abandoned are."""
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "draft", []),
             _write_plan(tmp_path / "plans", "0101-b", "draft",
                        ["0100-a", "path:bin/not-yet-written"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["0100-a"]["unreachable"] is False
    assert by_id["0101-b"]["unreachable"] is False


def test_unreachability_propagates_regardless_of_dict_iteration_order():
    """A chain a -> b -> c where only `a` carries the external: entry must
    mark all three unreachable no matter which id `_unreachable_ids` visits
    first. A grandparent (c) reached only by recursing down into a fresh
    child (b, then a) — never by iterating over an already-finished node —
    is exactly the case the parent's exhausted `depends_on` iterator misses
    if propagation only happens on iteration rather than also on pop."""
    mod = _load()
    index = {
        "0100-a": {"depends_on": ["external:x"]},
        "0101-b": {"depends_on": ["0100-a"]},
        "0102-c": {"depends_on": ["0101-b"]},
    }
    forward = mod._unreachable_ids(index)
    reversed_index = dict(reversed(list(index.items())))
    backward = mod._unreachable_ids(reversed_index)
    expected = {"0100-a", "0101-b", "0102-c"}
    assert forward == expected
    assert backward == expected


# ------------------------------------------------------------------- cycles

def test_a_cycle_is_reported_never_crashed_on_never_dropped(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "draft", ["0101-b"]),
             _write_plan(tmp_path / "plans", "0101-b", "draft", ["0100-a"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    assert data["cycles"]
    assert set(data["cycles"][0]) == {"0100-a", "0101-b"}
    # never silently broken: both directions of the cycle are still drawn
    pairs = {(e["from"], e["to"]) for e in data["edges"] if e["form"] == "plan"}
    assert ("0100-a", "0101-b") in pairs
    assert ("0101-b", "0100-a") in pairs
    # both nodes still get a layer, not an exception
    layers = {n["id"]: n["layer"] for n in data["nodes"]}
    assert set(layers) == {"0100-a", "0101-b"}


def test_cli_exits_nonzero_on_a_cycle_and_still_prints_the_graph(tmp_path):
    p1 = tmp_path / "0100-a.md"
    p2 = tmp_path / "0101-b.md"
    p1.write_text(_plan_text("0100-a", "draft", ["0101-b"]))
    p2.write_text(_plan_text("0101-b", "draft", ["0100-a"]))
    out = subprocess.run([sys.executable, str(FLEET_PLAN), "graph", str(p1), str(p2),
                          "--format", "json"], capture_output=True, text=True)
    assert out.returncode == 1, out.stderr
    data = json.loads(out.stdout)
    assert data["cycles"]
    assert len(data["nodes"]) == 2


def test_no_cycle_exits_zero(tmp_path):
    p1 = tmp_path / "0100-a.md"
    p1.write_text(_plan_text("0100-a", "draft", []))
    out = subprocess.run([sys.executable, str(FLEET_PLAN), "graph", str(p1),
                          "--format", "json"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_layering_and_cycle_detection_never_recurse(tmp_path):
    """A big ring (every node depends on exactly one other, forming one large
    cycle) reproduced as a plain dict — no recursion, so a hand-written cycle
    of any size cannot blow the stack (Boundaries)."""
    mod = _load()
    n = 400
    ids = [f"p{i}" for i in range(n)]
    index = {ids[i]: {"depends_on": [ids[i - 1]]} for i in range(n)}

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(80)
    try:
        layers = mod._plan_layers(index)
        cycles = mod._find_cycles(index)
        unreachable = mod._unreachable_ids(index)
    finally:
        sys.setrecursionlimit(old_limit)

    assert set(layers) == set(ids)
    assert len(cycles) == 1
    assert set(cycles[0]) == set(ids)
    assert unreachable == set()   # a cycle alone is not a D5 blocker


# ------------------------------------------------------------------ mermaid

def test_mermaid_renders_every_node_and_distinguishes_the_three_forms(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "done", []),
             _write_plan(tmp_path / "plans", "0101-b", "draft",
                        ["0100-a", "path:bin/x", "external:human-step"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    mm = mod._mermaid(data)
    assert mm.startswith("graph TD\n")
    assert "0100-a" in mm and "0101-b" in mm
    assert "external: human-step" in mm
    assert "path: bin/x" in mm
    assert "==>|external|" in mm       # external gets its own arrow style
    assert mm.count('"') % 2 == 0      # every opened label is closed


def test_mermaid_on_the_snapshot_has_one_node_per_plan(tmp_path):
    mod = _load()
    files = _write_snapshot(tmp_path / "plans")
    data = mod.graph(files, repo=tmp_path, ref="main")
    mm = mod._mermaid(data)
    for pid, _status, _deps in PLAN_47_SNAPSHOT:
        assert pid in mm


def test_cli_mermaid_format_writes_a_fenced_diagram(tmp_path):
    p1 = tmp_path / "0100-a.md"
    p1.write_text(_plan_text("0100-a", "draft", []))
    out_file = tmp_path / "out.mmd"
    out = subprocess.run([sys.executable, str(FLEET_PLAN), "graph", str(p1),
                          "--format", "mermaid", "--out", str(out_file)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out_file.read_text().startswith("graph TD")


# ---------------------------------------------------------------------- svg

def _node_rects(svg_text):
    root = ET.fromstring(svg_text)
    ns = "{http://www.w3.org/2000/svg}"
    rects = []
    for el in root.iter(f"{ns}rect"):
        if "node" in (el.get("class") or ""):
            rects.append((float(el.get("x")), float(el.get("y")),
                          float(el.get("width")), float(el.get("height"))))
    return rects


def _overlaps(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + bh


def _assert_no_overlaps(rects):
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            assert not _overlaps(rects[i], rects[j]), (rects[i], rects[j])


def test_svg_is_valid_xml_with_no_overlapping_node_boxes_small_fixture(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "done", []),
             _write_plan(tmp_path / "plans", "0101-b", "draft", ["0100-a"]),
             _write_plan(tmp_path / "plans", "0102-c", "draft", ["0100-a"]),
             _write_plan(tmp_path / "plans", "0103-d", "draft", ["0101-b", "0102-c"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    svg = mod._svg(data)
    rects = _node_rects(svg)
    assert len(rects) == 4
    _assert_no_overlaps(rects)


def test_svg_no_overlapping_boxes_on_the_full_snapshot(tmp_path):
    """The real stress case: 42 plans across several layers, some with a
    dozen dependents (0004) — the shape most likely to expose an off-by-one
    in the grid arithmetic."""
    mod = _load()
    files = _write_snapshot(tmp_path / "plans")
    data = mod.graph(files, repo=tmp_path, ref="main")
    svg = mod._svg(data)
    rects = _node_rects(svg)
    assert len(rects) == 42
    _assert_no_overlaps(rects)


def test_svg_marks_unreachable_nodes_distinctly(tmp_path):
    mod = _load()
    files = [_write_plan(tmp_path / "plans", "0100-a", "draft", ["external:x"])]
    data = mod.graph(files, repo=tmp_path, ref="main")
    svg = mod._svg(data)
    assert "#c00" in svg   # the unreachable stroke colour


def test_cli_svg_format_writes_valid_xml(tmp_path):
    p1 = tmp_path / "0100-a.md"
    p1.write_text(_plan_text("0100-a", "draft", []))
    out_file = tmp_path / "out.svg"
    out = subprocess.run([sys.executable, str(FLEET_PLAN), "graph", str(p1),
                          "--format", "svg", "--out", str(out_file)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    ET.fromstring(out_file.read_text())   # raises on malformed XML


# -------------------------------------------------------------------- docs

def test_plan_graph_doc_exists_and_names_the_three_forms():
    doc = (REPO_ROOT / "docs" / "plan-graph.md").read_text()
    assert "plan" in doc and "path:" in doc and "external:" in doc
