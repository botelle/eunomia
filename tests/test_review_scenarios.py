"""The review loop, rehearsed end to end with no model and no forge (plan 0099).

Every row runs the REAL `bin/orchestrator` as a subprocess, the way `fleet-watch`
spawns it, against three fakes: a scripted implementer (`tests/fakes/claude`), a
scripted reviewer (`tests/fakes/review.py`) and an in-memory Forgejo
(`tests/fakes/forge.py`). Each row asserts what the loop DOES today — this file
pins behaviour, it does not judge it (docs/review-scenarios.md).

Why a subprocess: every other test of the loop calls its functions with a
neighbour stubbed, and the seams between them — env, argv, PATH, exit codes, the
forge — are what failed on 2026-09-28. Only a spawned process crosses them the
way production does.

ONE THING IS NOT PRODUCTION: sleeps. `CI_POLL_SECS` (30 s) has no env override,
and every review round waits two polls, so the loop cannot finish eleven rows in
a minute at production pace. `sitecustomize.py` (written into each scenario's
tmp dir and put on PYTHONPATH) caps any sleep of a second or more at 20 ms. It
touches nothing but pace. The missing seam is named in the handoff.
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"
FAKES = Path(__file__).resolve().parent / "fakes"


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(mod)
    return mod


fake_forge = _load("fake_forge_0099", FAKES / "forge.py")
fake_review = _load("fake_review_0099", FAKES / "review.py")

REPO = "operator/demo"
PLAN_PATH = "plans/0042-a-feature.md"
PLAN_ID = "0042-a-feature"
BRANCH = "feat/0042-a-feature"
LEASE = "branch--feat-0042-a-feature--001"
SESSION = "orch-scenario"
AUTHOR_TOKEN = "tok-implbot"
REVIEWER_TOKEN = "tok-revbot"
ADMIN_TOKEN = "tok-operator"

PLAN = """\
---
id: 0042-a-feature
status: ready
zone: public
tier: 1
paths: ["bin/thing"]
---

# A feature

## 3. Boundaries
- do not X, because Y — the reasoning is what transfers
"""

# The loop's pacing knobs have no env override. See the module docstring.
SITECUSTOMIZE = """\
import time as _t
_real = _t.sleep
_t.sleep = lambda s: _real(min(s, 0.02) if s >= 1 else s)
"""

# Rows script these. Kept as data so a row reads as its script.
APPROVE = {"action": "approve"}
WORK = {"action": "work"}


def comment(*findings):
    return {"action": "comment", "findings": list(findings)}


def request_changes(*findings):
    return {"action": "request_changes", "findings": list(findings)}


HIGH = ("HIGH", "the boundary is not enforced on the retry path")
MEDIUM = ("MEDIUM", "the helper name misleads")
LOW = ("LOW", "a comment is stale")


def _git(args, cwd):
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args],
                       cwd=str(cwd), capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def _bare_remote(root):
    """A bare `operator/demo.git` holding one ready plan, reachable as
    FLEET_GIT_SSH_BASE=<root>/forge the way `_forge_dir` in
    test_orchestrator_session.py builds it."""
    src = root / "src"
    (src / "plans").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    (src / PLAN_PATH).write_text(PLAN)
    _git(["add", "."], src)
    _git(["commit", "-qm", "plan"], src)
    bare = root / "forge" / "operator" / "demo.git"
    bare.parent.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(bare)], check=True)
    return bare


def _write_lease(fleet):
    d = fleet / "leases"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{LEASE}.json").write_text(json.dumps({
        "id": LEASE, "state": "assigned", "holder": SESSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "resource": {"type": "branch", "repo": REPO, "branch": BRANCH},
    }))


class Result:
    """What a row asserts against."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def rounds(self):
        """Review rounds the fake reviewer was actually asked to take."""
        return len([c for c in self.reviewer_calls if "--probe-limit" not in c])

    @property
    def impl_runs(self):
        return len(self.implementer_prompts)

    @property
    def failure_reason(self):
        for e in self.events:
            if e.get("type") == "plan-failed":
                return (e.get("detail") or {}).get("reason")
        return None

    @property
    def succeeded(self):
        return self.rc == 0 and self.failure_reason is None

    def lease_state(self):
        return json.loads((self.fleet / "leases" / f"{LEASE}.json").read_text())["state"]

    def comments(self):
        return [c["body"] for c in self.forge.comments_of(1)]

    def review_states(self):
        return [r["state"] for r in self.forge.reviews_of(1)]


class Scenario:
    """One row's world: bare remote, fake forge, temp fleet dir and DB, scripts."""

    def __init__(self, tmp_path, reviewer, implementer, ci=None):
        assert reviewer, ("a row must script its reviewer — an empty script "
                          "cannot say what the loop was supposed to do")
        assert implementer, "a row must script its implementer"
        self.root = tmp_path
        self.fleet = tmp_path / "fleet"
        self.bare = _bare_remote(tmp_path)
        self.forge = fake_forge.FakeForge(
            {REPO: str(self.bare)},
            {AUTHOR_TOKEN: "implbot", REVIEWER_TOKEN: "revbot",
             ADMIN_TOKEN: "operator"},
            ci=ci).start()
        self.reviewer_script = tmp_path / "reviewer.json"
        self.reviewer_script.write_text(json.dumps(reviewer))
        self.implementer_script = tmp_path / "implementer.json"
        self.implementer_script.write_text(json.dumps(implementer))
        self.db = self.fleet / "fleet.db"
        self._sitecustomize = tmp_path / "site"
        self._sitecustomize.mkdir()
        (self._sitecustomize / "sitecustomize.py").write_text(SITECUSTOMIZE)
        self._bin = tmp_path / "fakebin"
        self._bin.mkdir()
        shim = self._bin / "claude"
        shim.write_text(
            "#!/bin/sh\n"
            f"export FAKE_CLAUDE_SCRIPT={self.implementer_script}\n"
            f"export FAKE_FORGE_URL={self.forge.url}\n"
            f"export FAKE_CLAUDE_TOKEN={AUTHOR_TOKEN}\n"
            f'exec "{sys.executable}" "{FAKES / "claude"}" "$@"\n')
        shim.chmod(0o755)
        _write_lease(self.fleet)

    def env(self):
        """Every seam pointed at the fakes, and nothing else inherited that a
        host could have set: FLEET_*/EUNOMIA_* from the developer's shell would
        make a row pass or fail depending on whose machine runs it."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("FLEET_", "EUNOMIA_", "FAKE_", "ANGELIA"))}
        env.update({
            "HOME": str(self.root / "home"),
            "PATH": f"{self._bin}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(self._sitecustomize),
            "EUNOMIA_FLEET_DIR": str(self.fleet),
            "EUNOMIA_SESSION": SESSION,
            "FLEET_CONFIG_BIN": os.environ.get("FLEET_CONFIG_BIN", ""),   # plan 0132
            "EUNOMIA_TRANSCRIPT_DIR": str(self.root / "transcripts"),
            "EUNOMIA_IMPL_RUNNER": "sub-sonnet",
            "FLEET_WORK_ROOT": str(self.root / "work"),
            "FLEET_GIT_SSH_BASE": str(self.root / "forge"),
            "FLEET_FORGEJO_URL": self.forge.url,
            "FLEET_AUTHOR_TOKEN_CMD": f"echo {AUTHOR_TOKEN}",
            "FLEET_CLAUDE_TOKEN_CMD": "echo stub-claude-oauth-value",   # plan 0116
            "FLEET_TOKEN_CMD": f"echo {AUTHOR_TOKEN}",
            "FLEET_NTFY_URL": self.forge.url + "/ntfy",
            "FLEET_DB": str(self.db),
            "FLEET_REVIEW_CMD": f"{sys.executable} {FAKES / 'review.py'}",
            "FAKE_REVIEW_SCRIPT": str(self.reviewer_script),
            "FAKE_FORGE_URL": self.forge.url,
            "FAKE_REVIEW_TOKEN": REVIEWER_TOKEN,
            "FLEET_WATCH_REPOS": REPO,
        })
        (self.root / "home").mkdir(exist_ok=True)
        # Loopback only. A row that reached anything else would spend real
        # quota or need a credential, which is the whole point of the fakes.
        # Only the forge/ntfy seams are checked: the CI runner ambiently sets
        # ACTIONS_CACHE_URL and friends to a LAN address, and nothing the
        # orchestrator reads goes through them.
        for k, v in env.items():
            if k.endswith("_URL") and k.startswith(("FLEET_", "FAKE_")):
                assert v.startswith("http://127.0.0.1:"), f"{k}={v} is not loopback"
        return env

    def run(self, before=None):
        if before is not None:
            before(self.forge)
        r = subprocess.run(
            [sys.executable, str(BIN / "orchestrator"), REPO, PLAN_PATH, LEASE],
            env=self.env(), capture_output=True, text=True, timeout=60)
        return self.result(r)

    def result(self, proc=None):
        self.forge.assert_no_unmodelled()
        events = []
        ev = self.fleet / "events.jsonl"
        if ev.exists():
            events = [json.loads(l) for l in ev.read_text().splitlines() if l.strip()]
        calls = Path(str(self.reviewer_script) + ".calls")
        prompts = sorted((Path(str(self.implementer_script) + ".prompts")).glob("*.txt"),
                         key=lambda p: int(p.stem)) \
            if Path(str(self.implementer_script) + ".prompts").exists() else []
        pages = self.fleet / "pages.jsonl"
        return Result(
            rc=None if proc is None else proc.returncode,
            stdout="" if proc is None else proc.stdout,
            stderr="" if proc is None else proc.stderr,
            forge=self.forge, fleet=self.fleet, events=events,
            reviewer_calls=[json.loads(l) for l in calls.read_text().splitlines()]
            if calls.exists() else [],
            implementer_prompts=[p.read_text() for p in prompts],
            ntfy=list(self.forge.ntfy),
            pages=[json.loads(l) for l in pages.read_text().splitlines()]
            if pages.exists() else [])

    def close(self):
        self.forge.stop()


@pytest.fixture
def scenario(tmp_path):
    made = []

    def make(reviewer, implementer=None, ci=None):
        sc = Scenario(tmp_path, reviewer, [WORK] * 8 if implementer is None else
                      implementer, ci=ci)
        made.append(sc)
        return sc

    yield make
    for sc in made:
        sc.close()


def _mopsus():
    return _load("mopsus_0099", BIN / "mopsus")


# ------------------------------------------------------------------ the rows
def test_row1_an_approval_at_head_succeeds_in_one_round(scenario):
    r = scenario([APPROVE]).run()
    assert r.succeeded, r.stderr
    assert (r.rounds, r.impl_runs) == (1, 1)
    assert r.review_states() == ["APPROVED"]
    # the argv the orchestrator really passes: --post and --no-followups are
    # both load-bearing (dispatch_review's docstring)
    assert r.reviewer_calls == [[REPO, "1", "--post", "--no-followups"]]
    assert r.lease_state() == "released"
    assert r.ntfy == [], "a successful run pages nobody"
    # the ledger and the deferred-findings comment are posted on success
    assert any("Orchestrator review ledger" in c for c in r.comments())
    assert any("Deferred findings" in c for c in r.comments())


def test_row2_a_high_finding_buys_a_fix_round_then_an_approval(scenario):
    r = scenario([comment(HIGH), APPROVE]).run()
    assert r.succeeded, r.stderr
    assert (r.rounds, r.impl_runs) == (2, 2)
    assert r.review_states() == ["COMMENT", "APPROVED"]
    # the fix session is briefed with the reviewer's own words
    assert HIGH[1] in r.implementer_prompts[1]
    heads = {rv["commit_id"] for rv in r.forge.reviews_of(1)}
    assert len(heads) == 2, "round 2 reviewed the fix, not the first tree"


def test_row3_request_changes_is_read_the_same_as_comment(scenario):
    r = scenario([request_changes(HIGH), APPROVE]).run()
    assert r.succeeded, r.stderr
    assert (r.rounds, r.impl_runs) == (2, 2)
    assert r.review_states() == ["REQUEST_CHANGES", "APPROVED"]


def test_row4_a_reviewer_that_never_approves_fails_at_the_review_bound(scenario):
    r = scenario([comment(HIGH)] * 5).run()
    assert not r.succeeded and r.rc == 1
    assert re.search(r"review bound 5 exhausted for operator/demo without an approval",
                     r.failure_reason), r.failure_reason
    # five reviews, and the implementer runs before the first and between them
    assert (r.rounds, r.impl_runs) == (5, 5)
    assert r.lease_state() == "active", "a failed run holds its lease (_stop)"
    assert any("Orchestrator stopped" in n for n in r.ntfy)
    assert any("review ledger" in c for c in r.comments()), \
        "the ledger is posted whatever the outcome"


def test_row5_only_medium_and_low_findings_from_a_withholder_buy_a_fix_round(scenario):
    """Plan 0132 D3 changes what this row used to pin. A reviewer that posted
    COMMENT withheld for something; its findings are all below the `high` bar, so
    all of them go to the fix round instead of the run stopping on 'no parseable
    blocking finding'. Only a withholding review with no finding at all still
    stops (row 6). The script then runs out, which ends the run."""
    r = scenario([comment(MEDIUM, LOW)]).run()
    assert not r.succeeded
    assert r.impl_runs == 2, "the build run plus one fix round"
    assert "round 2: review dispatch failed" in r.failure_reason, r.failure_reason


def test_row6_an_unparseable_review_is_not_read_as_an_approval(scenario):
    """#627's reason."""
    r = scenario([{"action": "unparseable"}]).run()
    assert not r.succeeded
    assert "refusing to read an unparsed review as an approval" in r.failure_reason
    assert (r.rounds, r.impl_runs) == (1, 1)


def test_row7_an_approval_printed_and_never_posted_did_not_land(scenario):
    """2026-09-07: 'APPROVE' read from terminal output while nothing was posted."""
    r = scenario([{"action": "post_nothing"}]).run()
    assert not r.succeeded
    assert "the verdict did not land" in r.failure_reason
    assert r.forge.reviews_of(1) == []
    assert r.rounds == 1


def test_row8_an_approval_of_an_earlier_head_is_not_the_verdict(scenario):
    r = scenario([comment(HIGH), {"action": "stale"}]).run()
    assert not r.succeeded
    assert "the verdict did not land" in r.failure_reason
    approvals = [rv for rv in r.forge.reviews_of(1) if rv["state"] == "APPROVED"]
    assert len(approvals) == 1
    head = r.forge._head_sha(REPO, BRANCH)
    assert approvals[0]["commit_id"] != head, "the approval was of the old tree"
    assert (r.rounds, r.impl_runs) == (2, 2)


def test_row9_a_dispatcher_that_crashes_fails_the_round_and_mopsus_cannot_class_it(
        scenario):
    """2026-09-28, plan 0095's failure. Mopsus files it `unclassified` today;
    plan 0095 changes that to `review-path`, and this assertion is what turns
    red, deliberately, when it lands."""
    err = ("PermissionError: [Errno 13] Permission denied: "
           "'/Users/x/.local/share/pins/eunomia/bin/fleet-config'")
    r = scenario([{"action": "exit", "code": 1, "stderr": err}]).run()
    assert not r.succeeded
    assert re.search(r"round 1: review dispatch failed: PermissionError",
                     r.failure_reason), r.failure_reason
    assert (r.rounds, r.impl_runs) == (1, 1)
    assert _mopsus().classify(r.failure_reason) == "unclassified"


def test_row10_a_held_review_is_resumed_by_mopsus_after_the_probe(scenario):
    """A usage limit stops the round: the reviewer writes talos's review_attempt
    row and exits 75. Mopsus finds the row, probes through the same dispatcher
    (exit 0 = the limit reset), and re-runs the review — which posts."""
    sc = scenario([{"action": "quota"}, {"action": "probe", "code": 0}, APPROVE])
    r = sc.run()
    assert not r.succeeded
    assert re.search(r"round 1: review dispatch failed: usage limit reached",
                     r.failure_reason), r.failure_reason
    con = sqlite3.connect(str(sc.db))
    try:
        held = con.execute("SELECT repo, pr, outcome FROM review_attempt").fetchall()
    finally:
        con.close()
    assert held == [(REPO, 1, "quota")], "the attempt is held"
    assert r.review_states() == [], "nothing was posted while the limit stood"

    env = sc.env()
    env["EUNOMIA_SESSION"] = "mopsus"
    env["PYTHONPATH"] = str(FAKES)   # plan 0134: mopsus reads the list via fleet-watch
    p = subprocess.run([sys.executable, str(BIN / "mopsus")], env=env,
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    # the retry is detached under a lock, so it is waited for on the forge
    deadline = time.time() + 30
    while time.time() < deadline and not sc.forge.reviews_of(1):
        time.sleep(0.1)
    after = sc.result()
    assert after.review_states() == ["APPROVED"], (p.stdout, p.stderr)
    assert after.forge.reviews_of(1)[0]["user"]["login"] == "revbot"
    argvs = after.reviewer_calls
    # the orchestrator's round, mopsus's probe, then mopsus's retry (no
    # --no-followups: that flag is the orchestrator's, not the retry's)
    assert argvs[0] == [REPO, "1", "--post", "--no-followups"]
    assert argvs[1][0] == "--probe-limit"
    assert argvs[2] == [REPO, "1", "--post"]
    con = sqlite3.connect(str(sc.db))
    try:
        probes = con.execute("SELECT outcome FROM review_probe").fetchall()
    finally:
        con.close()
    assert probes == [("reset",)]
    assert any(e.get("type") == "review-requeued" for e in after.events)


def test_row11_a_red_build_is_blocked_before_any_review(scenario):
    red = {"state": "failure",
           "statuses": [{"context": "CI / tests", "status": "failure"}]}
    # the fix round changes nothing, so the loop says so and stops
    r = scenario([APPROVE], [WORK, {"action": "noop"}], ci=red).run()
    assert not r.succeeded
    assert r.rounds == 0, "no review is spent on a commit that does not build"
    assert r.reviewer_calls == []
    assert re.search(r"round 1: fix session left the head at \w{8} — nothing was "
                     r"pushed", r.failure_reason), r.failure_reason
    assert "The build is failing on your pull request" in r.implementer_prompts[1]
    assert "CI / tests" in r.implementer_prompts[1]
    assert any("ci-failure" in c for c in r.comments()), \
        "the ledger records the blocked round"
    assert r.impl_runs == 2


# ------------------------------------------------- the fakes cannot pass vacuously
def test_an_empty_script_fails_the_row(tmp_path):
    with pytest.raises(AssertionError, match="script its reviewer"):
        Scenario(tmp_path, [], [WORK])


def test_a_reviewer_that_runs_out_of_script_fails_loudly(scenario):
    """Row 4 with one round scripted too few: the fake exits 98 and the loop says
    so, rather than the fake quietly approving."""
    r = scenario([comment(HIGH)] * 2).run()
    assert not r.succeeded
    assert "review dispatch failed: fake reviewer: round 3 but only 2 scripted" \
        in r.failure_reason


def test_an_unmodelled_forge_path_fails_the_row_naming_it(scenario):
    sc = scenario([APPROVE])

    def poke(forge):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(forge.url + "/api/v1/repos/operator/demo/labels")
        req.add_header("Authorization", f"token {AUTHOR_TOKEN}")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 501

    with pytest.raises(AssertionError,
                       match=r"does not model: GET /api/v1/repos/operator/demo/labels"):
        sc.run(before=poke)


def test_a_request_with_no_known_token_is_refused(scenario):
    sc = scenario([APPROVE])
    import urllib.error
    import urllib.request
    req = urllib.request.Request(sc.forge.url + "/api/v1/repos/operator/demo")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 401


def test_the_findings_builder_round_trips_through_the_orchestrators_parser():
    orch = _load("orchestrator_0099", BIN / "orchestrator")
    pairs = [("HIGH", "a: with a colon"), ("MEDIUM", "b — with a dash"),
             ("LOW", "c")]
    body = fake_review.findings_body(pairs)
    assert orch._FENCE.search(body), "the fenced block is what the parser reads"
    assert orch.parse_findings(body) == pairs
    assert orch.blocking_findings(body) == ["a: with a colon"]


def test_every_url_the_subprocess_is_given_is_loopback(scenario):
    env = scenario([APPROVE]).env()
    # The runner's own ACTIONS_*_URL are ambient and not a seam of the loop.
    urls = {k: v for k, v in env.items()
            if k.endswith("_URL") and k.startswith(("FLEET_", "FAKE_"))}
    assert urls and all(v.startswith("http://127.0.0.1:") for v in urls.values())
    assert "FLEET_ANGELIA_URL" not in env


def test_the_fake_implementer_is_committed_executable():
    assert os.access(FAKES / "claude", os.X_OK)
    r = subprocess.run(["git", "ls-files", "-s", "--", str(FAKES / "claude")],
                       cwd=str(ROOT), capture_output=True, text=True)
    if r.stdout.strip():                     # not tracked yet in a scratch copy
        assert r.stdout.startswith("100755"), r.stdout
