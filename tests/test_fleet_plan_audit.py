"""Tests for `fleet-plan audit` (plan 0038).

docs/plan-triage.md records three probes that all produced false "built"s:
path existence alone, grepping for the feature name, and a `Plan: <id>`
marker commit. `audit()` never grades "built" at all — the fixtures below
pin the three shapes that fooled each probe, and assert the tool lands on
one of its four honest states instead.

Each test builds a tiny throwaway git repo (`_repo`/`_commit`) rather than
reusing this actual checkout, so the filing-commit and tree-timing logic can
be pinned exactly, independent of whatever plans/ happens to hold today.
"""
import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
ALLOWED_STATES = {"not_built", "undecidable", "touched_after_filing", "error"}


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_plan", str(BIN / "fleet-plan"))
    spec = importlib.util.spec_from_loader("fleet_plan", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def _repo(tmp_path, name="repo"):
    """A fresh git repo whose default branch is `main`, regardless of the
    host's `init.defaultBranch` — set via `symbolic-ref` before the first
    commit rather than `branch -M` after, so it needs no existing commit."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _commit(repo, message, files):
    for relpath, content in files.items():
        p = repo / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _plan(pid, status, paths, boundaries="- don't X, because Y"):
    paths_yaml = "[" + ", ".join(f'"{p}"' for p in paths) + "]"
    return f"""---
id: {pid}
status: {status}
repo: operator/eunomia
zone: public
tier: 2
paths: {paths_yaml}
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
- r
"""


def _by_id(plans, pid):
    return next(p for p in plans if p["id"] == pid)


# ------------------------------------------------------------- the four states

def test_undecidable_when_every_path_predates_filing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "add tool", {"bin/tool.py": "x = 1\n"})
    _commit(repo, "plans: 0001-x", {
        "plans/0001-x.md": _plan("0001-x", "draft", ["bin/tool.py", "plans/0001-x.md"])})
    mod = _load()
    result = mod.audit([repo / "plans" / "0001-x.md"], repo=repo, ref="main")
    assert result["refused"] is None
    p = _by_id(result["plans"], "0001-x")
    assert p["state"] == "undecidable"
    assert p["pre_existing"] == ["bin/tool.py"]
    assert p["missing"] == [] and p["touched_after"] == []


def test_not_built_when_a_declared_path_is_missing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "plans: 0002-y", {
        "plans/0002-y.md": _plan("0002-y", "draft",
                                 ["bin/missing.py", "plans/0002-y.md"])})
    mod = _load()
    result = mod.audit([repo / "plans" / "0002-y.md"], repo=repo, ref="main")
    p = _by_id(result["plans"], "0002-y")
    assert p["state"] == "not_built"
    assert p["missing"] == ["bin/missing.py"]


def test_touched_after_filing_when_a_path_lands_later(tmp_path):
    """The 0013/0032 shape: some declared paths pre-date filing, one lands
    after — not built (it may be unrelated), not undecidable either."""
    repo = _repo(tmp_path)
    _commit(repo, "add existing", {"bin/existing.py": "x = 1\n"})
    _commit(repo, "plans: 0003-z", {
        "plans/0003-z.md": _plan("0003-z", "draft",
                                 ["bin/existing.py", "bin/new.py", "plans/0003-z.md"])})
    _commit(repo, "add new", {"bin/new.py": "y = 2\n"})
    mod = _load()
    result = mod.audit([repo / "plans" / "0003-z.md"], repo=repo, ref="main")
    p = _by_id(result["plans"], "0003-z")
    assert p["state"] == "touched_after_filing"
    assert p["touched_after"] == ["bin/new.py"]
    assert p["pre_existing"] == ["bin/existing.py"]
    assert "built" not in p["state"]


def test_glob_path_with_some_matches_after_filing_is_touched_after(tmp_path):
    """2556 M3: paths are globs, matched against the tree. 0032 declares
    `tests/test_orchestrator*.py`; one match pre-dates filing, others land
    after — the entry as a whole must read as touched-after, not undecidable."""
    repo = _repo(tmp_path)
    _commit(repo, "add test", {"tests/test_orchestrator.py": "pass\n"})
    _commit(repo, "plans: 0004-w", {
        "plans/0004-w.md": _plan("0004-w", "draft",
                                 ["tests/test_orchestrator*.py", "plans/0004-w.md"])})
    _commit(repo, "add freeze test", {"tests/test_orchestrator_freeze.py": "pass\n"})
    mod = _load()
    result = mod.audit([repo / "plans" / "0004-w.md"], repo=repo, ref="main")
    p = _by_id(result["plans"], "0004-w")
    assert p["state"] == "touched_after_filing"
    assert p["touched_after"] == ["tests/test_orchestrator*.py"]


# --------------------------------------------------------------------- scope

def test_done_and_abandoned_plans_are_never_graded(tmp_path):
    """§3 Boundaries: scope is `draft` on main. A `done`/`abandoned` plan must
    not appear in the report at all, however its paths look on disk."""
    repo = _repo(tmp_path)
    _commit(repo, "plans", {
        "plans/0005-done.md": _plan("0005-done", "done", ["bin/missing.py"]),
        "plans/0006-abandoned.md": _plan("0006-abandoned", "abandoned",
                                         ["bin/also-missing.py"]),
    })
    mod = _load()
    files = [repo / "plans" / "0005-done.md", repo / "plans" / "0006-abandoned.md"]
    result = mod.audit(files, repo=repo, ref="main")
    assert result["plans"] == []


def test_own_plan_file_excluded_from_grading(tmp_path):
    """2556 M2: the plan's own file is created BY the filing commit, so
    counting it as a declared path proves nothing about the plan and must
    never contribute to any bucket."""
    repo = _repo(tmp_path)
    _commit(repo, "plans: 0006-only-self", {
        "plans/0006-only-self.md": _plan("0006-only-self", "draft",
                                         ["plans/0006-only-self.md"])})
    mod = _load()
    result = mod.audit([repo / "plans" / "0006-only-self.md"], repo=repo, ref="main")
    p = _by_id(result["plans"], "0006-only-self")
    assert p["missing"] == [] and p["pre_existing"] == [] and p["touched_after"] == []
    assert p["state"] == "undecidable"


# ------------------------------------------------------------------ refusals

def test_shallow_clone_refuses_rather_than_reporting_zero(tmp_path):
    """2556 M4: a shallow clone must refuse outright, not report the most
    favourable possible reading (zero pre-existing paths) from missing data."""
    src = _repo(tmp_path, "src")
    _commit(src, "add tool", {"bin/tool.py": "x = 1\n"})
    _commit(src, "plans: 0007-x", {
        "plans/0007-x.md": _plan("0007-x", "draft", ["bin/tool.py", "plans/0007-x.md"])})

    shallow = tmp_path / "shallow"
    # `--depth` is silently ignored on a same-host local-path clone ("use
    # file:// instead") — a plain path here would make this test pass
    # against a full clone and prove nothing about the refusal.
    subprocess.run(["git", "clone", "--depth", "1", "--branch", "main",
                    f"file://{src}", str(shallow)], check=True, capture_output=True)
    mod = _load()
    result = mod.audit([shallow / "plans" / "0007-x.md"], repo=shallow, ref="main")
    assert result["refused"]
    assert "plans" not in result


def test_unparsable_plan_is_reported_as_error_not_dropped(tmp_path):
    """'Absent must not look like zero': a plan this tool cannot even read
    must show up AS an error, not vanish from the report."""
    repo = _repo(tmp_path)
    _commit(repo, "plans: 0008-x", {
        "plans/0008-x.md": _plan("0008-x", "draft", ["bin/tool.py", "plans/0008-x.md"])})
    broken = tmp_path / "broken.md"
    broken.write_text("not front matter at all\n")
    mod = _load()
    result = mod.audit([repo / "plans" / "0008-x.md", broken], repo=repo, ref="main")
    errored = [p for p in result["plans"] if p["file"] == "broken.md"]
    assert len(errored) == 1
    assert errored[0]["state"] == "error"


# ------------------------------------------------------------ the three probes

def test_probe_2_regression_a_name_match_in_an_unrelated_file_is_never_built(tmp_path):
    """docs/plan-triage.md probe 2: `bin/fleet-watch` contains `def repos(`
    and 0013 was graded built on that basis, though the function never reads
    the plan's own config file. `audit()` never inspects file CONTENTS at
    all, so this can't recur — pin that the state stays in the honest set."""
    repo = _repo(tmp_path)
    _commit(repo, "add watch", {"bin/fleet-watch": "def repos():\n    return []\n"})
    _commit(repo, "plans: 0009-repo-enable", {
        "plans/0009-repo-enable.md": _plan(
            "0009-repo-enable", "draft", ["bin/fleet-watch", "plans/0009-repo-enable.md"])})
    mod = _load()
    result = mod.audit([repo / "plans" / "0009-repo-enable.md"], repo=repo, ref="main")
    p = _by_id(result["plans"], "0009-repo-enable")
    assert p["state"] in ALLOWED_STATES
    assert p["state"] == "undecidable"  # pre-existed filing; not evidence either way


def test_probe_3_regression_the_filing_commit_message_is_never_read_as_proof(tmp_path):
    """docs/plan-triage.md probe 3: a `Plan: <id>` marker always hits the
    plan's own filing commit, which quotes its id in the message trivially.
    `audit()` never parses commit messages at all — a filing commit whose
    message names the plan's id must not change the verdict; only path
    timing does. Here the declared path lands strictly after filing, so the
    correct verdict is touched-after, regardless of what the message says."""
    repo = _repo(tmp_path)
    _commit(repo, "plans: 0010-thing (Plan: 0010-thing)", {
        "plans/0010-thing.md": _plan("0010-thing", "draft",
                                     ["bin/thing.py", "plans/0010-thing.md"])})
    _commit(repo, "add thing", {"bin/thing.py": "x = 1\n"})
    mod = _load()
    result = mod.audit([repo / "plans" / "0010-thing.md"], repo=repo, ref="main")
    p = _by_id(result["plans"], "0010-thing")
    assert p["state"] == "touched_after_filing"
    assert "Plan:" not in p["detail"]


# ------------------------------------------------------------------- hygiene

def test_running_audit_twice_changes_nothing_on_disk(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "add tool", {"bin/tool.py": "x = 1\n"})
    _commit(repo, "plans: 0011-x", {
        "plans/0011-x.md": _plan("0011-x", "draft", ["bin/tool.py", "plans/0011-x.md"])})
    mod = _load()
    files = [repo / "plans" / "0011-x.md"]
    before = _git(repo, "status", "--porcelain").stdout
    r1 = mod.audit(files, repo=repo, ref="main")
    mid = _git(repo, "status", "--porcelain").stdout
    r2 = mod.audit(files, repo=repo, ref="main")
    after = _git(repo, "status", "--porcelain").stdout
    assert before == mid == after == ""
    assert r1 == r2


def test_no_state_is_ever_named_built(tmp_path):
    """§3 Boundaries: no confidence score, and no state means "built" —
    that word must never appear as a reported state, only ever in prose
    explaining why the tool refuses to use it."""
    repo = _repo(tmp_path)
    _commit(repo, "add tool", {"bin/tool.py": "x = 1\n"})
    _commit(repo, "plans: 0012-x", {
        "plans/0012-x.md": _plan("0012-x", "draft", ["bin/tool.py", "plans/0012-x.md"])})
    mod = _load()
    result = mod.audit([repo / "plans" / "0012-x.md"], repo=repo, ref="main")
    assert all(p["state"] in ALLOWED_STATES for p in result["plans"])


# --------------------------------------------------------- the shared reader

def test_paths_block_style_is_parsed_as_a_list(tmp_path):
    """0036/0038 declare `paths` block-style; a reader that understands only
    the inline `[a, b]` shape reads this as declaring no paths at all."""
    p = tmp_path / "0013-block.md"
    p.write_text("""---
id: 0013-block
status: draft
repo: operator/eunomia
zone: public
tier: 2
paths:
  - bin/a
  - bin/b
---

## 1. Goal
g
""")
    mod = _load()
    fm, _body, _errs = mod.parse(p)
    assert fm["paths"] == ["bin/a", "bin/b"]


def test_paths_absent_entirely_still_fails_required_fm(tmp_path):
    """A plan with no `paths:` line at all must still trip REQUIRED_FM in
    lint() — parse_list_field returning None (not `[]`) is what preserves
    that, distinct from a declared-but-empty `paths: []`."""
    p = tmp_path / "0014-nopaths.md"
    p.write_text("""---
id: 0014-nopaths
status: draft
repo: operator/eunomia
zone: public
tier: 2
---

## 1. Goal
g
""")
    mod = _load()
    fm, _body, _errs = mod.parse(p)
    assert "paths" not in fm
