"""Tests for plan 0210 D2/D3: `dispatch_review` waits long enough for
`dispatch-review.py` to finish, names the line that explains a failure rather
than whatever line happened to print last, and gives a cihost (local)
reviewer seat the longer timeout talos plan 0054 gives it."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin"

SHA1 = "1" * 40
REPO = "operator/x"


def _load(tmp_path, **env):
    os.environ["EUNOMIA_FLEET_DIR"] = str(tmp_path / "fleet")
    os.environ["FLEET_DB"] = str(tmp_path / "fleet.db")
    for k in ("FLEET_REVIEW_CMD", "EUNOMIA_SESSION"):
        os.environ.pop(k, None)
    os.environ["EUNOMIA_SESSION"] = "review-dispatch-test"
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader(
        "orchestrator_rev_dispatch", str(BIN / "orchestrator"))
    spec = importlib.util.spec_from_loader("orchestrator_rev_dispatch", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _cmd(tmp_path, script):
    p = tmp_path / "fake-review.py"
    p.write_text(script)
    return f"{sys.executable} {p}"


# ---------------------------------------------------------- D3: failure line

def test_a_fatal_line_survives_a_later_bookkeeping_line(tmp_path):
    """The eunomia#1334 round-2 case: ordinal 1's FATAL text, embedded in the
    `dispatch-review: WARN ordinal` line, must not be lost behind ordinal 2's
    own bookkeeping print that comes after it."""
    mod = _load(tmp_path, FLEET_REVIEW_CMD=_cmd(tmp_path, """
import sys
sys.stderr.write("dispatch-review: WARN ordinal 1 (z-ai/glm-5.3) did not run: "
                 "FATAL: runner 'hosted-glm-5.3': the prompt (~203728 tokens) "
                 "exceeds max_input_tokens 200000 -- nothing was fetched.\\n")
sys.stderr.write("dispatch-review: recorded round 9 for ordinal 2\\n")
sys.exit(1)
"""))
    ok, why = mod.dispatch_review(REPO, 7, SHA1, "plans/p.md")
    assert not ok
    assert why.startswith("dispatch-review: WARN ordinal 1")
    assert "exceeds max_input_tokens" in why
    assert "recorded round 9" not in why


def test_a_refusing_line_is_preferred_over_what_follows_it(tmp_path):
    mod = _load(tmp_path, FLEET_REVIEW_CMD=_cmd(tmp_path, """
import sys
sys.stderr.write("REFUSING: worst-case $4.12 exceeds --max-cost\\n")
sys.stderr.write("dispatch-review: some other trailing note\\n")
sys.exit(1)
"""))
    ok, why = mod.dispatch_review(REPO, 7, SHA1, "plans/p.md")
    assert not ok and why.startswith("REFUSING: worst-case")


def test_with_no_marked_line_the_last_line_is_kept(tmp_path):
    mod = _load(tmp_path, FLEET_REVIEW_CMD=_cmd(tmp_path, """
import sys
sys.stderr.write("CI is still running\\n")
sys.exit(2)
"""))
    ok, why = mod.dispatch_review(REPO, 7, SHA1, "plans/p.md")
    assert not ok and why == "CI is still running"


def test_the_detail_is_cut_to_200_characters(tmp_path):
    mod = _load(tmp_path, FLEET_REVIEW_CMD=_cmd(tmp_path, """
import sys
sys.stderr.write("FATAL: " + ("x" * 400) + "\\n")
sys.exit(1)
"""))
    ok, why = mod.dispatch_review(REPO, 7, SHA1, "plans/p.md")
    assert not ok and len(why) == 200


# ---------------------------------------------------------------- D2: timeout

def test_the_default_dispatch_timeout_is_6000(tmp_path, monkeypatch):
    mod = _load(tmp_path)
    seen = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return real_run([sys.executable, "-c", "pass"], **{
            k: v for k, v in kwargs.items() if k != "timeout"})
    monkeypatch.setattr(mod.subprocess, "run", spy)
    mod.dispatch_review(REPO, 7, SHA1, "plans/p.md")
    assert seen["timeout"] == mod.REVIEW_DISPATCH_TIMEOUT == 6000


def test_an_explicit_cihost_timeout_is_passed_through(tmp_path, monkeypatch):
    mod = _load(tmp_path)
    seen = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen.update(kwargs)
        return real_run([sys.executable, "-c", "pass"], **{
            k: v for k, v in kwargs.items() if k != "timeout"})
    monkeypatch.setattr(mod.subprocess, "run", spy)
    mod.dispatch_review(REPO, 7, SHA1, "plans/p.md",
                        timeout=mod.REVIEW_DISPATCH_TIMEOUT_CIHOST)
    assert seen["timeout"] == mod.REVIEW_DISPATCH_TIMEOUT_CIHOST == 9600


# ------------------------------------------------------- D2: the cihost seat

def _set(repo, position, runner):
    r = subprocess.run([sys.executable, str(BIN / "fleet-models"), "set",
                        repo, position, runner],
                       env=os.environ, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_a_local_reviewer_seat_is_cihost(tmp_path):
    mod = _load(tmp_path)
    _set(REPO, "code_reviewer", "local-qwen")
    assert mod._cihost_review_seat(REPO, "code_reviewer") is True


def test_a_hosted_reviewer_seat_is_not_cihost(tmp_path):
    mod = _load(tmp_path)
    _set(REPO, "code_reviewer", "hosted-glm-5.3")
    assert mod._cihost_review_seat(REPO, "code_reviewer") is False


def test_an_unconfigured_repo_is_not_cihost(tmp_path):
    mod = _load(tmp_path)
    assert mod._cihost_review_seat(REPO, "code_reviewer") is False


def test_a_cihost_ordinal_survives_a_broken_sibling_ordinal(tmp_path, monkeypatch):
    """A non-UnknownRunner exception resolving ordinal 1 must not hide a cihost
    ordinal 2 behind a blanket False for the whole position."""
    mod = _load(tmp_path)
    db_path = tmp_path / "models.db"
    db_path.touch()

    class FakeModels:
        DB = str(db_path)
        UnknownRunner = ValueError

        @staticmethod
        def active_ordinals(con, repo, position):
            return [1, 2]

        @staticmethod
        def resolve(con, repo, position, ordinal):
            if ordinal == 1:
                raise RuntimeError("boom: not an UnknownRunner")
            return "local-qwen", "repo"

        @staticmethod
        def runner_harness(r):
            return "epeius"

    monkeypatch.setattr(mod, "_load_sibling", lambda name, mod_name: FakeModels())
    assert mod._cihost_review_seat(REPO, "code_reviewer") is True


# --------------------------------------- D2: the two call sites, one helper

def test_review_dispatch_timeout_widens_for_a_cihost_seat(tmp_path):
    mod = _load(tmp_path)
    _set(REPO, "code_reviewer", "local-qwen")
    assert mod._review_dispatch_timeout(REPO, "code_reviewer") == 9600


def test_review_dispatch_timeout_stays_default_for_a_hosted_seat(tmp_path):
    mod = _load(tmp_path)
    _set(REPO, "code_reviewer", "hosted-glm-5.3")
    assert mod._review_dispatch_timeout(REPO, "code_reviewer") == 6000


def test_review_loop_and_lane_review_job_share_the_timeout_helper(tmp_path):
    """review_loop and _lane_review_job must both route through
    `_review_dispatch_timeout` rather than each carrying their own copy of the
    cihost/hosted conditional, which is what let the two drift apart."""
    import inspect
    mod = _load(tmp_path)
    assert "_review_dispatch_timeout(repo, position)" in inspect.getsource(
        mod.review_loop)
    assert "_review_dispatch_timeout(repo, position)" in inspect.getsource(
        mod._lane_review_job)


# -------------------------------- D2: lane reviews follow the fallback seat

def test_lane_review_job_gets_the_cihost_timeout_through_the_code_reviewer_fallback(
        tmp_path, monkeypatch):
    """`lane_reviewer` unconfigured (exit 3, the `fleet-models reviewers`
    contract `_lane_review_seat` falls back on), `code_reviewer` a cihost
    runner: the lane review must still get REVIEW_DISPATCH_TIMEOUT_CIHOST
    (finding: eunomia#1386 round 2), because `_lane_review_seat` — not a bare
    "lane_reviewer" — is what `review_opened_lanes` actually asked to build
    its expected-reviewers set."""
    mod = _load(tmp_path)

    def fake_run(argv, **kw):
        assert argv[argv.index("reviewers") + 2] == "lane_reviewer"
        return subprocess.CompletedProcess(argv, 3, "", "")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    position = mod._lane_review_seat(REPO)
    assert position == "code_reviewer"

    monkeypatch.setattr(mod, "_cihost_review_seat",
                        lambda repo, pos: pos == "code_reviewer")
    assert mod._review_dispatch_timeout(REPO, position) == 9600
