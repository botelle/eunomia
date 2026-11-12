"""Plan 0180 D2: `plan:<owner/repo>:<id>` in `depends_on` — a plan on another
repository's main, with the forge stubbed by replacing `plan_files`."""
from pathlib import Path

import test_fleet_plan as tp
import test_fleet_watch as tw

BIN = Path(__file__).resolve().parent.parent / "bin"
OTHER = "operator/deskapp"
HERE = "operator/eunomia"


# --------------------------------------------------------------- fleet-watch

def _watch(tmp_path, monkeypatch, repos=(HERE, OTHER)):
    mod = tw._load(tmp_path / "fleet", FLEET_WATCH_REPOS=HERE)
    monkeypatch.setattr(mod, "conf_repos", lambda: ([HERE], sorted(repos)))
    return mod


def _stub_plans(mod, monkeypatch, per_repo):
    """`per_repo`: {repo: [(path, fm), ...] or None (unreadable)}."""
    monkeypatch.setattr(mod, "plan_files",
                        lambda repo, token: per_repo.get(repo))


def _block(mod, dep):
    return mod._plan_dependency_block(HERE, [dep], {}, "t")


def test_met_at_done(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch)
    _stub_plans(mod, monkeypatch,
               {OTHER: [("plans/0001-x.md", {"id": "0001-x", "status": "done"})]})
    assert _block(mod, f"plan:{OTHER}:0001-x") is None


def test_unmet_when_ready(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch)
    _stub_plans(mod, monkeypatch,
               {OTHER: [("plans/0001-x.md", {"id": "0001-x", "status": "ready"})]})
    got = _block(mod, f"plan:{OTHER}:0001-x")
    assert got[1:3] == ("plan", "unmet")


def test_dangling_when_absent(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch)
    _stub_plans(mod, monkeypatch, {OTHER: []})
    got = _block(mod, f"plan:{OTHER}:0001-x")
    assert got[1:3] == ("plan", "dangling")


def test_abandoned_when_abandoned(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch)
    _stub_plans(mod, monkeypatch,
               {OTHER: [("plans/0001-x.md", {"id": "0001-x", "status": "abandoned"})]})
    got = _block(mod, f"plan:{OTHER}:0001-x")
    assert got[1:3] == ("plan", "abandoned")


def test_unreadable_on_a_5xx_listing(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch)
    _stub_plans(mod, monkeypatch, {OTHER: None})
    got = _block(mod, f"plan:{OTHER}:0001-x")
    assert got[1:3] == ("plan", "unreadable")


def test_unmet_when_repository_not_in_repos_conf(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch, repos=(HERE,))
    _stub_plans(mod, monkeypatch, {})
    got = _block(mod, f"plan:{OTHER}:0001-x")
    assert got[1:3] == ("plan", "unmet")


def test_bare_id_keeps_its_meaning(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch)
    plans_by_id = {"0001-x": {"id": "0001-x", "status": "done"}}
    assert mod._plan_dependency_block(HERE, ["0001-x"], plans_by_id, "t") is None


def test_blocks_returns_every_unmet_entry_block_returns_first(tmp_path, monkeypatch):
    mod = _watch(tmp_path, monkeypatch)
    _stub_plans(mod, monkeypatch, {OTHER: []})
    deps = [f"plan:{OTHER}:0001-x", "owner:does-not-exist.sh", f"plan:{OTHER}:0002-y"]
    monkeypatch.setattr(mod, "_owner_check", lambda name: ("unmet", "unmet", "nope"))
    blocks = mod._plan_dependency_blocks(HERE, deps, {}, "t")
    assert [b[0] for b in blocks] == deps
    assert blocks[0][1:3] == ("plan", "dangling")
    first = mod._plan_dependency_block(HERE, deps, {}, "t")
    assert first == blocks[0]


# --------------------------------------------------------------- fleet-plan

def _plan_mod(monkeypatch, known=(HERE, OTHER)):
    fp = tp._load()

    class W:
        conf_repos = staticmethod(lambda: ([HERE], sorted(known)))
    monkeypatch.setattr(fp, "_load_watch", lambda: W)
    return fp


def test_lint_this_repository_is_error(monkeypatch):
    fp = _plan_mod(monkeypatch)
    fm = {"repo": HERE, "depends_on": [f"plan:{HERE}:0001-x"]}
    errs = fp._lint_forge_deps("p.md", fm)
    assert len(errs) == 1 and "this repository" in errs[0]


def test_lint_unknown_repository_is_error(monkeypatch):
    fp = _plan_mod(monkeypatch, known=(HERE,))
    fm = {"repo": HERE, "depends_on": [f"plan:{OTHER}:0001-x"]}
    errs = fp._lint_forge_deps("p.md", fm)
    assert len(errs) == 1 and "config/repos.conf" in errs[0]


def test_lint_absent_plan_is_error(monkeypatch):
    fp = _plan_mod(monkeypatch)
    monkeypatch.setattr(fp, "_plan_on_main", lambda repo, pid: False)
    fm = {"repo": HERE, "depends_on": [f"plan:{OTHER}:0001-x"]}
    errs = fp._lint_forge_deps("p.md", fm)
    assert len(errs) == 1 and "0001-x" in errs[0]


def test_lint_unreadable_forge_is_not_an_error(monkeypatch):
    fp = _plan_mod(monkeypatch)
    monkeypatch.setattr(fp, "_plan_on_main", lambda repo, pid: None)
    fm = {"repo": HERE, "depends_on": [f"plan:{OTHER}:0001-x"]}
    assert fp._lint_forge_deps("p.md", fm) == []


def test_lint_malformed_is_error(monkeypatch):
    fp = _plan_mod(monkeypatch)
    fm = {"repo": HERE, "depends_on": ["plan:nonsense"]}
    assert fp._lint_forge_deps("p.md", fm)


def test_lint_met_plan_lints_clean(monkeypatch):
    fp = _plan_mod(monkeypatch)
    monkeypatch.setattr(fp, "_plan_on_main", lambda repo, pid: True)
    fm = {"repo": HERE, "depends_on": [f"plan:{OTHER}:0001-x"]}
    assert fp._lint_forge_deps("p.md", fm) == []


def test_plan_dep_is_not_a_graph_node(monkeypatch):
    fp = _plan_mod(monkeypatch)
    fm = {"repo": HERE, "depends_on": [f"plan:{OTHER}:0001-x"]}
    assert fp._plan_id_deps(fm) == []
