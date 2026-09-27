"""Tests for fleetlib's workspace helpers — ADR-0001.

git is real here for the same reason it is in test_fleet_candidate: the claim
under test is that two concurrent runs get two trees, and a mock cannot fail
that claim."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(fleet_dir, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(fleet_dir)
    for k in ("FLEET_WORK_ROOT", "FLEET_GIT_SSH_BASE"):
        os.environ.pop(k, None)
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("fleetlib", str(BIN / "fleetlib.py"))
    spec = importlib.util.spec_from_loader("fleetlib", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git(args, cwd):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _forge(tmp_path, files=None):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    for name, body in (files or {"README.md": "hi\n"}).items():
        f = src / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    _git(["add", "."], src)
    _git(["commit", "-qm", "root"], src)
    bare = tmp_path / "forge" / "operator" / "demo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return str(tmp_path / "forge")


def _lease(fleet_dir, lease_id, state="active"):
    d = fleet_dir / "leases"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{lease_id}.json").write_text(json.dumps({"id": lease_id, "state": state}))


def test_two_leases_in_one_repo_get_two_trees(tmp_path):
    """The failure ADR-0001 exists for: two plans, one repo, one directory."""
    ssh = _forge(tmp_path)
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"),
                FLEET_GIT_SSH_BASE=ssh)
    a = lib.add_worktree("operator/demo", "branch--feat-a--001", "origin/main")
    b = lib.add_worktree("operator/demo", "branch--feat-b--002", "origin/main")
    assert a != b
    assert a.is_dir() and b.is_dir()
    (a / "scratch.txt").write_text("a")
    assert not (b / "scratch.txt").exists()
    # one history, two trees
    repo_dir = tmp_path / "work" / lib.repo_slug("operator/demo")
    assert len(list(repo_dir.glob("branch--*"))) == 2


def test_remove_worktree_leaves_the_store(tmp_path):
    ssh = _forge(tmp_path)
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"),
                FLEET_GIT_SSH_BASE=ssh)
    wt = lib.add_worktree("operator/demo", "branch--feat-a--001", "origin/main")
    lib.remove_worktree("operator/demo", "branch--feat-a--001")
    assert not wt.exists()
    assert lib.store_path("operator/demo").exists()


def test_a_stale_tree_is_replaced_not_reused(tmp_path):
    ssh = _forge(tmp_path)
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"),
                FLEET_GIT_SSH_BASE=ssh)
    wt = lib.add_worktree("operator/demo", "branch--feat-a--001", "origin/main")
    (wt / "half-written.txt").write_text("from a dead run")
    again = lib.add_worktree("operator/demo", "branch--feat-a--001", "origin/main")
    assert again == wt
    assert not (wt / "half-written.txt").exists()


def test_orphans_are_reported_and_not_deleted(tmp_path):
    ssh = _forge(tmp_path)
    fleet = tmp_path / "fleet"
    lib = _load(fleet, FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh)
    live, dead = "branch--live--001", "branch--dead--002"
    lib.add_worktree("operator/demo", live, "origin/main")
    wt_dead = lib.add_worktree("operator/demo", dead, "origin/main")
    lib.add_worktree("operator/demo", "cand-7-abc1234", "origin/main")
    _lease(fleet, live, "active")            # dead has no lease at all

    orphans = lib.orphan_worktrees()

    assert [o[1] for o in orphans] == [dead]
    assert orphans[0][0] == "operator/demo"
    assert wt_dead.is_dir(), "the sweep reports; it must not delete the evidence"


def test_a_released_lease_is_an_orphan_too(tmp_path):
    ssh = _forge(tmp_path)
    fleet = tmp_path / "fleet"
    lib = _load(fleet, FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh)
    lib.add_worktree("operator/demo", "branch--done--003", "origin/main")
    _lease(fleet, "branch--done--003", "released")
    assert [o[1] for o in lib.orphan_worktrees()] == ["branch--done--003"]


def test_keys_and_repos_are_validated_before_they_become_paths(tmp_path):
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"))
    for bad in ("../escape", "a/b", "", ".hidden"):
        try:
            lib.worktree_path("operator/demo", bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted as a workspace key")
    for bad in ("operator", "a/b/c", "../x/y", ""):
        try:
            lib.repo_slug(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{bad!r} was accepted as a repo")


def test_the_workspace_helper_cannot_push(tmp_path):
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"))
    try:
        lib._git(["push", "origin", "main"], cwd=tmp_path)
    except AssertionError as e:
        assert "never push" in str(e)
    else:
        raise AssertionError("the push guard did not fire")


def test_repo_slugs_do_not_collide_across_underscore_shapes(tmp_path):
    """_REPO_COMP_RE admits '_', so joining on '__' is not injective:
    'foo__bar/baz' and 'foo/bar__baz' would share one clone, one .fetch.lock
    and one work root — and the sweep would attribute one repo's work to the
    other. fleetlib already had an injective encoder for this exact reason."""
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"))
    a, b = "foo__bar/baz", "foo/bar__baz"
    assert lib.repo_slug(a) != lib.repo_slug(b)
    assert lib.store_path(a) != lib.store_path(b)
    assert lib.worktree_path(a, "branch--x--001") != lib.worktree_path(b, "branch--x--001")


def test_the_sweep_reports_the_repo_it_recorded_not_a_decoded_guess(tmp_path):
    ssh = _forge(tmp_path)
    fleet = tmp_path / "fleet"
    lib = _load(fleet, FLEET_WORK_ROOT=str(tmp_path / "work"), FLEET_GIT_SSH_BASE=ssh)
    lib.add_worktree("operator/demo", "branch--dead--002", "origin/main")
    orphans = lib.orphan_worktrees()
    assert [o[0] for o in orphans] == ["operator/demo"]
    assert (tmp_path / "work" / lib.repo_slug("operator/demo") / ".repo").read_text().strip() == "operator/demo"


# ---------------------------------------------------------------- lease ttl
def test_an_active_lease_with_a_silent_heartbeat_orphans_in_minutes(tmp_path):
    """The orphan corpus only ever exercised the `assigned` branch, because its
    helper backdates `created` on a lease that was never activated. Once
    something calls --activate, is_orphaned takes the `active` branch instead,
    where the threshold is ttl_minutes of heartbeat silence — 240 by default."""
    from datetime import datetime, timedelta, timezone
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"))
    sid = "orch-test-1"
    (tmp_path / "fleet" / "sessions" / sid).mkdir(parents=True)
    hb = tmp_path / "fleet" / "sessions" / sid / "hb"
    hb.touch()
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).timestamp()
    os.utime(hb, (stale, stale))

    rec = {"id": "branch--x--001", "state": "active", "holder": sid,
           "ttl_minutes": lib.lease_ttl_minutes()}
    assert lib.is_orphaned(rec), (
        f"30 minutes of silence not orphaned at ttl={rec['ttl_minutes']}min")
    assert rec["ttl_minutes"] <= 10, "ttl is not sized against the heartbeat"


def test_a_malformed_heartbeat_knob_falls_back_instead_of_raising(tmp_path):
    """This is parsed after the lease is activated; raising there strands a
    live lease with no heartbeat behind it."""
    lib = _load(tmp_path / "fleet", FLEET_WORK_ROOT=str(tmp_path / "work"))
    for bad in ("", "abc", "0", "-5", "99999"):
        os.environ["FLEET_HEARTBEAT_SECS"] = bad
        assert lib.heartbeat_secs() == 60, f"{bad!r} did not fall back"
        assert lib.lease_ttl_minutes() >= 2
    os.environ["FLEET_HEARTBEAT_SECS"] = "120"
    assert lib.heartbeat_secs() == 120
    os.environ.pop("FLEET_HEARTBEAT_SECS")
