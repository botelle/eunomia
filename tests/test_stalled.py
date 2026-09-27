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
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ldr))
    ldr.exec_module(mod)
    return mod


@pytest.fixture
def st():
    return _load("fleet_stalled", BIN / "fleet-stalled")


@pytest.fixture(autouse=True)
def _wire(st, monkeypatch, tmp_path):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / ".fleet"))
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
                 ttl_minutes=240):
    from datetime import datetime, timedelta, timezone
    leases = tmp_path / ".fleet" / "leases"
    leases.mkdir(parents=True, exist_ok=True)
    activated = activated or datetime.now(timezone.utc)
    rec = {"id": lease_id, "state": state, "holder": "sess-1",
          "ttl_minutes": ttl_minutes,
          "activated": activated.strftime("%Y-%m-%dT%H:%M:%SZ"),
          "created": activated.strftime("%Y-%m-%dT%H:%M:%SZ"),
          "resource": {"type": "branch", "repo": repo, "branch": branch}}
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
