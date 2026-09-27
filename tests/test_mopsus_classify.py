"""mopsus classifies a failure and shortlists actions for it (plan 0064).

Every reason string below is taken verbatim from a real brief in
~/dev/.fleet/mopsus/briefs/ (2026-09-19), one test per class, so a rule that
drifts from what the orchestrator actually writes fails here rather than at 3am.
"""
import json
import re

import pytest

from test_mopsus import _db, _exit_phase, _failed_event, _start_phase, _wire, fc, mp  # noqa: F401

REASONS = {
    "work-exists-unverified": [
        "no verified marked PR: #470: plans/0063-the-configuration-has-a-second-copy.md "
        "still says status: ready (session exit 0)",
        "no verified marked PR: #162 carries no 'Plan: 0002-voice-tile-actually-listens' "
        "line — an unmarked PR leaves the plan re-dispatchable with its work in a "
        "branch nobody will look at (session exit 0)",
        "no verified marked PR: no pull request with head feat/0012-session-pid-binding "
        "(session exit 0)",
        "no verified marked PR: could not read operator/eunomia pull requests (failed) "
        "after 5 attempts over 10 minutes (session exit resumed)",
        "round 1: fix session left the head at 970622c9 — nothing was pushed",
    ],
    "backend-before-work": [
        "session ended on a backend error before any work: API 500 after 5s — "
        "API Error: 500 Internal server error",
    ],
    "quota": [
        "session ended on a backend error before any work: API 429 after 5s — "
        "You have reached your monthly spend limit",
        "session ended on a backend error before any work: API 400 after 5s — "
        "usage limit reached",
    ],
    "backend-after-work": [
        "session ended on a backend error after work began: API 500 after 1200s — "
        "API Error: 500 Internal server error",
    ],
    "transport": [
        "workspace setup failed: RuntimeError: clone failed: ssh: connect to host "
        "forge.example port 3022: Undefined error: 0\nfatal: Could not read from "
        "remote repository.\n\nPlease make sure you have the correct access rights\n"
        "and the repository exists.",
        "workspace setup failed: RuntimeError: git fetch --quiet failed: ssh: connect "
        "to host forge.example port 3022: Undefined error: 0\nfatal: Could not read "
        "from remote repository.",
    ],
    "timeout": [
        "implementer timed out after 90 min; process group killed",
        "round 1: fix session timed out after 90 min",
    ],
    "plan-text-refused": [
        "plan text carries credential shapes ['private-key'] — refusing to build a "
        "prompt from it",
    ],
    "branch-collision": [
        "could not create branch feat/0001-home-surface-goals-critical-calenda: fatal: "
        "a branch named 'feat/0001-home-surface-goals-critical-calenda' already exists",
    ],
    "host-missing-tool": [
        "runner 'sub-sonnet' needs 'claude', which is not on PATH on this host — "
        "refusing to spawn",
    ],
    "raced-ci": [
        "round 2: review dispatch failed: REFUSING: CI is still running on c0d0a28c — "
        "wait for it rather than review a commit that may not pass. (--ignore-ci to "
        "override.)",
    ],
}
ALL = [(cls, r) for cls, rs in REASONS.items() for r in rs]

# The one real reason that is deliberately NOT stretched into a class: the forge
# answered and refused the key, so a retry fails identically.
PUBLICKEY = ("workspace setup failed: RuntimeError: clone failed: "
             "implbot@forge.example: Permission denied (publickey).\nfatal: Could "
             "not read from remote repository.")


def _row(reason, pr=None, repo="operator/x", lease="branch--x-0001--001"):
    return {"lease": lease, "repo": repo, "plan_id": "0001-x", "pr": pr,
            "failure_reason": reason, "failure_log": None,
            "failure_transcript": None, "rc": None, "timed_out": None,
            "seconds": None}


def _names(view):
    return [a["action"] for a in view["actions"]]


# --- D1: one test per class -------------------------------------------------


@pytest.mark.parametrize("cls", sorted(REASONS))
def test_every_real_reason_of_a_class_is_classified_as_it(mp, cls):
    for reason in REASONS[cls]:
        assert mp.classify(reason) == cls, reason


def test_every_class_in_the_actions_table_has_a_test_and_a_rule(mp):
    ruled = {name for name, _ in mp.CLASS_RULES}
    assert ruled == set(REASONS)
    assert set(mp.CLASS_ACTIONS) == ruled | {"unclassified"}


def test_a_reason_matching_nothing_is_unclassified_and_the_brief_prints_it(mp):
    reason = "quantum flux exceeded the flange tolerance on lease 7"
    assert mp.classify(reason) == "unclassified"
    assert mp.classify(None) == "unclassified"
    row = dict(_row(reason), **{"class": "unclassified"})
    brief = mp.compose_brief(row)
    assert "class: unclassified" in brief
    assert f"raw reason: {reason}" in brief


def test_a_refused_key_is_unclassified_not_stretched_into_transport(mp):
    assert mp.classify(PUBLICKEY) == "unclassified"


# --- D3: open-pr first when a PR exists; retry first for bare transport ------


@pytest.mark.parametrize("cls,reason", ALL)
def test_a_failure_with_a_pr_leads_with_open_pr_carrying_the_number(mp, cls, reason):
    view = mp.failure_view(_row(reason, pr=443, repo="operator/eunomia"))
    assert _names(view)[0] == "open-pr"
    assert "retry" not in _names(view)[:1]
    first = view["actions"][0]
    assert first["pr"] == 443 and view["pr"] == 443
    assert "#443" in first["cost"]
    # A UNIVERSAL LINK, not a scheme: `minos://` resolves to nothing and a
    # phone tapping it gets silence. The host and the /pr/ path are what the
    # app-site-association at minospr.app actually claims.
    assert first["links"]["minos"] == (
        "https://minospr.app/pr/operator/eunomia/443")
    assert not first["links"]["minos"].startswith("minos://")
    assert first["links"]["forge"].endswith("/operator/eunomia/pulls/443")


def test_open_pr_carries_a_universal_minos_link_and_the_forge_url(mp):
    links = mp.failure_view(_row(REASONS["timeout"][1], pr=9))["actions"][0]["links"]
    assert set(links) == {"minos", "forge"}
    assert links["minos"] != links["forge"]


def test_a_transport_failure_leads_with_retry(mp):
    view = mp.failure_view(_row(REASONS["transport"][0]))
    assert view["class"] == "transport"
    assert _names(view)[0] == "retry"
    assert "open-pr" not in _names(view)


def test_the_largest_two_classes_want_opposite_first_actions(mp):
    work = mp.failure_view(_row(REASONS["work-exists-unverified"][0], pr=470))
    transport = mp.failure_view(_row(REASONS["transport"][0]))
    assert _names(work)[0] == "open-pr" and _names(transport)[0] == "retry"


def test_work_exists_without_a_pr_looks_at_the_branch_before_retrying(mp):
    view = mp.failure_view(_row(REASONS["work-exists-unverified"][2]))
    assert _names(view)[:2] == ["check-branch", "retry"]


def test_timeout_never_offers_a_plain_retry(mp):
    names = _names(mp.failure_view(_row(REASONS["timeout"][0])))
    assert names == ["retry-longer-bound", "split-plan"]


def test_every_action_names_what_it_costs(mp):
    for cls in mp.CLASS_ACTIONS:
        for a in mp.failure_view(_row("x", pr=1) | {"class": cls})["actions"]:
            assert a["cost"].strip(), (cls, a)


# --- Boundary: no action changes a plan's status ------------------------------

STATUS_CHANGE = re.compile(
    r"abandon|status\s*[:=]|set[- ]status|flip|mark[- ]done|\bdone\b|\bready\b", re.I)


def test_no_action_in_any_shortlist_changes_a_plans_status(mp):
    for cls in mp.CLASS_ACTIONS:
        for pr in (None, 7):
            view = mp.failure_view(_row("x", pr=pr) | {"class": cls})
            for a in view["actions"]:
                assert not STATUS_CHANGE.search(a["action"]), (cls, a)
                assert not STATUS_CHANGE.search(a["cost"]), (cls, a)


# --- Boundary: classify from failure_reason only ------------------------------


def test_classification_opens_no_log_or_transcript(mp, monkeypatch, tmp_path):
    absent_log = tmp_path / "gone" / "run.log"
    absent_tx = tmp_path / "gone" / "transcript.log"
    opened = []
    real_open = type(absent_log).open
    monkeypatch.setattr(type(absent_log), "open",
                        lambda self, *a, **k: (opened.append(str(self)),
                                               real_open(self, *a, **k))[1])
    for cls, reason in ALL:
        row = dict(_row(reason, pr=5), failure_log=str(absent_log),
                   failure_transcript=str(absent_tx))
        view = mp.failure_view(row)
        assert view["class"] == cls
    assert mp.classify("anything at all") == "unclassified"
    assert opened == []
    assert not absent_log.parent.exists()


# --- D4: the text brief, and --format json ------------------------------------


def test_the_text_brief_is_byte_identical_for_a_row_that_has_no_class(mp):
    row = _row("boom")
    assert "class" not in row
    brief = mp.compose_brief(row)
    assert "--- class" not in brief
    assert brief == (
        "Dispatch failure: operator/x 0001-x\n"
        "lease:      branch--x-0001--001\n"
        "branch:     -\n"
        "reason:     boom\n"
        "rc:         -\n"
        "timed_out:  -\n"
        "seconds:    -\n"
        "pr:         -\n"
        "log:        -\n"
        "transcript: -\n"
        "\n--- tail of run log (15 lines) ---\n(no run log recorded)\n"
    )


def test_a_classed_brief_keeps_its_old_shape_and_appends_the_shortlist(mp):
    plain = mp.compose_brief(_row(REASONS["timeout"][0]))
    classed = mp.compose_brief(dict(_row(REASONS["timeout"][0]), **{"class": "timeout"}))
    assert classed.startswith(plain)
    assert "--- class: timeout ---" in classed
    assert "1. retry-longer-bound" in classed


def test_a_swept_brief_carries_class_and_the_pr_links(mp, fc, tmp_path, _wire):
    lease = "branch--x-0020--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/eunomia", "0020-x"),
              _exit_phase(lease, 0, 0, 1063)],
             [_failed_event(lease, REASONS["work-exists-unverified"][0])])
    mp.sweep(db=db)
    handle = mp._load_state()["leases"][lease]["handle"]
    brief = mp._read_brief(handle)
    assert "--- class: work-exists-unverified ---" in brief


def test_format_json_carries_class_actions_and_pr(mp, fc, tmp_path, _wire, capsys):
    lease = "branch--x-0021--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/loyalty", "0002-x"),
              _exit_phase(lease, 0, 0, 91, seq=2),
              ("branch--x-0021--001", 3, "2026-09-13T01:00:00Z", "review-start", 81,
               None, None, None, None, None, None, "{}", f"{lease}.log", 3)],
             [_failed_event(lease, REASONS["work-exists-unverified"][4])])
    mp.sweep(db=db)
    handle = mp._load_state()["leases"][lease]["handle"]
    capsys.readouterr()

    assert mp.main([handle, "--format", "json", "--db", str(db)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["class"] == "work-exists-unverified"
    assert data["pr"] == 81
    assert data["handle"] == handle
    assert data["actions"][0]["action"] == "open-pr"
    assert data["actions"][0]["pr"] == 81
    assert data["actions"][0]["links"]["forge"].endswith("/operator/loyalty/pulls/81")


def test_format_json_answers_for_a_handle_issued_before_classes_existed(
        mp, fc, tmp_path, _wire, capsys):
    lease = "branch--x-0022--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/ares", "0001-x"),
              _exit_phase(lease, 143, 1, 5400)],
             [_failed_event(lease, REASONS["timeout"][0])])
    mp._save_state({"next_id": 2, "leases": {lease: {"handle": "h1"}}})
    assert mp.main(["h1", "--format", "json", "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["class"] == "timeout"


def test_format_json_on_an_unknown_handle_fails_cleanly(mp, tmp_path, capsys):
    assert mp.main(["h404", "--format", "json", "--db", str(tmp_path / "no.db")]) == 1
    assert "h404" in capsys.readouterr().err


def test_format_json_without_a_handle_is_a_usage_error(mp):
    with pytest.raises(SystemExit) as e:
        mp.main(["--format", "json"])
    assert e.value.code == 2


def test_format_json_never_opens_a_session(mp, fc, tmp_path, _wire, monkeypatch, capsys):
    lease = "branch--x-0023--001"
    db = _db(fc, tmp_path,
             [_start_phase(lease, "operator/ares", "0001-x"),
              _exit_phase(lease, 143, 1, 5400)],
             [_failed_event(lease, REASONS["timeout"][0])])
    mp._save_state({"next_id": 2, "leases": {lease: {"handle": "h1"}}})
    seen = []
    monkeypatch.setattr(mp, "_default_exec", lambda brief: seen.append(brief))
    mp.main(["h1", "--format", "json", "--db", str(db)])
    assert seen == []


# --- plan 0079: the model backend classes -----------------------------------


def test_only_the_before_work_backend_class_is_retryable_and_quota_is_not(mp):
    assert "backend-before-work" in mp.RETRYABLE_CLASSES
    assert "quota" not in mp.RETRYABLE_CLASSES
    assert "backend-after-work" not in mp.RETRYABLE_CLASSES


def test_the_backend_shortlists_lead_as_specified(mp):
    def first(cls):
        return [a for a, _ in mp.CLASS_ACTIONS[cls]]
    assert first("backend-before-work") == ["retry"]
    assert first("quota") == ["wait-for-reset", "retry"]
    assert first("backend-after-work") == ["check-branch", "retry"]


# --- eunomia#559: the backend rules must claim their reason BEFORE transport
# or timeout ever see it, since plan 0079's reason strings embed a free-text
# excerpt of whatever the backend said, and that excerpt can itself contain
# words the looser rules match on. -----------------------------------------


def test_a_backend_error_whose_excerpt_mentions_a_reset_is_still_backend_after_work(mp):
    reason = ("session ended on a backend error after work began: API 500 "
             "after 30s — Connection reset by peer")
    assert mp.classify(reason) == "backend-after-work"


def test_a_backend_error_whose_excerpt_mentions_a_timeout_is_still_backend_before_work(mp):
    reason = ("session ended on a backend error before any work: API 500 "
             "after 5s — Connection timed out")
    assert mp.classify(reason) == "backend-before-work"


def test_a_plain_transport_failure_with_no_backend_wrapper_is_still_transport(mp):
    assert mp.classify("ssh: connect to host forge.example port 3022: "
                       "Connection refused") == "transport"


def test_a_429_before_work_reason_mentioning_connection_refused_is_still_quota(mp):
    reason = ("session ended on a backend error before any work: API 429 "
             "after 5s — Connection refused while checking your monthly "
             "spend limit")
    assert mp.classify(reason) == "quota"
