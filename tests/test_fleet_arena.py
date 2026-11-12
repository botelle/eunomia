"""Tests for bin/fleet-arena — one arena repository, many cells (plan 0062).

git is real here for the same reason it is in test_fleetlib_workspace and
test_fleet_candidate: the claims under test are about what --base checks
against a real origin/main and what git show reads at a real commit, and a
mock cannot fail those claims. The orchestrator is a recording stub (the same
idiom test_fleet_watch uses), so no test here spends a model or a network
call to bin/orchestrator itself.
"""
import importlib.machinery
import importlib.util
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

PLAN = """---
id: 0001-smoke
status: ready
repo: operator/scratch
zone: public
tier: 2
paths:
  - "x"
---
# smoke plan
"""


def _load(name, mod_name=None):
    mod_name = mod_name or name.replace("-", "_")
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(args, cwd):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _forge(tmp_path, plan_text=PLAN, plan_name="0001-smoke.md"):
    """A bare repo at tmp_path/forge/operator/scratch.git holding one plan file
    on main. Returns (ssh_base, base_sha, src_dir)."""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    plans = src / "plans"
    plans.mkdir()
    (plans / plan_name).write_text(plan_text)
    _git(["add", "."], src)
    _git(["commit", "-qm", "root"], src)
    base_sha = _git(["rev-parse", "HEAD"], src)
    bare = tmp_path / "forge" / "operator" / "scratch.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return str(tmp_path / "forge"), base_sha, src


_ARENA_ENV = ("EUNOMIA_FLEET_DIR", "FLEET_WORK_ROOT", "FLEET_GIT_SSH_BASE",
              "FLEET_DB", "EUNOMIA_SESSION", "FLEET_ORCHESTRATOR", "FLEET_SPAWN")


def _arena(tmp_path, monkeypatch, ssh_base):
    """A fresh bin/fleet-arena module, reading an isolated fleet dir/db and
    fetching from the scratch forge — never the real fleet or the real forge."""
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(exist_ok=True)
    for k in _ARENA_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(fleet_dir))
    monkeypatch.setenv("FLEET_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("FLEET_GIT_SSH_BASE", ssh_base)
    monkeypatch.setenv("FLEET_DB", str(fleet_dir / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_SESSION", "arena-test")
    # The stub orchestrator is a plain script: run it by Popen, not through a
    # launchd job this test host should not be asked to create.
    monkeypatch.setenv("FLEET_SPAWN", "popen")
    return _load("fleet-arena")


def _stub_orchestrator(tmp_path):
    """Records one line per spawn — argv[1:4] and the env fields a cell's
    dispatch must fill — and never runs a real session."""
    log = tmp_path / "spawns.log"
    s = tmp_path / "orch-stub.sh"
    s.write_text(
        '#!/bin/sh\n[ "$1" = "--ready" ] && exit 0\n'
        f'echo "$1|$2|$3|$EUNOMIA_SESSION|$EUNOMIA_IMPL_RUNNER|$EUNOMIA_PLAN_ID" '
        f'>> {log}\n')
    s.chmod(s.stat().st_mode | stat.S_IXUSR)
    return str(s), log


def _wait_spawns(log, n, timeout=5.0):
    """Spawns are DETACHED (Popen, never waited on) — wait for a known count
    rather than reading the log bare, the same reasoning test_fleet_watch's
    own _wait_spawns documents."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        cur = log.read_text().splitlines() if log.exists() else []
        if len(cur) >= n:
            return cur
        time.sleep(0.05)
    return log.read_text().splitlines() if log.exists() else []


def _counts(mod):
    con = mod.connect()
    try:
        cells = con.execute("SELECT COUNT(*) FROM arena_cell").fetchone()[0]
        rows = con.execute("SELECT COUNT(*) FROM arena_cell_assignment").fetchone()[0]
        return cells, rows
    finally:
        con.close()


def _cell_row(mod, cell_id):
    con = mod.connect()
    try:
        return con.execute(
            "SELECT repo, round, plan_sha, base_sha, branch, pr, lease, outcome "
            "FROM arena_cell WHERE cell_id=?", (cell_id,)).fetchone()
    finally:
        con.close()


def _assignment_rows(mod, cell_id):
    con = mod.connect()
    try:
        return {r[0]: r[1:] for r in con.execute(
            "SELECT position, ordinal, runner, source, effective FROM "
            "arena_cell_assignment WHERE cell_id=?", (cell_id,))}
    finally:
        con.close()


# ------------------------------------------------------------ refused before writes


def test_unknown_runner_is_refused_before_any_row_is_written(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    orch, _log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)
    before = _counts(mod)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=no-such-runner", "--round", "r1"])

    assert rc != 0
    assert _counts(mod) == before


def test_non_implementer_position_is_refused_before_any_row_is_written(tmp_path, monkeypatch):
    """The only resolve_repo call site in bin/orchestrator is
    resolved_implementer_env, with the literal 'implementer' — every other
    position is refused, even a value-valid one like code_reviewer=sub-opus."""
    ssh, base, _src = _forge(tmp_path)
    orch, _log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)
    before = _counts(mod)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "code_reviewer=sub-opus", "--round", "r1"])

    assert rc != 0
    assert _counts(mod) == before


def test_unwired_and_wrong_credential_runners_are_refused_before_any_row(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    orch, _log = _stub_orchestrator(tmp_path)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)
    for runner in ("sub-codex", "api-sonnet"):
        before = _counts(mod)
        rc = mod.main(["cell", "--repo", "operator/scratch", "--plan", "plans/0001-smoke.md",
                       "--base", base, "--assign", f"implementer={runner}"])
        assert rc != 0, runner
        assert _counts(mod) == before, runner


def test_unspawnable_verdict_tracks_orchestrator_resolve_credential(tmp_path, monkeypatch):
    """_refuse_unspawnable must agree with bin/orchestrator's resolve_credential
    for every runner and every initiator, so a change to the orchestrator's rule
    (plan 0116: local_cli -> oauth_token) cannot leave the arena refusing cells
    the orchestrator would run. Fails if the two disagree."""
    ssh, _base, _src = _forge(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    orch = mod.models._load_orchestrator()
    for initiator in (None, "operator", "nobody-knows"):
        if initiator is None:
            monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
        else:
            monkeypatch.setenv("EUNOMIA_INITIATOR", initiator)
        for name, r in orch.RUNNERS.items():
            try:
                have = orch.resolve_credential().kind
            except orch.Stop:
                have = None
            should_refuse = bool(r.unwired) or have is None or bool(r.needs and r.needs != have)
            try:
                mod._refuse_unspawnable(name)
                refused = False
            except mod.Refused:
                refused = True
            assert refused == should_refuse, (initiator, name, r.needs, have)
    # The break this guards: an unset initiator spends oauth_token, so a Claude
    # runner must not be refused for lacking local_cli.
    monkeypatch.delenv("EUNOMIA_INITIATOR", raising=False)
    mod._refuse_unspawnable("sub-sonnet")


def test_base_shape_and_mismatch_are_refused_before_any_row_is_written(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    orch, _log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)
    before = _counts(mod)

    for bad_base in ("main", base[:7], "0" * 40):
        rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                       "plans/0001-smoke.md", "--base", bad_base,
                       "--assign", "implementer=sub-sonnet", "--round", "r1"])
        assert rc != 0, f"--base {bad_base!r} was not refused"

    assert _counts(mod) == before, "a refused --base must write nothing"


# --------------------------------------------------------------- the record


def test_every_cell_gets_all_seven_positions_and_exactly_one_is_assigned(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-sonnet", "--round", "r1"])
    assert rc == 0
    _wait_spawns(log, 1)

    cells, rows = _counts(mod)
    assert cells == 1
    assert rows == 7

    con = mod.connect()
    cell_id = con.execute("SELECT cell_id FROM arena_cell").fetchone()[0]
    con.close()
    assignment = _assignment_rows(mod, cell_id)
    assert set(assignment) == {"implementer", "plan_reviewer", "code_reviewer",
                               "tester", "mediator", "impl_supervisor",
                               "test_supervisor"}
    assigned = [(p, v) for p, v in assignment.items() if v[2] == "assigned"]
    assert assigned == [("implementer", (1, "sub-sonnet", "assigned", 1))]
    effective = [p for p, v in assignment.items() if v[3] == 1]
    assert effective == ["implementer"]


def test_reviewer_positions_are_recorded_from_repo_model_not_pinned(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-opus", "--round", "r1"])
    assert rc == 0
    _wait_spawns(log, 1)

    con = mod.connect()
    cell_id = con.execute("SELECT cell_id FROM arena_cell").fetchone()[0]
    con.close()
    assignment = _assignment_rows(mod, cell_id)
    for position in ("plan_reviewer", "code_reviewer"):
        ordinal, runner, source, effective = assignment[position]
        assert source in ("repo_model", "default")
        assert effective == 0


def test_recorded_assignment_matches_what_the_orchestrator_launched_with(tmp_path, monkeypatch):
    """Ground truth is the STUB'S OWN LOG — what env the orchestrator process
    actually received — not a second read of the same table (plan 0062 D6)."""
    ssh, base, _src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-opus", "--round", "r1"])
    assert rc == 0
    lines = _wait_spawns(log, 1)
    assert len(lines) == 1
    _repo_arg, plan_arg, _lease_arg, _sid, launched_runner, launched_plan_id = \
        lines[0].split("|")
    assert launched_runner == "sub-opus"
    assert plan_arg == "plans/0001-smoke.md"
    assert launched_plan_id == "0001-smoke"

    con = mod.connect()
    cell_id = con.execute("SELECT cell_id FROM arena_cell").fetchone()[0]
    con.close()
    assignment = _assignment_rows(mod, cell_id)
    assert assignment["implementer"][1] == launched_runner


def test_plan_sha_is_a_content_hash_of_git_show_at_base(tmp_path, monkeypatch):
    """'not a file the operator handed over' — plan_sha must equal the hash
    of what `git show <base>:<plan path>` reads, independent of this file."""
    import hashlib
    ssh, base, src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-sonnet", "--round", "r1"])
    assert rc == 0
    _wait_spawns(log, 1)

    want = hashlib.sha256((src / "plans" / "0001-smoke.md").read_bytes()).hexdigest()
    con = mod.connect()
    got = con.execute("SELECT plan_sha, base_sha FROM arena_cell").fetchone()
    con.close()
    assert got == (want, base)


# ------------------------------------------------------------------ branches


def test_two_cells_on_one_plan_get_two_distinct_branches(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    for runner in ("sub-sonnet", "sub-opus"):
        rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                       "plans/0001-smoke.md", "--base", base,
                       "--assign", f"implementer={runner}", "--round", "r1"])
        assert rc == 0
    _wait_spawns(log, 2)

    con = mod.connect()
    branches = [r[0] for r in con.execute(
        "SELECT branch FROM arena_cell WHERE round='r1'")]
    con.close()
    assert len(branches) == 2
    assert len(set(branches)) == 2
    assert all(b.startswith("cell/0001-smoke/") for b in branches)


def test_two_concurrent_cells_both_reach_running_without_waiting_on_each_other(tmp_path, monkeypatch):
    """Started together, neither waits on the other's orchestrator to exit —
    D3's 'no lock per arena, cells do not serialise' — proven by driving the
    real CLI as two subprocesses against a stub orchestrator that never exits
    on its own (so a serialised implementation would hang this test)."""
    import os as _os

    ssh, base, _src = _forge(tmp_path)
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(exist_ok=True)
    log = tmp_path / "spawns.log"
    hang = tmp_path / "orch-hang.sh"
    hang.write_text('#!/bin/sh\n[ "$1" = "--ready" ] && exit 0\n'
                    f'echo "$1|$2|$3|$$" >> {log}\nsleep 300\n')
    hang.chmod(hang.stat().st_mode | stat.S_IXUSR)

    fleet_db = str(fleet_dir / "fleet.db")
    env = {
        "EUNOMIA_FLEET_DIR": str(fleet_dir),
        "FLEET_WORK_ROOT": str(tmp_path / "work"),
        "FLEET_GIT_SSH_BASE": ssh,
        "FLEET_DB": fleet_db,
        "EUNOMIA_SESSION": "arena-test",
        "FLEET_ORCHESTRATOR": str(hang),
        "FLEET_SPAWN": "popen",
        "PATH": _os.environ.get("PATH", ""),
        "HOME": _os.environ.get("HOME", ""),
    }

    def _spawn(round_name, runner):
        return subprocess.Popen(
            [sys.executable, str(BIN / "fleet-arena"), "cell",
             "--repo", "operator/scratch", "--plan", "plans/0001-smoke.md",
             "--base", base, "--assign", f"implementer={runner}",
             "--round", round_name],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    p1 = _spawn("concurrent", "sub-sonnet")
    p2 = _spawn("concurrent", "sub-opus")
    rc1 = p1.wait(timeout=30)
    rc2 = p2.wait(timeout=30)
    assert (rc1, rc2) == (0, 0), (p1.communicate(), p2.communicate())

    spawns = _wait_spawns(log, 2)
    try:
        con = sqlite3.connect(fleet_db)
        try:
            rows = con.execute(
                "SELECT branch, outcome FROM arena_cell WHERE round='concurrent'").fetchall()
        finally:
            con.close()
    finally:
        # the stubs sleep 300s and are detached; do not leave them behind
        for line in spawns:
            try:
                _os.kill(int(line.rsplit("|", 1)[1]), 15)
            except (ValueError, ProcessLookupError):
                pass
    assert len(rows) == 2
    assert len({branch for branch, _outcome in rows}) == 2
    assert all(outcome == "running" for _branch, outcome in rows)


# --------------------------------------------------------------- failure path


def test_a_dispatch_failure_is_recorded_failed_with_assignment_intact(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", str(tmp_path / "does-not-exist.sh"))

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-sonnet", "--round", "r1"])
    assert rc != 0

    con = mod.connect()
    cell_id, outcome = con.execute(
        "SELECT cell_id, outcome FROM arena_cell").fetchone()
    n_assign = con.execute(
        "SELECT COUNT(*) FROM arena_cell_assignment WHERE cell_id=?",
        (cell_id,)).fetchone()[0]
    con.close()
    assert outcome == "failed"
    assert n_assign == 7, "the row is marked failed, never deleted"


def test_a_lease_mint_failure_is_also_recorded_failed_not_deleted(tmp_path, monkeypatch):
    """A lease-mint failure happens AFTER the row already exists (D3): the
    row and its seven assignment rows must be marked failed, never deleted —
    distinct from the orchestrator-spawn failure path tested above."""
    ssh, base, _src = _forge(tmp_path)
    orch, _log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    def _boom(*_a, **_k):
        raise mod.DispatchFailed("simulated lease assign failure")
    monkeypatch.setattr(mod, "mint_lease", _boom)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-sonnet", "--round", "clash"])
    assert rc != 0

    con = mod.connect()
    cell_id, outcome, lease = con.execute(
        "SELECT cell_id, outcome, lease FROM arena_cell WHERE round='clash'").fetchone()
    n_assign = con.execute(
        "SELECT COUNT(*) FROM arena_cell_assignment WHERE cell_id=?",
        (cell_id,)).fetchone()[0]
    con.close()
    assert outcome == "failed"
    assert lease is None
    assert n_assign == 7


# ------------------------------------------------------------- repo_model tier


def test_non_effective_positions_surface_repo_model_as_informational(tmp_path, monkeypatch):
    ssh, base, _src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    fm = _load("fleet-models")
    con = fm.connect()
    fm.set_runner(con, "operator/scratch", "tester", "sub-opus", actor="cli:t")
    fm.set_runner(con, "default", "mediator", "sub-sonnet", actor="cli:t")
    con.close()

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-sonnet", "--round", "r1"])
    assert rc == 0
    _wait_spawns(log, 1)

    con = mod.connect()
    cell_id = con.execute("SELECT cell_id FROM arena_cell").fetchone()[0]
    con.close()
    assignment = _assignment_rows(mod, cell_id)
    assert assignment["tester"] == (1, "sub-opus", "repo_model", 0)
    assert assignment["mediator"] == (1, "sub-sonnet", "default", 0)
    assert assignment["impl_supervisor"] == (1, "none", "default", 0)


# --------------------------------------------------------------- list / show


def test_list_filters_by_repo_and_round(tmp_path, monkeypatch, capsys):
    ssh, base, _src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    for round_name in ("a", "b"):
        rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                       "plans/0001-smoke.md", "--base", base,
                       "--assign", "implementer=sub-sonnet", "--round", round_name])
        assert rc == 0
    _wait_spawns(log, 2)
    capsys.readouterr()

    rc = mod.main(["list", "--repo", "operator/scratch", "--round", "a"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.strip()]
    assert len(lines) == 1
    fields = lines[0].split()
    assert "a" in fields
    assert "b" not in fields


def test_show_reproduces_all_seven_positions(tmp_path, monkeypatch, capsys):
    ssh, base, _src = _forge(tmp_path)
    orch, log = _stub_orchestrator(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    monkeypatch.setenv("FLEET_ORCHESTRATOR", orch)

    rc = mod.main(["cell", "--repo", "operator/scratch", "--plan",
                   "plans/0001-smoke.md", "--base", base,
                   "--assign", "implementer=sub-sonnet", "--round", "r1"])
    assert rc == 0
    _wait_spawns(log, 1)
    capsys.readouterr()

    con = mod.connect()
    cell_id = con.execute("SELECT cell_id FROM arena_cell").fetchone()[0]
    con.close()

    rc = mod.main(["show", cell_id])
    assert rc == 0
    out = capsys.readouterr().out
    for position in ("implementer", "plan_reviewer", "code_reviewer", "tester",
                     "mediator", "impl_supervisor", "test_supervisor"):
        assert position in out


def test_show_of_unknown_cell_fails(tmp_path, monkeypatch, capsys):
    ssh, _base, _src = _forge(tmp_path)
    mod = _arena(tmp_path, monkeypatch, ssh)
    rc = mod.main(["show", "01ARZ3NDEKTSV4RRFFQ69G5FAV"])
    assert rc != 0


def test_arena_keeps_its_own_seven_positions(tmp_path, monkeypatch):
    """`style_reviewer` joined fleet-models' POSITIONS (plan 0150); the arena table's
    CHECK was fixed when cells were written, so arena must not follow it."""
    mod = _arena(tmp_path, monkeypatch, "ssh://unused")
    assert mod.ARENA_POSITIONS == ("implementer", "plan_reviewer", "code_reviewer",
                                   "tester", "mediator", "impl_supervisor",
                                   "test_supervisor")
    assert "style_reviewer" in mod.models.POSITIONS
    assert "style_reviewer" not in mod.ARENA_POSITIONS
