"""Plan 0211 D2: `blocked.json`, the file lynceus plan 0051 depends on
existing on `main` — created here, and keeping exactly this name, so that
plan's own lease never has to touch `bin/fleet-watch` to get it.

Imports `test_fleet_watch.py`'s and `test_plan_overlap_hold.py`'s own loaders
and fixtures rather than restating them (the same precedent
`test_plan_dependencies.py`'s own docstring gives)."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import test_fleet_watch as tw
import test_plan_overlap_hold as toh
from test_plan_dependencies import _api_factory_multi

REPO = toh.REPO


def _no_quota(mod):
    """The real UsageBar meter (plan 0094) is read from a fixed path outside
    `tmp_path` — never test-isolated — so a live closed gate on this host
    would otherwise hold operator/sniff's dispatch nondeterministically."""
    mod._quota = lambda: None
    return mod

_D2_TOP_KEYS = {"v", "written_at", "cycle_complete", "held_repos",
               "skipped_repos", "blocked"}
_D2_ENTRY_KEYS = {"repo", "plan", "dependency", "form", "reason", "detail",
                  "since", "pr", "handle", "orphans"}


DEP = """---
id: 0020-dep
status: ready
repo: operator/sniff
zone: public
tier: 2
depends_on: ["0099-missing"]
---
# Dep
"""

STUCK = """---
id: 0023-stuck
status: ready
repo: operator/sniff
zone: public
tier: 2
---
# Stuck
"""

INFLIGHT = """---
id: 0024-inflight
status: ready
repo: operator/sniff
zone: public
tier: 2
---
# Inflight
"""


def _mixed_world(tmp_path, cap="10"):
    plans = toh._two(["bin/orchestrator"], ["bin/orchestrator"])     # P, Q
    texts = {f"{pid}.md": toh._plan(pid, paths, st)
             for pid, (paths, st) in plans.items()}
    texts["0020-dep.md"] = DEP
    texts["0023-stuck.md"] = STUCK
    texts["0024-inflight.md"] = INFLIGHT
    stuck_pr = dict(toh._pr(9, "0023-stuck", "merged"),
                    merged_at="2026-11-05T00:00:00Z", closed_at="2026-11-05T00:00:00Z")
    pulls = [toh._pr(7, toh.Q, "open"), stuck_pr]
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO, FLEET_WATCH_CAP=cap,
                   FLEET_OPERATOR_UID="3")
    tw._wire(mod, _api_factory_multi(plan_texts=texts, pulls=pulls), orch)
    _no_quota(mod)
    mod.lib.write_lease({
        "id": "branch--inflight--001",
        "resource": {"type": "branch", "repo": REPO, "branch": "feat/0024-inflight"},
        "holder": "some-session", "granted_by": "watch-test", "state": "active",
        "ttl_minutes": 60, "created": "2000-01-01T00:00:00Z",
        "activated": mod.lib.ts(mod.lib.now_utc()),
        "note": "plan:0024-inflight",
    })
    return mod, tmp_path / "fleet"


def test_blocked_json_carries_every_form_and_skips_in_flight(tmp_path):
    mod, fd = _mixed_world(tmp_path)
    assert mod.cycle() == 0
    data = json.loads((fd / "blocked.json").read_text())
    assert set(data.keys()) == _D2_TOP_KEYS
    plans = {e["plan"] for e in data["blocked"]}
    assert plans == {"0020-dep", toh.P, "0023-stuck"}
    assert "0024-inflight" not in plans
    for e in data["blocked"]:
        assert set(e.keys()) == _D2_ENTRY_KEYS

    dep_entry = next(e for e in data["blocked"] if e["plan"] == "0020-dep")
    assert dep_entry["form"] == "plan" and dep_entry["pr"] is None

    overlap_entry = next(e for e in data["blocked"] if e["plan"] == toh.P)
    assert overlap_entry["form"] == "overlap" and overlap_entry["pr"] == 7

    stuck_entry = next(e for e in data["blocked"] if e["plan"] == "0023-stuck")
    assert stuck_entry["form"] == "built" and stuck_entry["pr"] == 9
    assert stuck_entry["reason"] == "built-not-done"


def test_since_is_stable_until_the_blocker_changes(tmp_path):
    texts = {"0020-dep.md": DEP}
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO,
                   FLEET_WATCH_CAP="3")
    tw._wire(mod, _api_factory_multi(plan_texts=texts), orch)
    _no_quota(mod)
    fd = tmp_path / "fleet"

    def since_of():
        data = json.loads((fd / "blocked.json").read_text())
        return next(e for e in data["blocked"] if e["plan"] == "0020-dep")["since"]

    assert mod.cycle() == 0
    since1 = since_of()
    # Backdate the state file BEFORE the second cycle: `since` has
    # one-second resolution (`lib.ts`), and a same-second re-run would pass
    # even with `_clear_blocked` wrongly clearing it every cycle (plan 0211
    # D1's own regression). Backdating makes an unwanted rewrite visible.
    state_file = mod._blocked_state_file(REPO, "0020-dep")
    backdated = state_file.stat().st_mtime - 3600
    os.utime(state_file, (backdated, backdated))
    assert mod.cycle() == 0
    since2 = since_of()
    assert since2 != since1, "since must reflect the state file's own mtime"
    assert since2 == mod.lib.ts(datetime.fromtimestamp(backdated, tz=timezone.utc))

    texts["0020-dep.md"] = DEP.replace("0099-missing", "0098-other")
    assert mod.cycle() == 0
    since3 = since_of()
    assert since3 != since2


def test_held_repo_appears_with_gate_and_until(tmp_path, monkeypatch):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO)
    tw._wire(mod, tw._api_factory(), orch)
    _no_quota(mod)
    monkeypatch.setattr(mod, "_hold_for", lambda repo, gates: (
        "vendor:muse", "2026-11-09T00:00:00Z", "implementer#1 sub-codex"))
    assert mod.cycle() == 0
    data = json.loads((tmp_path / "fleet" / "blocked.json").read_text())
    assert data["held_repos"] == [{"repo": "operator/sniff", "gate": "vendor:muse",
                                   "until": "2026-11-09T00:00:00Z",
                                   "seat": "implementer#1 sub-codex"}]


def test_red_main_repo_appears_in_skipped_repos(tmp_path, monkeypatch):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO)
    tw._wire(mod, tw._api_factory(), orch)
    _no_quota(mod)
    monkeypatch.setattr(mod, "main_health_sweep",
                        lambda allow, token, dry_run=False: {"operator/sniff": {"state": "red"}})
    assert mod.cycle() == 0
    data = json.loads((tmp_path / "fleet" / "blocked.json").read_text())
    assert data["skipped_repos"] == [{"repo": "operator/sniff", "why": "main-red"}]


PLAN_A = """---
id: 0030-a
status: ready
repo: operator/sniff
zone: public
tier: 2
---
# A
"""
PLAN_B = """---
id: 0031-b
status: ready
repo: operator/sniff
zone: public
tier: 2
---
# B
"""


def test_a_cap_of_one_with_two_dispatchable_plans_is_incomplete(tmp_path):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO,
                   FLEET_WATCH_CAP="1")
    tw._wire(mod, _api_factory_multi(
        plan_texts={"0030-a.md": PLAN_A, "0031-b.md": PLAN_B}), orch)
    _no_quota(mod)
    assert mod.cycle() == 1
    data = json.loads((tmp_path / "fleet" / "blocked.json").read_text())
    assert data["cycle_complete"] is False


def test_dry_run_writes_no_file(tmp_path):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO)
    tw._wire(mod, tw._api_factory(), orch)
    _no_quota(mod)
    assert mod.cycle(dry_run=True) == 1
    assert not (tmp_path / "fleet" / "blocked.json").exists()


def test_a_cycle_that_returns_early_leaves_the_old_file_untouched(tmp_path, monkeypatch):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO)
    tw._wire(mod, tw._api_factory(), orch)
    _no_quota(mod)
    assert mod.cycle() == 1
    path = tmp_path / "fleet" / "blocked.json"
    before = path.read_bytes()
    monkeypatch.setattr(mod, "_token", lambda: (None, "token helper failed"))
    assert mod.cycle() == 0
    assert path.read_bytes() == before


def test_a_crash_between_write_and_rename_leaves_the_old_file_whole(tmp_path, monkeypatch):
    orch, log = tw._stub_orchestrator(tmp_path)
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=REPO)
    tw._wire(mod, tw._api_factory(), orch)
    _no_quota(mod)
    assert mod.cycle() == 1
    path = tmp_path / "fleet" / "blocked.json"
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(mod.os, "replace", boom)
    assert mod.cycle() == 0
    after = path.read_bytes()
    assert after == before
    assert json.loads(after)["v"] == 1
