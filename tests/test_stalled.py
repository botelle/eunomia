"""fleet-stalled — a pull request waiting on nobody is named once a day (plan 0060).

`FakeForge` implements the three verbs this tool reads (`list_pulls`,
`list_reviews`, `get_commit_status`) with the same `(status, body)` contract
`fleetforge.Forge` promises, and each fake list holds a single page — short
enough for `paged()`'s own short-page-ends-the-scan rule to end the read the
same way a real, small result set would. That is enough to exercise every
call site through the real `fleetforge.paged()`, without a stub HTTP server.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def st(monkeypatch):
    # bin/fleet-stalled and the modules it loads read FLEET_* settings at
    # IMPORT (REVIEWER, FORGEJO_URL, TOKEN_CMD, ...), before `_wire` runs, so
    # clearing them there is too late. Load with none set: a test needs its
    # results to depend on this file, not on the shell that ran it. A test
    # that wants one sets it itself. (#1441 review, 2026-11-09.)
    for key in [k for k in os.environ if k.startswith("FLEET_")]:
        monkeypatch.delenv(key)
    return _load("fleet_stalled", BIN / "fleet-stalled")


@pytest.fixture(autouse=True)
def _wire(st, monkeypatch, tmp_path):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
    # FLEET_DB wins over EUNOMIA_FLEET_DIR in `_dispatch_db_path`, so any
    # FLEET_DB in the environment the suite runs in points every failed-plan
    # test at another database, and seven of them fail with `assert 0 == 1`
    # (#1438, 2026-11-09). Where #1438's CI run got one is not established.
    monkeypatch.delenv("FLEET_DB", raising=False)
    monkeypatch.delenv("FLEET_NTFY_URL", raising=False)
    monkeypatch.delenv("FLEET_ANGELIA_URL", raising=False)
    # notify() is bin/orchestrator's real angelia/ntfy sender — replaced with a
    # recorder so tests never touch a network, while the tool's own dedupe
    # logic (which decides whether notify() is even called) is exercised for
    # real (mirrors test_mopsus.py's own wiring).
    calls = []
    monkeypatch.setattr(st.orch, "notify",
                        lambda title, body: (calls.append((title, body)), True)[1])
    return calls


# --- a small, self-consistent fake Forgejo -----------------------------------


class FakeForge:
    def __init__(self):
        self.pulls = {}                  # repo -> [pr, ...]
        self.reviews = {}                # (repo, pr) -> [review, ...]
        self.statuses = {}               # (repo, sha) -> (status, body)
        self.calls = []

    def list_pulls(self, repo, state="open", page=1, limit=50, sort=None):
        self.calls.append(("list_pulls", repo, page))
        if page > 1:
            return 200, []
        return 200, self.pulls.get(repo, [])

    def list_reviews(self, repo, n, page=1, limit=50):
        self.calls.append(("list_reviews", repo, n, page))
        if page > 1:
            return 200, []
        return 200, self.reviews.get((repo, n), [])

    def get_commit_status(self, repo, sha):
        self.calls.append(("get_commit_status", repo, sha))
        return self.statuses.get((repo, sha), (404, None))


def _pr(number, head_sha, head_ref="feat/x", base_sha="mainsha0", merge_base="mainsha0",
       base_ref="main"):
    return {"number": number,
           "head": {"sha": head_sha, "ref": head_ref},
           "base": {"sha": base_sha, "ref": base_ref},
           "merge_base": merge_base}


def _review(commit_id, login="revbot", state="COMMENT", at="2026-09-14T11:25:29Z"):
    return {"user": {"login": login}, "state": state, "commit_id": commit_id,
           "submitted_at": at}


def _status(state):
    return 200, {"state": state}


# --------------------------------------------------------------- config parsing


def test_mode_defaults_to_report_for_unlisted_repo(st):
    assert st.mode_for("operator/unlisted", {}) == "report"
    assert st.mode_for("operator/unlisted", {"default": "report"}) == "report"


def test_mode_honors_repo_entry_over_default(st):
    values = {"default": "report", "operator/quiet": "off"}
    assert st.mode_for("operator/quiet", values) == "off"
    assert st.mode_for("operator/other", values) == "report"


def test_conf_parses_the_three_named_modes(st, tmp_path):
    conf = tmp_path / "stalled.conf"
    conf.write_text("default = report\noperator/a = off\noperator/b = dispatch\n")
    assert st.load_conf(conf) == {"default": "report", "operator/a": "off",
                                  "operator/b": "dispatch"}


def test_conf_rejects_unknown_mode(st, tmp_path):
    conf = tmp_path / "stalled.conf"
    conf.write_text("operator/a = auto-fix\n")
    with pytest.raises(st.ConfigError):
        st.load_conf(conf)


def test_conf_rejects_an_unparseable_line(st, tmp_path):
    conf = tmp_path / "stalled.conf"
    conf.write_text("this is not a config line\n")
    with pytest.raises(st.ConfigError):
        st.load_conf(conf)


def test_missing_conf_defaults_every_repo_to_report(st, tmp_path, capsys):
    values = st.load_conf(tmp_path / "does-not-exist.conf")
    assert values == {}
    assert st.mode_for("operator/anything", values) == "report"
    assert "unreadable" in capsys.readouterr().err


# ------------------------------------------------------------------- commit_state


def test_commit_state_success_is_green(st):
    forge = FakeForge()
    forge.statuses[("o/r", "sha1")] = _status("success")
    assert st.commit_state(forge, "o/r", "sha1") == ("green", True)


@pytest.mark.parametrize("state", ["failure", "error"])
def test_commit_state_failure_or_error_is_red(st, state):
    forge = FakeForge()
    forge.statuses[("o/r", "sha1")] = _status(state)
    assert st.commit_state(forge, "o/r", "sha1") == ("red", True)


@pytest.mark.parametrize("state", ["pending", "warning", ""])
def test_commit_state_other_resolved_states_are_unknown_but_readable(st, state):
    forge = FakeForge()
    forge.statuses[("o/r", "sha1")] = _status(state)
    assert st.commit_state(forge, "o/r", "sha1") == ("unknown", True)


def test_commit_state_unreadable_is_not_ok(st):
    forge = FakeForge()
    forge.statuses[("o/r", "sha1")] = (500, None)
    assert st.commit_state(forge, "o/r", "sha1") == ("unknown", False)


def test_commit_state_with_no_sha_is_unknown_and_unreadable(st):
    forge = FakeForge()
    assert st.commit_state(forge, "o/r", None) == ("unknown", False)


# -------------------------------------------------------------------- classify_pr


def test_no_reviews_ever_at_a_green_head_is_unreviewed(st):
    forge = FakeForge()
    forge.statuses[("o/r", "head1")] = _status("success")
    pr = _pr(1, "head1")
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert (state, troubles) == ("unreviewed", [])


def test_a_review_at_an_older_sha_is_stale_review_not_unreviewed(st):
    """Reproduces #414: round-1 COMMENT, fixes pushed, no second round ever
    dispatched. `unreviewed` must NOT fire just because there is no review AT
    the current head — a stale one exists, which is a different problem."""
    forge = FakeForge()
    forge.statuses[("o/r", "head2")] = _status("success")
    pr = _pr(414, "head2")
    forge.reviews[("o/r", 414)] = [
        _review("old1", state="COMMENT", at="2026-09-14T11:25:29Z"),
        _review("old2", state="COMMENT", at="2026-09-16T16:03:34Z"),
    ]
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert state == "stale-review"
    assert troubles == []
    assert "old2" in detail


def test_a_review_at_the_current_head_is_healthy_and_not_reported(st):
    forge = FakeForge()
    forge.statuses[("o/r", "head3")] = _status("success")
    pr = _pr(2, "head3")
    forge.reviews[("o/r", 2)] = [_review("head3", state="APPROVED")]
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert (state, troubles) == (None, [])


def test_a_review_from_someone_other_than_the_reviewer_does_not_count(st):
    forge = FakeForge()
    forge.statuses[("o/r", "head4")] = _status("success")
    pr = _pr(3, "head4")
    forge.reviews[("o/r", 3)] = [_review("head4", login="someone-else", state="APPROVED")]
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert state == "unreviewed"


def test_red_at_head_with_a_moved_and_green_base_is_stale_base(st):
    """Reproduces #415: red on one lane, base has moved on and is green
    there — the two-signal test, not a commit count."""
    forge = FakeForge()
    forge.statuses[("o/r", "head5")] = _status("failure")
    forge.statuses[("o/r", "mainsha-new")] = _status("success")
    pr = _pr(415, "head5", base_sha="mainsha-new", merge_base="mainsha-old")
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert (state, troubles) == ("stale-base", [])


def test_red_at_head_with_a_current_base_is_red_even_when_far_behind(st):
    """A branch can look far behind on its own commits while its actual base
    (the merge-base) has not moved at all — that must still read as `red`,
    never `stale-base`, regardless of how old the branch's own history is."""
    forge = FakeForge()
    forge.statuses[("o/r", "head6")] = _status("failure")
    pr = _pr(4, "head6", base_sha="mainsha0", merge_base="mainsha0")
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert (state, troubles) == ("red", [])


def test_red_at_head_with_a_moved_but_unconfirmed_base_is_red_not_stale_base(st):
    """The base has moved, but its own tip is NOT green (or is unreadable) —
    the safe direction is `red`, never guessing `stale-base`."""
    forge = FakeForge()
    forge.statuses[("o/r", "head7")] = _status("failure")
    forge.statuses[("o/r", "mainsha-new")] = _status("failure")
    pr = _pr(5, "head7", base_sha="mainsha-new", merge_base="mainsha-old")
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert (state, troubles) == ("red", [])


def test_red_at_head_with_an_unreadable_main_status_is_red_not_stale_base(st):
    forge = FakeForge()
    forge.statuses[("o/r", "head8")] = _status("failure")
    # no status wired for mainsha-new -> get_commit_status falls through to 404
    pr = _pr(6, "head8", base_sha="mainsha-new", merge_base="mainsha-old")
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert (state, troubles) == ("red", [])


def test_pending_ci_at_head_is_not_classified_and_is_not_a_trouble(st):
    forge = FakeForge()
    forge.statuses[("o/r", "head9")] = _status("pending")
    pr = _pr(7, "head9")
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert (state, troubles) == (None, [])


def test_an_unreadable_head_status_is_a_trouble_and_not_classified(st):
    forge = FakeForge()
    forge.statuses[("o/r", "head10")] = (500, None)
    pr = _pr(8, "head10")
    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert state is None
    assert troubles and troubles[0][0] == "status"


def test_an_unreadable_review_list_is_a_trouble_and_not_classified(st, monkeypatch):
    forge = FakeForge()
    forge.statuses[("o/r", "head11")] = _status("success")
    pr = _pr(9, "head11")

    def broken_list_reviews(repo, n, page=1, limit=50):
        return 500, None
    forge.list_reviews = broken_list_reviews

    troubles = []
    state, detail = st.classify_pr(forge, "o/r", pr, troubles)
    assert state is None
    assert troubles and troubles[0][0] == "reviews"


# ------------------------------------------------------------------- process_repo


def test_process_repo_reports_an_unreadable_pull_listing_as_a_trouble(st):
    forge = FakeForge()

    def broken_list_pulls(repo, state="open", page=1, limit=50, sort=None):
        return 500, None
    forge.list_pulls = broken_list_pulls

    troubles = []
    findings = st.process_repo(forge, "o/r", set(), troubles)
    assert findings == []
    assert troubles and troubles[0][0] == "pulls"


def test_a_pull_missing_its_head_is_skipped_and_troubled(st):
    forge = FakeForge()
    forge.pulls["o/r"] = [{"number": 10, "head": {}, "base": {}, "merge_base": None}]
    troubles = []
    findings = st.process_repo(forge, "o/r", set(), troubles)
    assert findings == []
    assert troubles and troubles[0][0] == "pulls"


def test_a_stewarded_pr_is_absent_from_the_report_and_never_queried(st):
    forge = FakeForge()
    forge.pulls["o/r"] = [_pr(11, "head12", head_ref="feat/live")]
    stewarded = {("o/r", "feat/live")}
    troubles = []
    findings = st.process_repo(forge, "o/r", stewarded, troubles)
    assert findings == []
    assert troubles == []
    assert not any(c[0] == "get_commit_status" for c in forge.calls), (
        "a stewarded PR must be skipped before any classification read")


def test_process_repo_reports_one_finding_per_stalled_pr(st):
    forge = FakeForge()
    forge.statuses[("o/r", "head13")] = _status("success")
    forge.pulls["o/r"] = [_pr(12, "head13", head_ref="feat/dead")]
    troubles = []
    findings = st.process_repo(forge, "o/r", set(), troubles)
    assert len(findings) == 1
    f = findings[0]
    assert (f["repo"], f["pr"], f["head_sha"], f["branch"], f["state"]) == (
        "o/r", 12, "head13", "feat/dead", "unreviewed")


# ------------------------------------------------------------------- stewardship


def _write_lease(tmp_path, lease_id, repo, branch, state="active", activated=None,
                 ttl_minutes=240, note=None):
    leases = tmp_path / ".fleet" / "leases"
    leases.mkdir(parents=True, exist_ok=True)
    activated = activated or datetime.now(timezone.utc)
    rec = {"id": lease_id, "state": state, "holder": "sess-1",
          "ttl_minutes": ttl_minutes,
          "activated": activated.strftime("%Y-%m-%dT%H:%M:%SZ"),
          "created": activated.strftime("%Y-%m-%dT%H:%M:%SZ"),
          "resource": {"type": "branch", "repo": repo, "branch": branch}}
    if note is not None:
        rec["note"] = note
    (leases / f"{lease_id}.json").write_text(json.dumps(rec))


def test_stewarded_branches_includes_a_live_active_lease(st, tmp_path):
    _write_lease(tmp_path, "branch--live--001", "o/r", "feat/live")
    assert ("o/r", "feat/live") in st.stewarded_branches()


def test_stewarded_branches_excludes_a_released_lease(st, tmp_path):
    _write_lease(tmp_path, "branch--done--001", "o/r", "feat/done", state="released")
    assert ("o/r", "feat/done") not in st.stewarded_branches()


def test_stewarded_branches_excludes_an_orphaned_lease(st, tmp_path):
    from datetime import datetime, timedelta, timezone
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_lease(tmp_path, "branch--stale--001", "o/r", "feat/stale",
                activated=old, ttl_minutes=5)
    assert ("o/r", "feat/stale") not in st.stewarded_branches()


def test_stewarded_branches_excludes_a_non_branch_resource(st, tmp_path):
    leases = tmp_path / ".fleet" / "leases"
    leases.mkdir(parents=True, exist_ok=True)
    rec = {"id": "paths--x--001", "state": "active", "holder": "sess-1",
          "ttl_minutes": 240,
          "activated": "2026-09-17T00:00:00Z", "created": "2026-09-17T00:00:00Z",
          "resource": {"type": "paths", "repo": "o/r"}}
    (leases / "paths--x--001.json").write_text(json.dumps(rec))
    assert not any(r == "o/r" for r, _ in st.stewarded_branches())


# ------------------------------------------------------------------------ action


def test_action_for_unreviewed_names_the_review_dispatcher(st):
    action = st.action_for("unreviewed", "o/r", 7, "feat/x")
    assert "o/r" in action and "7" in action and "--post" in action and "--no-followups" in action


def test_action_for_stale_base_names_a_rebase_and_nothing_automated(st):
    action = st.action_for("stale-base", "o/r", 7, "feat/x")
    assert "rebase" in action
    assert "feat/x" in action


def test_action_for_red_has_no_command(st):
    action = st.action_for("red", "o/r", 7, "feat/x")
    assert "investigate" in action


def test_compose_page_keeps_the_action_line_whole_even_when_the_detail_is_long(st, monkeypatch):
    # action_for()'s "unreviewed"/"stale-review" branch names orch.review_command(),
    # whose default is [sys.executable, ...]. The installed launchd unit invokes
    # this script with /usr/bin/python3 (launchd/org.eunomia.fleet-stalled.plist),
    # a short, fixed path — but under pytest, sys.executable is the checkout-local
    # venv interpreter, and this fleet's worktrees are named after their full
    # branch, so that path can itself run past PAGE_MAX_CHARS before `detail` is
    # even considered. Pin it to what production actually runs, so this test
    # exercises the budgeting logic instead of the length of the checkout path.
    monkeypatch.setattr(st.orch, "review_command", lambda: ["/usr/bin/python3", "/x/dispatch-review.py"])
    finding = {"repo": "o/r", "pr": 7, "state": "unreviewed", "branch": "feat/x",
              "head_sha": "a" * 40,
              "detail": "x" * 500}
    page = st.compose_page(finding)
    action = st.action_for("unreviewed", "o/r", 7, "feat/x")
    assert page.endswith("\n" + action)
    assert len(page) <= st.PAGE_MAX_CHARS + 1  # +1 for the newline joining the two lines


# ---------------------------------------------------------------------------- run


def _repos_conf(tmp_path, *repos):
    p = tmp_path / "repos.conf"
    p.write_text("".join(f"repo: {r} dispatch\n" for r in repos))
    return p


def _stalled_conf(tmp_path, **modes):
    p = tmp_path / "stalled.conf"
    p.write_text("".join(f"{k} = {v}\n" for k, v in modes.items()))
    return p


def _args(repos_conf, stalled_conf, dry_run=False):
    return argparse.Namespace(dry_run=dry_run, conf=str(stalled_conf),
                              repos_conf=str(repos_conf))


def test_run_pages_once_per_head_and_again_on_a_new_head(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)

    forge.statuses[("o/r", "head1")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head1", head_ref="feat/x")]

    repos_conf = _repos_conf(tmp_path, "o/r")
    stalled_conf = _stalled_conf(tmp_path, default="report")
    args = _args(repos_conf, stalled_conf)

    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1

    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1, "a cycle over an unchanged head must not page again"

    forge.statuses[("o/r", "head2")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head2", head_ref="feat/x")]
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 2, "a new head is a new page"


def test_run_dispatch_mode_refuses_and_does_not_fall_back_to_report(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)

    forge.statuses[("o/r", "head1")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head1", head_ref="feat/x")]

    repos_conf = _repos_conf(tmp_path, "o/r")
    stalled_conf = _stalled_conf(tmp_path, **{"o/r": "dispatch"})
    args = _args(repos_conf, stalled_conf)

    assert st.run(args) == st.EXIT_UNKNOWN
    # The refusal itself pages (like mopsus's "not built yet" refusal) — what
    # must never happen is the PR being scanned and reported as if the mode
    # were 'report'.
    assert [title for title, _ in _wire] == ["fleet-stalled: mode"], (
        "dispatch is not built — it must refuse loudly, never silently report")
    assert not any(c[0] == "list_pulls" for c in forge.calls), (
        "a refused repo must not be scanned at all")


def test_run_off_mode_skips_the_repo_entirely(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)

    forge.statuses[("o/r", "head1")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head1", head_ref="feat/x")]

    repos_conf = _repos_conf(tmp_path, "o/r")
    stalled_conf = _stalled_conf(tmp_path, **{"o/r": "off"})
    args = _args(repos_conf, stalled_conf)

    assert st.run(args) == st.EXIT_OK
    assert _wire == []
    assert not any(c[0] == "list_pulls" for c in forge.calls)


def test_run_excludes_a_pr_under_an_active_lease(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)

    forge.statuses[("o/r", "head1")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head1", head_ref="feat/live")]
    _write_lease(tmp_path, "branch--live--001", "o/r", "feat/live")

    repos_conf = _repos_conf(tmp_path, "o/r")
    stalled_conf = _stalled_conf(tmp_path, default="report")
    args = _args(repos_conf, stalled_conf)

    assert st.run(args) == st.EXIT_OK
    assert _wire == []


def test_run_dry_run_writes_no_state_and_pages_nothing(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)

    forge.statuses[("o/r", "head1")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head1", head_ref="feat/x")]

    repos_conf = _repos_conf(tmp_path, "o/r")
    stalled_conf = _stalled_conf(tmp_path, default="report")
    args = _args(repos_conf, stalled_conf, dry_run=True)

    assert st.run(args) == st.EXIT_STALLED
    assert _wire == []
    assert not st._state_path().exists()


def test_run_returns_unknown_on_an_unreadable_pull_listing(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()

    def broken_list_pulls(repo, state="open", page=1, limit=50, sort=None):
        return 500, None
    forge.list_pulls = broken_list_pulls
    monkeypatch.setattr(st, "_forge", lambda token: forge)

    repos_conf = _repos_conf(tmp_path, "o/r")
    stalled_conf = _stalled_conf(tmp_path, default="report")
    args = _args(repos_conf, stalled_conf)

    assert st.run(args) == st.EXIT_UNKNOWN


def test_run_returns_ok_when_nothing_is_stalled(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    forge.statuses[("o/r", "head1")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head1", head_ref="feat/x")]
    forge.reviews[("o/r", 1)] = [_review("head1", state="APPROVED")]
    monkeypatch.setattr(st, "_forge", lambda token: forge)

    repos_conf = _repos_conf(tmp_path, "o/r")
    stalled_conf = _stalled_conf(tmp_path, default="report")
    args = _args(repos_conf, stalled_conf)

    assert st.run(args) == st.EXIT_OK
    assert _wire == []


# --- held (plan 0170) ----------------------------------------------------------

WAITS = "plan 0002-b waits on 0001-a: not done on main"


class HoldForge(FakeForge):
    def __init__(self):
        super().__init__()
        self.deps_statuses = {}          # sha -> [status, ...] newest first

    def list_statuses(self, repo, sha, page=1, limit=50):
        if page > 1:
            return 200, []
        return 200, self.deps_statuses.get(sha, [])


def _ctx(context, status):
    return {"context": context, "status": status}


def _combined(state, *ctxs):
    return 200, {"state": state, "statuses": list(ctxs)}


def _deps_status(status, desc, i=1):
    return {"id": i, "context": "eunomia/deps", "status": status, "description": desc,
            "created_at": "2026-10-27T10:00:00Z", "creator": {"login": "mergebot"}}


def _classify(st, forge, head="h1"):
    troubles = []
    got = st.classify_pr(forge, "o/r", _pr(1, head), troubles)
    return got, troubles


def test_red_only_on_deps_with_waits_on_is_held(st):
    f = HoldForge()
    f.statuses[("o/r", "h1")] = _combined("failure", _ctx("eunomia/deps", "failure"))
    f.deps_statuses["h1"] = [_deps_status("failure", WAITS)]
    (state, detail), troubles = _classify(st, f)
    assert (state, detail) == ("held", WAITS) and not troubles


def test_red_only_on_deps_other_failure_is_red(st):
    f = HoldForge()
    f.statuses[("o/r", "h1")] = _combined("failure", _ctx("eunomia/deps", "failure"))
    f.deps_statuses["h1"] = [_deps_status("failure", "plan id 'x' is not a plan id")]
    (state, _), _t = _classify(st, f)
    assert state == "red"


def test_deps_pending_alone_and_unreviewed_pages_without_trouble(st):
    f = HoldForge()
    f.statuses[("o/r", "h1")] = _combined("pending", _ctx("eunomia/deps", "pending"))
    (state, _), troubles = _classify(st, f)
    assert state == "unreviewed" and not troubles


def test_deps_and_another_context_red_is_red(st):
    f = HoldForge()
    f.statuses[("o/r", "h1")] = _combined(
        "failure", _ctx("eunomia/deps", "failure"), _ctx("ci/test", "failure"))
    f.deps_statuses["h1"] = [_deps_status("failure", WAITS)]
    (state, _), _t = _classify(st, f)
    assert state == "red"


def test_another_context_rerun_green_with_deps_failing_is_held(st):
    f = HoldForge()
    # the combined endpoint is de-duplicated: only the latest ci/test is shown
    f.statuses[("o/r", "h1")] = _combined(
        "failure", _ctx("eunomia/deps", "failure"), _ctx("ci/test", "success"))
    f.deps_statuses["h1"] = [_deps_status("failure", WAITS)]
    (state, _), _t = _classify(st, f)
    assert state == "held"


def test_plan_pr_with_green_ci_and_no_review_is_unreviewed(st):
    f = HoldForge()
    f.statuses[("o/r", "h1")] = _combined(
        "success", _ctx("eunomia/deps", "success"), _ctx("ci/test", "success"))
    (state, _), _t = _classify(st, f)
    assert state == "unreviewed"


def test_unreadable_deps_statuses_is_a_trouble(st):
    f = HoldForge()
    f.statuses[("o/r", "h1")] = _combined("failure", _ctx("eunomia/deps", "failure"))
    f.list_statuses = lambda *a, **k: (500, None)
    (state, _), troubles = _classify(st, f)
    assert state is None and troubles


def test_held_is_never_paged_and_clearing_pages_once(st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    f = HoldForge()
    monkeypatch.setattr(st, "_forge", lambda token: f)
    f.statuses[("o/r", "h1")] = _combined("failure", _ctx("eunomia/deps", "failure"))
    f.deps_statuses["h1"] = [_deps_status("failure", WAITS)]
    f.pulls["o/r"] = [_pr(1, "h1")]
    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))

    assert st.run(args) == st.EXIT_OK
    assert st.run(args) == st.EXIT_OK
    assert _wire == []

    # the hold clears on the same head; the PR is unreviewed and pages once
    f.statuses[("o/r", "h1")] = _combined("success", _ctx("eunomia/deps", "success"))
    assert st.run(args) == st.EXIT_STALLED
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1


# --- failed-plan (plan 0194) --------------------------------------------------


def _write_dispatch_db(tmp_path, rows):
    """A `dispatch` TABLE, not the real VIEW — `newest_dispatch`'s query reads
    columns, not the view-ness of the thing holding them, and a table is a
    plain `CREATE TABLE` + `INSERT` away."""
    db_path = tmp_path / ".fleet" / "fleet.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.execute("""CREATE TABLE dispatch (
        lease TEXT, repo TEXT, plan_id TEXT, outcome TEXT, terminal INTEGER,
        failure_reason TEXT, log_path TEXT, current_phase TEXT,
        current_phase_ts TEXT)""")
    for r in rows:
        con.execute(
            "INSERT INTO dispatch (lease, repo, plan_id, outcome, terminal, "
            "failure_reason, log_path, current_phase, current_phase_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (r["lease"], r["repo"], r["plan_id"], r.get("outcome"),
             int(r.get("terminal", 1)), r.get("failure_reason"),
             r.get("log_path"), r.get("current_phase"), r.get("current_phase_ts")))
    con.commit()
    con.close()
    return db_path


def _plan_fm(plan_id, status="ready"):
    return {"id": plan_id, "status": status, "paths": [], "depends_on": []}


def _ts(days_ago=1):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _dispatch_row(repo="o/r", plan_id="0001-x", lease="branch--x--001",
                  outcome="failed", terminal=1, current_phase_ts=None,
                  failure_reason="boom", current_phase="implementer-exit"):
    return {"repo": repo, "plan_id": plan_id, "lease": lease, "outcome": outcome,
           "terminal": terminal, "failure_reason": failure_reason,
           "log_path": "/x/run.log", "current_phase": current_phase,
           "current_phase_ts": current_phase_ts or _ts()}


def _wire_failed_plan_run(st, monkeypatch, plans=(("0001-x", "ready"),), marked_pr=None):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)
    monkeypatch.setattr(
        st.fwatch, "plan_files",
        lambda repo, token: [(f"plans/{pid}.md", _plan_fm(pid, status))
                             for pid, status in plans])
    monkeypatch.setattr(st.fwatch, "marked_pr", lambda repo, plan_id, token: marked_pr)
    return forge


# ---------------------------------------------------------------- newest_dispatch


def test_newest_dispatch_picks_the_row_with_the_latest_phase_ts(st, tmp_path):
    db_path = _write_dispatch_db(tmp_path, [
        _dispatch_row(lease="branch--x--001", current_phase_ts=_ts(5)),
        _dispatch_row(lease="branch--x--002", current_phase_ts=_ts(1)),
    ])
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = st.newest_dispatch(con, "o/r", "0001-x")
    assert row["lease"] == "branch--x--002"


def test_newest_dispatch_is_none_for_an_undispatched_plan(st, tmp_path):
    db_path = _write_dispatch_db(tmp_path, [_dispatch_row(plan_id="0001-x")])
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    assert st.newest_dispatch(con, "o/r", "0002-never-dispatched") is None


# ------------------------------------------------------------- action_for_failed_plan


def test_action_for_failed_plan_with_no_pr_names_fleet_requeue(st):
    finding = {"repo": "o/r", "plan": "0001-x", "pr": None, "pr_state": None}
    assert st.action_for_failed_plan(finding) == "fleet-requeue o/r 0001-x"


def test_action_for_failed_plan_with_an_open_pr_names_respawn(st):
    finding = {"repo": "o/r", "plan": "0001-x", "pr": 42, "pr_state": "open"}
    action = st.action_for_failed_plan(finding)
    assert "respawn" in action and "o/r#42" in action


def test_action_for_failed_plan_with_a_closed_pr_names_the_status_decision(st):
    finding = {"repo": "o/r", "plan": "0014-x", "pr": 102, "pr_state": "closed"}
    action = st.action_for_failed_plan(finding)
    assert "o/r#102 is closed" in action
    assert "fleet-requeue o/r" not in action


# -------------------------------------------------------------------- run(): failed-plan


def test_a_failed_plan_with_no_marked_pr_is_named_with_fleet_requeue(
        st, monkeypatch, tmp_path, _wire):
    _wire_failed_plan_run(st, monkeypatch, marked_pr=None)
    _write_dispatch_db(tmp_path, [_dispatch_row()])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1
    title, body = _wire[0]
    assert title == "fleet-stalled: failed-plan"
    assert "fleet-requeue o/r 0001-x" in body


def test_a_failed_plan_with_an_open_pr_and_a_dead_steward_is_named_with_respawn(
        st, monkeypatch, tmp_path, _wire):
    pr = {"number": 42, "state": "open", "head": {"ref": "feat/x-r2"}}
    _wire_failed_plan_run(st, monkeypatch, marked_pr=pr)
    _write_dispatch_db(tmp_path, [_dispatch_row()])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1
    title, body = _wire[0]
    assert "respawn" in body and "o/r#42" in body


def test_a_failed_plan_with_an_open_pr_and_a_live_steward_is_not_named(
        st, monkeypatch, tmp_path, _wire):
    pr = {"number": 42, "state": "open", "head": {"ref": "feat/x-r2"}}
    _wire_failed_plan_run(st, monkeypatch, marked_pr=pr)
    _write_dispatch_db(tmp_path, [_dispatch_row()])
    _write_lease(tmp_path, "branch--steward--001", "o/r", "feat/x-r2")

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_OK
    assert _wire == []


@pytest.mark.parametrize("outcome", ["closed", "failed"])
def test_a_ready_plan_whose_marked_pr_is_closed_is_named_with_the_status_decision(
        st, monkeypatch, tmp_path, _wire, outcome):
    pr = {"number": 102, "state": "closed", "head": {"ref": "feat/x"}}
    _wire_failed_plan_run(st, monkeypatch, plans=(("0014-x", "ready"),), marked_pr=pr)
    _write_dispatch_db(tmp_path, [_dispatch_row(plan_id="0014-x", outcome=outcome)])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1
    title, body = _wire[0]
    assert "o/r#102 is closed" in body
    assert "fleet-requeue o/r" not in body


def test_a_plan_with_a_live_lease_is_not_named(st, monkeypatch, tmp_path, _wire):
    _wire_failed_plan_run(st, monkeypatch, marked_pr=None)
    _write_dispatch_db(tmp_path, [_dispatch_row()])
    _write_lease(tmp_path, "branch--x--001", "o/r", "feat/x-r2", note="plan:0001-x")

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_OK
    assert _wire == []


def test_a_plan_with_only_a_dead_orphaned_lease_is_still_named(
        st, monkeypatch, tmp_path, _wire):
    """D1's liveness check is fleetlib's TTL/heartbeat verdict, never the
    lease's `state` field: an `active` lease whose orchestrator stopped
    heartbeating is not alive, and must not hide a genuinely failed plan."""
    _wire_failed_plan_run(st, monkeypatch, marked_pr=None)
    _write_dispatch_db(tmp_path, [_dispatch_row()])
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    _write_lease(tmp_path, "branch--x--001", "o/r", "feat/x-r2",
                note="plan:0001-x", activated=old, ttl_minutes=5)

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1


@pytest.mark.parametrize("status", ["done", "abandoned"])
def test_a_plan_not_status_ready_is_not_named(st, monkeypatch, tmp_path, _wire, status):
    checked = []
    _wire_failed_plan_run(st, monkeypatch, plans=(("0001-x", status),))
    monkeypatch.setattr(st.fwatch, "marked_pr",
                        lambda repo, plan_id, token: checked.append(1))
    _write_dispatch_db(tmp_path, [_dispatch_row()])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_OK
    assert _wire == []
    assert checked == [], "a plan not status: ready must not even cost a marked-PR read"


def test_a_plan_whose_newest_dispatch_merged_is_not_named(st, monkeypatch, tmp_path, _wire):
    _wire_failed_plan_run(st, monkeypatch, marked_pr=None)
    _write_dispatch_db(tmp_path, [_dispatch_row(outcome="merged")])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_OK
    assert _wire == []


def test_a_second_run_on_the_same_utc_day_is_silent_and_the_next_day_repages(
        st, monkeypatch, tmp_path, _wire):
    _wire_failed_plan_run(st, monkeypatch, marked_pr=None)
    _write_dispatch_db(tmp_path, [_dispatch_row()])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1

    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1, "a second run the same UTC day must stay silent"

    data = json.loads(st._state_path().read_text())
    for p in data["pages"]:
        if p.get("state") == st.FAILED_PLAN:
            p["day"] = "2000-01-01"
    st._state_path().write_text(json.dumps(data))

    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 2, "a new UTC day re-pages"


def test_a_new_dispatch_lease_stops_the_page(st, monkeypatch, tmp_path, _wire):
    _wire_failed_plan_run(st, monkeypatch, marked_pr=None)
    _write_dispatch_db(tmp_path, [_dispatch_row()])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_STALLED
    assert len(_wire) == 1

    _write_lease(tmp_path, "branch--x--002", "o/r", "feat/x-r2", note="plan:0001-x")
    assert st.run(args) == st.EXIT_OK
    assert len(_wire) == 1, "a live dispatch lease stops the page entirely"


def test_an_undelivered_page_is_retried_then_stays_silent_once_delivered(
        st, monkeypatch, tmp_path, _wire):
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)
    forge.statuses[("o/r", "head1")] = _status("success")
    forge.pulls["o/r"] = [_pr(1, "head1", head_ref="feat/x")]

    outcomes = [False, True]
    monkeypatch.setattr(st.orch, "notify", lambda title, body: outcomes.pop(0))

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))

    assert st.run(args) == st.EXIT_STALLED
    data = json.loads(st._state_path().read_text())
    assert data["pages"][0]["delivered"] is False

    assert st.run(args) == st.EXIT_STALLED
    data = json.loads(st._state_path().read_text())
    assert len(data["pages"]) == 1
    assert data["pages"][0]["delivered"] is True

    def fail_notify(title, body):
        raise AssertionError("must not re-notify once delivered")
    monkeypatch.setattr(st.orch, "notify", fail_notify)
    assert st.run(args) == st.EXIT_STALLED


def test_a_repo_with_no_plans_tree_is_not_a_trouble(st, monkeypatch, tmp_path, _wire):
    """`plan_files` returning None (Forgejo 404s a repo with no `plans/` tree
    exactly like a genuine read failure) must not page — most of
    `config/repos.conf` is not plan-driven, and `bin/fleet-watch`'s own
    `cycle()` treats this identically, a warn and a skip, never a page."""
    monkeypatch.setattr(st, "_token", lambda: ("tok", ""))
    forge = FakeForge()
    monkeypatch.setattr(st, "_forge", lambda token: forge)
    monkeypatch.setattr(st.fwatch, "plan_files", lambda repo, token: None)
    _write_dispatch_db(tmp_path, [])

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_OK
    assert _wire == []


def test_a_missing_fleet_db_is_not_a_trouble_and_finds_nothing(
        st, monkeypatch, tmp_path, _wire):
    """plan 0036's `dispatch` view never installed on this host is a normal
    state (mirrors operator/lynceus's own read of it), never a scan trouble."""
    _wire_failed_plan_run(st, monkeypatch, marked_pr=None)
    # no fleet.db written at all

    args = _args(_repos_conf(tmp_path, "o/r"), _stalled_conf(tmp_path, default="report"))
    assert st.run(args) == st.EXIT_OK
    assert _wire == []
