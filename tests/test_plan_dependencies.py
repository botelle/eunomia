import pytest
"""Tests for `depends_on` (plan 0039): the three forms, `fleet-watch`'s
dispatch-time check, and `fleet-plan lint`'s graph checks.

Reuses `test_fleet_watch`'s and `test_fleet_plan`'s own loaders and fixtures by
IMPORTING those modules (pytest's default "prepend" import mode already puts
`tests/` on `sys.path` to collect them) rather than duplicating the Forgejo/
broker stub machinery here — this file adds one new fixture of its own
(`_api_factory_multi`, below), which those two files have no need for.
"""
import re
import urllib.parse
from pathlib import Path

import test_fleet_plan as tp
import test_fleet_watch as tw

BIN = Path(__file__).resolve().parent.parent / "bin"


# --------------------------------------------------------------- fleet-watch

def _api_factory_multi(protected=True, plan_texts=None, pulls=None,
                       comments=None, path_hits=None, calls=None):
    """Like `test_fleet_watch._api_factory`, but each `plans/<file>` answers
    its OWN text (`_api_factory` always answers the one `plan_text` regardless
    of which file was asked for, which is fine for single-plan tests and
    useless for a `depends_on` test that needs a dependency plan with its own
    id and status). `path_hits` is the set of repo-relative paths that exist
    on main, for the `path:` dependency form."""
    plan_texts = plan_texts or {}
    path_hits = path_hits or set()

    def _api(method, path, body=None, token=None):
        if calls is not None:
            calls.append((method, path))
        if "/branch_protections" in path:
            return (200, [tw._bp()]) if protected else (200, [])
        if path.endswith("/contents/plans?ref=main"):
            return 200, tw._contents_list(plan_texts.keys())
        if "/contents/plans/" in path:
            name = urllib.parse.unquote(
                path.split("/contents/plans/", 1)[1].split("?", 1)[0])
            if name in plan_texts:
                return 200, tw._contents_file(plan_texts[name])
            return 404, None
        if "/contents/" in path:
            # a `path:` dependency lookup: an arbitrary repo path, never
            # under plans/ (that shape is handled above).
            p = urllib.parse.unquote(
                path.split("/contents/", 1)[1].split("?", 1)[0])
            return (200, {}) if p in path_hits else (404, None)
        if "/pulls?" in path:
            return 200, ((pulls or []) if "page=1" in path else [])
        if "/issues/" in path and "/comments" in path:
            rows = list(comments or [])
            m = re.search(r"[?&]since=([^&]+)", path)
            if m:
                since = tw._instant(urllib.parse.unquote(m.group(1)))
                rows = [c for c in rows
                        if since is None or (tw._instant(c.get("created_at")) or since) >= since]
            return 200, rows
        return 404, None
    return _api


DEP_DONE = """---
id: 0008-dep
status: done
repo: operator/sniff
zone: public
tier: 2
---
# Dep
"""

DEPENDENT = """---
id: 0009-thing
status: ready
repo: operator/sniff
zone: public
tier: 2
depends_on: [0008-dep]
---
# Thing
"""


def test_a_met_plan_dependency_dispatches(tmp_path):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    api = _api_factory_multi(plan_texts={"0008-dep.md": DEP_DONE,
                                         "0009-thing.md": DEPENDENT})
    tw._wire(mod, api, orch)
    assert mod.cycle() == 1
    assert len(tw._wait_spawns(log, 1)) == 1


def test_an_unmet_plan_dependency_blocks_and_is_reported(tmp_path, capsys):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    dep_draft = DEP_DONE.replace("status: done", "status: draft")
    api = _api_factory_multi(plan_texts={"0008-dep.md": dep_draft,
                                         "0009-thing.md": DEPENDENT})
    tw._wire(mod, api, orch)
    assert mod.cycle() == 0
    assert tw._spawns(log) == []
    err = capsys.readouterr().err
    assert "0009-thing" in err and "0008-dep" in err and "unmet" in err


def test_a_dependency_on_an_abandoned_plan_reports_that_reason(tmp_path, capsys):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    dep_abandoned = DEP_DONE.replace("status: done", "status: abandoned")
    api = _api_factory_multi(plan_texts={"0008-dep.md": dep_abandoned,
                                         "0009-thing.md": DEPENDENT})
    tw._wire(mod, api, orch)
    assert mod.cycle() == 0
    assert tw._spawns(log) == []
    assert "abandoned" in capsys.readouterr().err


def test_a_dangling_plan_dependency_blocks_rather_than_dispatching(tmp_path):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    api = _api_factory_multi(plan_texts={"0009-thing.md": DEPENDENT})
    tw._wire(mod, api, orch)
    assert mod.cycle() == 0
    assert tw._spawns(log) == []


def test_path_dependency_is_checked_against_main_via_the_forge(tmp_path):
    """D2/Boundaries: a `path:` dependency is read straight off the forge at
    `ref=main`, never a local working tree."""
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    dependent = DEPENDENT.replace("depends_on: [0008-dep]",
                                  "depends_on: [path:bin/fleet-bind]")
    calls = []
    api = _api_factory_multi(plan_texts={"0009-thing.md": dependent},
                             path_hits={"bin/fleet-bind"}, calls=calls)
    tw._wire(mod, api, orch)
    assert mod.cycle() == 1
    assert any(m == "GET" and p.startswith(
        "/repos/operator/sniff/contents/bin/fleet-bind?ref=main")
        for m, p in calls)


def test_an_unmet_path_dependency_blocks(tmp_path):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    dependent = DEPENDENT.replace("depends_on: [0008-dep]",
                                  "depends_on: [path:bin/fleet-bind]")
    api = _api_factory_multi(plan_texts={"0009-thing.md": dependent})
    tw._wire(mod, api, orch)
    assert mod.cycle() == 0
    assert tw._spawns(log) == []


def test_external_dependency_never_auto_satisfies(tmp_path, capsys):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    dependent = DEPENDENT.replace("depends_on: [0008-dep]",
                                  "depends_on: [external:cihost-caddy]")
    api = _api_factory_multi(plan_texts={"0009-thing.md": dependent})
    tw._wire(mod, api, orch)
    assert mod.cycle() == 0
    assert tw._spawns(log) == []
    assert "external" in capsys.readouterr().err


def test_dry_run_reports_a_blocked_plan_and_mutates_nothing(tmp_path):
    orch, log = tw._stub_orchestrator(tmp_path)
    fleet_dir = tmp_path / "fleet"
    mod = tw._load(fleet_dir, FLEET_WATCH_REPOS="operator/sniff")
    dep_draft = DEP_DONE.replace("status: done", "status: draft")
    api = _api_factory_multi(plan_texts={"0008-dep.md": dep_draft,
                                         "0009-thing.md": DEPENDENT})
    tw._wire(mod, api, orch)
    before = (sorted(str(p.relative_to(fleet_dir)) for p in fleet_dir.rglob("*"))
              if fleet_dir.exists() else [])
    assert mod.cycle(dry_run=True) == 0
    after = (sorted(str(p.relative_to(fleet_dir)) for p in fleet_dir.rglob("*"))
             if fleet_dir.exists() else [])
    assert before == after
    events = fleet_dir / "events.jsonl"
    assert not events.exists()


def test_a_blocked_plan_pages_and_events_once_across_ten_cycles_then_clears(tmp_path):
    orch, log = tw._stub_orchestrator(tmp_path)
    fleet_dir = tmp_path / "fleet"
    mod = tw._load(fleet_dir, FLEET_WATCH_REPOS="operator/sniff",
                   FLEET_WATCH_CAP="1")
    dep_draft = DEP_DONE.replace("status: done", "status: draft")
    api = _api_factory_multi(plan_texts={"0008-dep.md": dep_draft,
                                         "0009-thing.md": DEPENDENT})
    tw._wire(mod, api, orch)
    for _ in range(10):
        assert mod.cycle() == 0
    assert tw._spawns(log) == []
    blocked = tw._events(fleet_dir, "plan-blocked")
    assert len(blocked) == 1, blocked
    assert blocked[0]["detail"]["plan"] == "0009-thing"
    assert blocked[0]["detail"]["dependency"] == "0008-dep"
    assert blocked[0]["detail"]["reason"] == "unmet"

    # the dependency clears: the plan dispatches, and no further plan-blocked
    api2 = _api_factory_multi(plan_texts={"0008-dep.md": DEP_DONE,
                                          "0009-thing.md": DEPENDENT})
    # plan 0051: the injected seam moved from `_api` to `_forge` (a
    # fleetforge.Forge-shaped double) — see test_fleet_watch's _ForgeDouble.
    mod._forge = lambda token: tw._ForgeDouble(api2)
    assert mod.cycle() == 1
    assert len(tw._wait_spawns(log, 1)) == 1
    assert len(tw._events(fleet_dir, "plan-blocked")) == 1
    dispatched = tw._events(fleet_dir, "plan-dispatched")
    assert len(dispatched) == 1 and dispatched[0]["detail"]["plan"] == "0009-thing"


def test_block_style_depends_on_is_parsed_by_fleet_watch_too(tmp_path):
    """D4b names BOTH readers. `fleet-plan`'s copy is covered below; this pins
    `bin/fleet-watch`'s `front_matter()`."""
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS="operator/sniff")
    text = ("---\nid: 0009-thing\nstatus: ready\nrepo: operator/sniff\n"
            "zone: public\ntier: 2\ndepends_on:\n  - 0008-dep\n---\n# Thing\n")
    fm = mod.front_matter(text)
    assert fm["depends_on"] == ["0008-dep"]


# --------------------------------------------------------------- fleet-plan

def _plan_with_deps(tmp_path, filename, plan_id, status, depends_on_block,
                    boundaries=None):
    """A well-formed plan with an arbitrary `depends_on:` block, inline or
    block-style — `test_fleet_plan.PLAN` has no depends_on placeholder at all,
    so these tests build the front matter directly."""
    boundaries = boundaries or "- don't reuse the collector regex, because it guards free text"
    text = f"""---
id: {plan_id}
status: {status}
repo: operator/eunomia
zone: public
tier: 1
paths: ["a"]
{depends_on_block}
---

## 1. Goal
g

## 2. Deliverables
- d

## 3. Boundaries
{boundaries}

## 4. Definition of done
- dod

## 5. Handoff
h

## 6. Resources
- lease: b
"""
    p = tmp_path / filename
    p.write_text(text)
    return p


def test_two_plan_cycle_fails_lint_naming_both_ids(tmp_path):
    a = _plan_with_deps(tmp_path, "0009-a.md", "0009-a", "draft",
                        "depends_on: [0010-b]")
    _plan_with_deps(tmp_path, "0010-b.md", "0010-b", "draft",
                    "depends_on: [0009-a]")
    errs = tp._load().lint(a)
    assert any("cycle" in e and "0009-a" in e and "0010-b" in e for e in errs), errs


def test_dangling_dependency_id_fails_lint(tmp_path):
    p = _plan_with_deps(tmp_path, "0009-a.md", "0009-a", "draft",
                        "depends_on: [0099-does-not-exist]")
    errs = tp._load().lint(p)
    assert any("0099-does-not-exist" in e and "not the id of any plan" in e
              for e in errs), errs


def test_block_style_depends_on_is_parsed_not_read_as_empty(tmp_path):
    """The defect this guards: a reader that understood only the inline `[a,
    b]` shape read `depends_on:` / `- id` as an empty list — dependencies
    silently dropped, exactly like review 2573's finding for `paths`."""
    block = "depends_on:\n  - 0099-does-not-exist"
    p = _plan_with_deps(tmp_path, "0009-a.md", "0009-a", "draft", block)
    fm, _, _ = tp._load().parse(p)
    assert fm["depends_on"] == ["0099-does-not-exist"]
    errs = tp._load().lint(p)
    assert any("0099-does-not-exist" in e for e in errs), errs


def test_dependency_on_abandoned_plan_fails_lint_when_dispatchable(tmp_path):
    _plan_with_deps(tmp_path, "0005-dead.md", "0005-dead", "abandoned",
                    "depends_on: []")
    live = _plan_with_deps(tmp_path, "0009-a.md", "0009-a", "ready",
                           "depends_on: [0005-dead]")
    errs = tp._load().lint(live)
    assert any("0005-dead" in e and "abandoned" in e and "edit the edge" in e
              for e in errs), errs


def test_dependency_on_abandoned_plan_warns_not_errors_when_dependent_is_done(tmp_path):
    _plan_with_deps(tmp_path, "0005-dead.md", "0005-dead", "abandoned",
                    "depends_on: []")
    done = _plan_with_deps(tmp_path, "0009-a.md", "0009-a", "done",
                           "depends_on: [0005-dead]")
    mod = tp._load()
    assert mod.lint(done) == []
    warns = mod.dependency_warnings(done)
    assert any("0005-dead" in w for w in warns), warns


def test_a_met_dependency_lints_clean_and_warns_nothing(tmp_path):
    _plan_with_deps(tmp_path, "0005-live.md", "0005-live", "done",
                    "depends_on: []")
    dependent = _plan_with_deps(tmp_path, "0009-a.md", "0009-a", "ready",
                                "depends_on: [0005-live]")
    mod = tp._load()
    assert mod.lint(dependent) == []
    assert mod.dependency_warnings(dependent) == []


@pytest.mark.skip(reason="asserts on the private fleet's plan history, which the public cut omits")
def test_the_repos_own_depends_on_edges_are_four_warnings_zero_errors():
    """The regression this plan's own DoD names: the abandoned-dependency
    edges among the real plans in this repo warn, and only the (now-fixed)
    dispatchable ones would ever error."""
    mod = tp._load()
    plans = sorted((Path(__file__).resolve().parent.parent / "plans").glob("*.md"))
    errs = [e for f in plans for e in mod.lint(f)]
    warns = [w for f in plans for w in mod.dependency_warnings(f)]
    assert errs == [], errs
    assert len(warns) == 4, warns
