"""fleet-ci-stress — plan 0128: plan 0113's D3 rounds, run from a script.

Runs the real program against `tests/fakes/forge.py`, extended here (that file
is outside this plan's lane) with the four things a round touches: the branch
list, the commit list, the actions tasks/runs lists, and branch deletion. The
git remote is a real bare repository, so a push and a deletion are real: the
assertions about "no branch left behind" read the bare repo's refs, not a
counter the program could keep wrong.
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes.forge import FakeForge  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROG = ROOT / "bin" / "fleet-ci-stress"
REPO = "operator/eunomia"
TOKEN = "s3cr3t-token-value-9f2c"
T0 = 1_791_000_000                      # an arbitrary fixed instant


def _iso(offset):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 + offset))


def _load():
    ldr = importlib.machinery.SourceFileLoader("fleet_ci_stress", str(PROG))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("fleet_ci_stress", ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fcs():
    return _load()


def _job(branch, wf, name, start, end, result="success", sha="", run=1, id_=0):
    return {"id": id_, "name": name, "head_branch": branch, "head_sha": sha,
            "run_number": run, "workflow_id": wf, "status": result,
            "run_started_at": _iso(start), "updated_at": _iso(end)}


# ------------------------------------------------------------------ the fake
class StressForge(FakeForge):
    """FakeForge plus branches, commits, tasks, runs and branch deletion.

    CI is simulated: every `ci-stress/*` ref present in the bare repo gets the
    jobs `plan(index)` returns, the first time it is seen, so a branch the
    program never pushed has no CI and a branch it deleted keeps its history
    (a task row outlives its branch)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.plan = None                 # fn(branch_index) -> [(wf, name, start, end, result)]
        self.hold = False                # jobs read back as "running"
        self.appear_after = 0            # CI is slow to start: this many task/run
                                         # listings pass, once a branch exists,
                                         # before its jobs show up
        self.static_tasks = []           # tasks that exist from the start
        self.fail_runs = False           # /actions/runs answers 500
        self.no_run_ids = False          # /actions/runs rows omit `id`
        self.on_branches = None          # fn(call_count) on each branch listing
        self.branch_calls = 0
        self.sim = {}                    # branch -> [task dict]
        self.repo = next(iter(self.repos))

    def refs(self):
        rc, out = self._git(self.repo, "for-each-ref", "--format=%(refname:short) %(objectname)",
                            "refs/heads/")
        return dict(l.split(" ", 1) for l in out.splitlines())

    def stress_refs(self):
        return sorted(b for b in self.refs() if b.startswith("ci-stress/"))

    def _simulate(self):
        for b, sha in sorted(self.refs().items()):
            if not b.startswith("ci-stress/") or b in self.sim or self.plan is None:
                continue
            idx = "abc".index(b[-1])
            self.sim[b] = [
                _job(b, wf, name, s, e, res, sha,
                     run=100 + idx * 2 + (wf == "plans.yml"),
                     id_=self._next_id())
                for wf, name, s, e, res in self.plan(idx)]

    def all_tasks(self):
        if self.appear_after > 0 and self.stress_refs():
            self.appear_after -= 1
        else:
            self._simulate()
        rows = [t for ts in self.sim.values() for t in ts]
        if self.hold:                    # decided at read time, so a test can release it
            rows = [{**t, "status": "running"} for t in rows]
        rows += self.static_tasks
        return sorted(rows, key=lambda t: -t["id"])

    def _route(self, method, path, query, body, login):
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/branches", path)
        if m and method == "GET":
            self.branch_calls += 1
            if self.on_branches:
                self.on_branches(self.branch_calls)
            return 200, self._page([{"name": b} for b in sorted(self.refs())], query)
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/branches/(?P<b>.+)", path)
        if m and method == "DELETE":
            if m["b"] not in self.refs():
                return 404, None
            self._git(self.repo, "update-ref", "-d", f"refs/heads/{m['b']}")
            return 204, None
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/commits", path)
        if m and method == "GET":
            ref = (query.get("sha") or ["main"])[0]
            rc, out = self._git(self.repo, "rev-list", f"--max-count={(query.get('limit') or ['10'])[0]}",
                                f"refs/heads/{ref}")
            return (200, [{"sha": s} for s in out.split()]) if rc == 0 else (404, None)
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/actions/tasks", path)
        if m and method == "GET":
            rows = self.all_tasks()
            return 200, {"total_count": len(rows),
                         "workflow_runs": self._page(rows, query)}
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/actions/runs", path)
        if m and method == "GET" and self.fail_runs:
            return 500, None
        if m and method == "GET":
            runs = {}
            for t in self.all_tasks():
                r = runs.setdefault((t["run_number"], t["head_branch"]), {
                    # The real /actions/runs row carries `id` (what
                    # GET /actions/runs/<id> takes); this fake's did not. The
                    # run_number offset makes it unique per run, as the forge's is.
                    "id": 9000 + t["run_number"],
                    "run_number": t["run_number"], "prettyref": t["head_branch"],
                    "commit_sha": t["head_sha"], "path": t["workflow_id"] + "@refs/heads/x",
                    "status": t["status"]})
                if t["status"] not in ("success", "failure"):
                    r["status"] = t["status"]
                elif r["status"] == "success":
                    r["status"] = t["status"]
            rows = list(runs.values())
            if self.no_run_ids:
                rows = [{k: v for k, v in r.items() if k != "id"} for r in rows]
            return 200, {"total_count": len(rows), "workflow_runs": self._page(rows, query)}
        return super()._route(method, path, query, body, login)


def _good_plan(idx):
    """Three branches whose full-suite jobs run side by side: every pair overlaps."""
    s = idx * 90
    return [("ci.yml", "tests (cihost)", s, s + 600, "success"),
            ("ci.yml", "tests (cihost-linux)", s + 5, s + 590, "success"),
            ("ci.yml", "talos-contract", s, s + 30, "success"),
            ("plans.yml", "lint", s + 10, s + 500, "success")]


def _serial_plan(idx):
    """Each branch finishes before the next starts: no cross-branch overlap."""
    s = idx * 1000
    return [("ci.yml", "tests (cihost)", s, s + 300, "success"),
            ("ci.yml", "tests (cihost-linux)", s, s + 300, "success"),
            ("plans.yml", "lint", s, s + 100, "success")]


# ---------------------------------------------------------------- the world
class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bare = tmp_path / "forge.git"
        self.work = tmp_path / "work"
        self.out = tmp_path / "evidence"
        g = lambda *a, cwd=None: subprocess.run(
            ["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout.strip()
        g("init", "--bare", "-b", "main", str(self.bare))
        g("clone", str(self.bare), str(self.work))
        (self.work / "f").write_text("1")
        g("add", "f", cwd=self.work)
        g("commit", "-m", "base", cwd=self.work)
        (self.work / "f").write_text("2")
        g("commit", "-am", "scratch", cwd=self.work)
        g("push", "origin", "HEAD:refs/heads/main", cwd=self.work)
        self.sha = g("rev-parse", "HEAD", cwd=self.work)
        self.base = g("rev-parse", "HEAD^", cwd=self.work)
        self.tree = g("rev-parse", "HEAD^{tree}", cwd=self.work)
        self.forge = StressForge({REPO: str(self.bare)}, {TOKEN: "implbot"}).start()
        self.forge.plan = _good_plan

    def env(self):
        return {**os.environ, "FLEET_FORGEJO_URL": self.forge.url,
                "FLEET_CI_STRESS_TOKEN_CMD": f"echo {TOKEN}"}

    def argv(self, *args):
        return [sys.executable, str(PROG), *args]

    def round_args(self, n=1, kind="iso", *extra):
        return self.argv("round", "--kind", kind, "--tree", self.sha, "--n", str(n),
                         "--poll", "0.05", "--repo-dir", str(self.work),
                         "--out-dir", str(self.out), *extra)

    def run(self, args, timeout=60):
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              env=self.env())

    def record(self, kind="iso", n=1):
        return json.loads((self.out / f"round-{kind}-{n}.json").read_text())


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.forge.stop()


# --------------------------------------------------------- the overlap rule
def _t(branch, name, start, end, wf="ci.yml", id_=1):
    return {"id": id_, "branch": branch, "name": name, "workflow": wf,
            "run_started_at": _iso(start), "updated_at": _iso(end)}


def test_a_same_run_matrix_pair_never_counts(fcs):
    tasks = [_t("r1-a", "tests (cihost)", 0, 600, id_=1),
             _t("r1-a", "tests (cihost-linux)", 0, 600, id_=2),
             _t("r1-b", "tests (cihost)", 2000, 2600, id_=3)]
    assert fcs.find_overlap(tasks) is None


def test_a_one_sided_pair_never_counts(fcs):
    # a.start+60 < b.updated holds; b.start+60 < a.updated does not.
    tasks = [_t("r1-a", "tests (cihost)", 0, 100, id_=1),
             _t("r1-b", "tests (cihost)", 80, 300, id_=2)]
    assert fcs.find_overlap(tasks) is None


def test_a_cross_branch_two_sided_pair_counts(fcs):
    tasks = [_t("r1-a", "tests (cihost)", 0, 600, id_=1),
             _t("r1-b", "tests (cihost-linux)", 120, 700, id_=2)]
    pair = fcs.find_overlap(tasks)
    assert pair and {pair["a"]["branch"], pair["b"]["branch"]} == {"r1-a", "r1-b"}


def test_only_full_suite_jobs_can_form_the_pair(fcs):
    tasks = [_t("r1-a", "talos-contract", 0, 600, id_=1),
             _t("r1-b", "talos-contract", 0, 600, id_=2)]
    assert fcs.find_overlap(tasks) is None
    tasks = [_t("r1-a", "lint", 0, 600, wf="plans.yml", id_=1),
             _t("r1-b", "tests (cihost)", 0, 600, id_=2)]
    assert fcs.find_overlap(tasks) is not None


# ---------------------------------------------------------------- the round
def test_a_round_records_pushes_and_leaves_no_branch(world):
    r = world.run(world.round_args())
    assert r.returncode == 0, r.stderr
    assert Path(r.stdout.strip()) == world.out / "round-iso-1.json"
    rec = world.record()
    assert rec["counts"] is True and rec["overlap"] is not None
    assert (rec["kind"], rec["round"]) == ("iso", 1)
    assert (rec["scratch_sha"], rec["tree"], rec["base_commit"]) == (
        world.sha, world.tree, world.base)
    assert len(rec["tasks"]) == 12
    assert {t["branch"] for t in rec["tasks"]} == {
        "ci-stress/0113-r1-a", "ci-stress/0113-r1-b", "ci-stress/0113-r1-c"}
    assert {"id", "run_number", "workflow", "name", "branch", "result",
            "run_started_at", "updated_at"} <= set(rec["tasks"][0])
    assert world.forge.stress_refs() == []
    # one push put the three branches up together
    assert world.forge.sim and len(world.forge.sim) == 3


def test_an_uncounted_round_is_recorded_and_exits_1(world):
    world.forge.plan = _serial_plan
    r = world.run(world.round_args())
    assert r.returncode == 1
    assert world.record()["counts"] is False
    assert world.forge.stress_refs() == []


def test_a_non_empty_queue_refuses_before_any_push(world):
    world.forge.set_ci(world.sha, "pending", [{"context": "CI / tests", "status": "pending"}])
    r = world.run(world.round_args())
    assert r.returncode == 2 and "queue is not empty" in r.stderr
    assert world.forge.stress_refs() == [] and world.forge.sim == {}
    assert not world.forge.requests_matching("DELETE", "")
    assert not (world.out / "round-iso-1.json").exists()


def test_a_superseded_pending_head_blocks_the_round(world):
    """The queue is read from the last 10 commits, not only the branch tip."""
    g = lambda *a: subprocess.run(["git", "-C", str(world.work), *a], check=True,
                                  capture_output=True, text=True,
                                  env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    g("commit", "--allow-empty", "-m", "tip")
    g("push", "origin", "HEAD:refs/heads/main")
    world.forge.set_ci(world.sha, "pending", [{"context": "CI / tests", "status": "pending"}])
    # world.sha is now the commit under the tip; the default (success) covers the tip.
    r = world.run(world.round_args())
    assert r.returncode == 2 and "queue is not empty" in r.stderr


def test_a_queue_that_fills_between_check_and_push_refuses(world):
    def fill(calls):
        if calls == 2:                  # the re-check immediately before the push
            world.forge.set_ci(world.sha, "pending",
                               [{"context": "CI / tests", "status": "pending"}])
    # first listing sees a clean queue; by the second, main's commit is pending
    world.forge.on_branches = fill
    world.forge.set_ci(world.sha, "success", [])
    r = world.run(world.round_args())
    assert r.returncode == 2 and "queue is not empty" in r.stderr
    assert world.forge.branch_calls >= 2
    assert world.forge.stress_refs() == [] and world.forge.sim == {}


def test_branches_are_deleted_when_a_job_goes_red(world):
    plan = world.forge.plan
    world.forge.plan = lambda i: [
        (wf, n, s, e, "failure" if (i == 1 and n == "lint") else res)
        for wf, n, s, e, res in plan(i)]
    r = world.run(world.round_args())
    assert r.returncode == 1
    rec = world.record()
    assert [t["result"] for t in rec["tasks"]].count("failure") == 1
    assert world.forge.stress_refs() == []


def test_branches_are_deleted_when_the_bound_expires(world):
    world.forge.hold = True
    r = world.run(world.round_args(1, "iso", "--bound", "0.02"))
    assert r.returncode == 3 and "bound" in r.stderr
    assert world.forge.sim, "the branches were pushed before the bound ran out"
    assert world.forge.stress_refs() == []
    assert not (world.out / "round-iso-1.json").exists()


@pytest.mark.parametrize("sig, code", [(signal.SIGTERM, 143), (signal.SIGINT, 130),
                                       (signal.SIGHUP, 129)])
def test_branches_are_deleted_when_interrupted(world, sig, code):
    world.forge.hold = True
    p = subprocess.Popen(world.round_args(1, "iso", "--bound", "5"), env=world.env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 30
        while len(world.forge.stress_refs()) < 3 and time.time() < deadline:
            time.sleep(0.05)
        assert len(world.forge.stress_refs()) == 3, "never reached the wait"
        p.send_signal(sig)
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == code
    assert world.forge.stress_refs() == []


def test_an_existing_stress_branch_is_refused(world):
    subprocess.run(["git", "-C", str(world.work), "push", "origin",
                    "HEAD:refs/heads/ci-stress/0113-r1-b"], check=True, capture_output=True)
    r = world.run(world.round_args())
    assert r.returncode == 2 and "already on the forge" in r.stderr
    # the refusal pushed nothing and so deleted nothing: the stale branch is the operator's
    assert world.forge.stress_refs() == ["ci-stress/0113-r1-b"]


def test_an_existing_record_is_not_clobbered(world):
    world.out.mkdir()
    (world.out / "round-iso-1.json").write_text("{}")
    r = world.run(world.round_args())
    assert r.returncode == 2 and "--replace" in r.stderr
    assert world.forge.stress_refs() == [] and world.forge.sim == {}


def test_an_admin_token_helper_is_refused(world):
    env = {**world.env(), "FLEET_CI_STRESS_TOKEN_CMD": "fetch-forgejo-admin-token.sh"}
    r = subprocess.run(world.round_args(), env=env, capture_output=True, text=True)
    assert r.returncode == 2 and "admin" in r.stderr
    assert world.forge.requests == []


def test_the_token_never_reaches_argv_output_or_disk(world):
    args = world.round_args()
    r = world.run(args)
    assert r.returncode == 0
    assert TOKEN not in " ".join(args) and TOKEN not in r.stdout + r.stderr
    assert all(TOKEN not in p.read_text() for p in world.out.iterdir())
    assert world.forge.requests and all(q["login"] == "implbot" for q in world.forge.requests)


# ------------------------------------------------------------------- a rerun
def _prior_attempt(world, k=1, runs=(51, 53, 55), plans=(52, 54, 56), result="success"):
    """An earlier attempt of round k: same three branches, same commit, already
    finished, with run numbers (and so ids) the new attempt will not reuse."""
    i = 1
    for idx, b in enumerate("abc"):
        for wf, name, s, e, res in _good_plan(idx):
            run = plans[idx] if wf == "plans.yml" else runs[idx]
            world.forge.static_tasks.append(
                _job(f"ci-stress/0113-r{k}-{b}", wf, name, s, e, result, world.sha,
                     run, 3000 + i))
            i += 1


def test_a_rerun_waits_for_its_own_runs_and_records_only_them(world):
    _prior_attempt(world)
    world.forge.appear_after = 6         # the new CI has not started on the first polls
    r = world.run(world.round_args())
    assert r.returncode == 0, r.stderr
    rec = world.record()
    old = {t["id"] for t in world.forge.static_tasks}
    assert len(rec["tasks"]) == 12 and not ({t["id"] for t in rec["tasks"]} & old)
    assert all(t["run_number"] >= 100 for t in rec["tasks"])
    assert rec["excluded_prior_runs"] == {
        "ci-stress/0113-r1-a": 2, "ci-stress/0113-r1-b": 2, "ci-stress/0113-r1-c": 2}
    assert world.forge.sim and world.forge.stress_refs() == []


def test_a_rerun_whose_runs_never_appear_does_not_count_the_old_ones(world):
    _prior_attempt(world)
    world.forge.appear_after = 10 ** 9
    r = world.run(world.round_args(1, "iso", "--bound", "0.02"))
    assert r.returncode == 3 and "bound" in r.stderr
    assert not (world.out / "round-iso-1.json").exists()
    assert world.forge.stress_refs() == []


def test_a_first_attempt_excludes_nothing(world):
    r = world.run(world.round_args())
    assert r.returncode == 0, r.stderr
    assert world.record()["excluded_prior_runs"] == {
        "ci-stress/0113-r1-a": 0, "ci-stress/0113-r1-b": 0, "ci-stress/0113-r1-c": 0}


def test_an_unreadable_baseline_refuses_before_the_push(world):
    _prior_attempt(world)
    world.forge.fail_runs = True
    r = world.run(world.round_args())
    assert r.returncode == 2 and "baseline" in r.stderr
    assert world.forge.stress_refs() == [] and world.forge.sim == {}
    assert not world.forge.requests_matching("DELETE", "")
    assert not (world.out / "round-iso-1.json").exists()


def test_a_prior_run_with_no_id_refuses_before_the_push(world):
    _prior_attempt(world)
    world.forge.no_run_ids = True
    r = world.run(world.round_args())
    assert r.returncode == 2 and "no id" in r.stderr
    assert world.forge.stress_refs() == [] and world.forge.sim == {}


def test_gate_reads_a_record_written_without_the_new_field(world, fcs):
    recs = _full(fcs)
    assert all("excluded_prior_runs" not in r for r in recs)
    _write(world.out, *recs[:5], {**recs[5], "excluded_prior_runs": {"x": 1}})
    r = _gate(world)
    assert r.returncode == 0 and "GATE: PASS" in r.stdout


# ------------------------------------------------------------------ rebuild
def _static_round(world, k, runs=(1258, 1260, 1262), plans=(1259, 1261, 1263), sha="f" * 40,
                  overlap=True):
    plan = _good_plan if overlap else _serial_plan
    i = 1
    for idx, b in enumerate("abc"):
        for wf, name, s, e, res in plan(idx):
            run = plans[idx] if wf == "plans.yml" else runs[idx]
            world.forge.static_tasks.append(
                _job(f"ci-stress/0113-r{k}-{b}", wf, name, s, e, res, sha, run, 5000 + i))
            i += 1


def test_rebuild_writes_a_record_from_the_api_alone(world):
    _static_round(world, 1)
    r = world.run(world.argv("rebuild", "--kind", "iso", "--round", "1",
                             "--runs", "1258,1260,1262", "--repo-dir", str(world.work),
                             "--out-dir", str(world.out)))
    assert r.returncode == 0, r.stderr
    rec = world.record()
    assert rec["source"] == "rebuild" and rec["counts"] is True
    assert rec["scratch_sha"] == "f" * 40
    assert len(rec["tasks"]) == 12             # the plans.yml runs came along
    assert not world.forge.requests_matching("DELETE", "")
    assert world.forge.stress_refs() == []     # nothing was pushed


def test_rebuild_refuses_a_run_the_api_lacks(world):
    _static_round(world, 1)
    r = world.run(world.argv("rebuild", "--kind", "iso", "--round", "1",
                             "--runs", "1258,1260,9999", "--repo-dir", str(world.work),
                             "--out-dir", str(world.out)))
    assert r.returncode == 2 and "9999" in r.stderr
    assert not (world.out / "round-iso-1.json").exists()


def test_rebuild_refuses_runs_that_are_not_the_rounds_branches(world):
    _static_round(world, 2)
    r = world.run(world.argv("rebuild", "--kind", "iso", "--round", "1",
                             "--runs", "1258,1260,1262", "--repo-dir", str(world.work),
                             "--out-dir", str(world.out)))
    assert r.returncode == 2 and "not the three" in r.stderr


# --------------------------------------------------------------------- gate
def _rec(fcs, kind, k, counts=True, red=False, lint=True):
    tasks = []
    for b in "abc":
        br = f"ci-stress/0113-r{k}-{b}"
        tasks.append({"id": 1, "run_number": 1, "workflow": "ci.yml", "name": "tests (cihost)",
                      "branch": br, "result": "success",
                      "run_started_at": _iso(0), "updated_at": _iso(10)})
        if lint:
            tasks.append({"id": 2, "run_number": 2, "workflow": "plans.yml", "name": "lint",
                          "branch": br, "result": "failure" if red and b == "b" else "success",
                          "run_started_at": _iso(0), "updated_at": _iso(10)})
    return {"kind": kind, "round": k, "tasks": tasks, "counts": counts}


def _write(d, *recs):
    d.mkdir(exist_ok=True)
    for r in recs:
        (d / f"round-{r['kind']}-{r['round']}.json").write_text(json.dumps(r))


def _gate(world):
    return subprocess.run(world.argv("gate", "--out-dir", str(world.out)),
                          capture_output=True, text=True, timeout=30)


def _full(fcs, **over):
    iso = [_rec(fcs, "iso", k, **(over.get("iso", {}).get(k, {}))) for k in (1, 2, 3)]
    ctl = [_rec(fcs, "ctl", k, **(over.get("ctl", {}).get(k, {}))) for k in (1, 2, 3)]
    return iso + ctl


def test_gate_passes_on_three_counted_green_iso_rounds_and_three_ctl(world, fcs):
    _write(world.out, *_full(fcs))
    r = _gate(world)
    assert r.returncode == 0 and "GATE: PASS" in r.stdout


def test_gate_passes_with_a_red_control_round(world, fcs):
    """Control rounds are recorded, not gated on their result."""
    _write(world.out, *_full(fcs, ctl={2: {"red": True, "counts": False}}))
    assert _gate(world).returncode == 0


def test_gate_fails_on_a_red_isolated_job(world, fcs):
    _write(world.out, *_full(fcs, iso={3: {"red": True}}))
    r = _gate(world)
    assert r.returncode == 1 and "GATE: FAIL" in r.stdout


def test_gate_fails_on_an_uncounted_round(world, fcs):
    _write(world.out, *_full(fcs, iso={2: {"counts": False}}))
    r = _gate(world)
    assert r.returncode == 1 and "does not count" in r.stdout


def test_gate_fails_on_a_missing_control_round(world, fcs):
    recs = _full(fcs)[:-1]
    _write(world.out, *recs)
    r = _gate(world)
    assert r.returncode == 1 and "2 of 3 control" in r.stdout


def test_gate_fails_when_plans_lint_is_absent(world, fcs):
    _write(world.out, *_full(fcs, iso={1: {"lint": False}}))
    assert _gate(world).returncode == 1


def test_gate_fails_with_no_records_and_on_an_unreadable_one(world, fcs):
    world.out.mkdir()
    assert _gate(world).returncode == 1
    (world.out / "round-iso-1.json").write_text("not json")
    assert _gate(world).returncode == 2


def test_gate_edits_nothing(world, fcs):
    _write(world.out, *_full(fcs))
    before = {p.name: p.read_bytes() for p in world.out.iterdir()}
    _gate(world)
    assert {p.name: p.read_bytes() for p in world.out.iterdir()} == before
