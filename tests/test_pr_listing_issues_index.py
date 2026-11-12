"""Plan 0218 — the marked-PR check reads the issues index, not `/pulls`.

Covers `_pull_pages`'s reshaping of a raw issues-index item, and the
order-independence `marked_pr`, `newest_closed_marked_pr` and
`_open_marked_plans` all need now that the walk is newest-created first
rather than `sort=oldest`."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleet_watch import _load  # noqa: E402

REPO = "operator/eunomia"


class StubForge:
    """`pages`: raw issues-index pages (1-based). A page past the end of the
    list reads as an empty (ended) page, matching a real forge's last page."""

    def __init__(self, pages, fail_on=None):
        self.pages = pages
        self.fail_on = fail_on
        self.calls = []
        self.pulls_by_number = {}

    def list_pull_issues(self, repo, state="all", page=1, limit=50):
        self.calls.append(page)
        if page == self.fail_on:
            return 500, None
        if page > len(self.pages):
            return 200, []
        return 200, self.pages[page - 1]

    def get_pull(self, repo, n):
        hit = self.pulls_by_number.get(n)
        return (200, hit) if hit is not None else (404, None)


def _issue(number, plan_id, state="open", merged=None, merged_at=None,
          uid=42, login="implbot"):
    """One raw issues-index item, exactly the shape `list_pull_issues`
    returns: `merged`/`merged_at` nested under `pull_request`, no `head`."""
    return {"number": number, "state": state,
            "user": {"id": uid, "login": login},
            "body": f"Plan: {plan_id}\n",
            "created_at": f"2026-01-{number:02d}T00:00:00Z",
            "updated_at": f"2026-02-{number:02d}T00:00:00Z",
            "closed_at": (None if state == "open"
                         else f"2026-03-{number:02d}T00:00:00Z"),
            "pull_request": {"merged": merged, "merged_at": merged_at}}


@pytest.fixture
def mod(tmp_path, monkeypatch):
    m = _load(tmp_path, FLEET_IMPLEMENTER_UIDS="42")
    monkeypatch.setattr(m, "_notify_once", lambda *a, **k: None)
    monkeypatch.setattr(m, "_clear_notified", lambda *a, **k: None)
    return m


def _install(mod, monkeypatch, forge):
    monkeypatch.setattr(mod, "_forge", lambda token: forge)
    return forge


def _paginate(items, size=2):
    return [items[i:i + size] for i in range(0, len(items), size)] or [[]]


# ------------------------------------------------- order independence (D2)

def test_marked_pr_agrees_across_page_order_preferring_the_open_one(mod, monkeypatch):
    """An older closed marked PR (#3) and a newer open one (#7): `marked_pr`
    must return the open one either way, exactly as `sort=oldest` did."""
    items = [_issue(3, "0009-x", state="closed", merged=False),
             _issue(5, "0009-x", state="closed", merged=True, merged_at="t"),
             _issue(7, "0009-x", state="open")]
    oldest_first = _install(mod, monkeypatch, StubForge(_paginate(items)))
    a = mod.marked_pr(REPO, "0009-x", "t")
    newest_first = _install(mod, monkeypatch,
                            StubForge(_paginate(list(reversed(items)))))
    b = mod.marked_pr(REPO, "0009-x", "t")
    assert a["number"] == b["number"] == 7


def test_marked_pr_agrees_across_page_order_with_no_open_pr(mod, monkeypatch):
    """No open PR: both orders must pick the SAME (oldest) closed/merged one."""
    items = [_issue(3, "0009-x", state="closed", merged=False),
             _issue(5, "0009-x", state="closed", merged=True, merged_at="t")]
    _install(mod, monkeypatch, StubForge(_paginate(items)))
    a = mod.marked_pr(REPO, "0009-x", "t")
    _install(mod, monkeypatch, StubForge(_paginate(list(reversed(items)))))
    b = mod.marked_pr(REPO, "0009-x", "t")
    assert a["number"] == b["number"] == 3


def test_newest_closed_marked_pr_agrees_across_page_order(mod, monkeypatch):
    items = [_issue(3, "0009-x", state="closed", merged=False),
             _issue(5, "0009-x", state="closed", merged=True, merged_at="t")]
    _install(mod, monkeypatch, StubForge(_paginate(items)))
    a = mod.newest_closed_marked_pr(REPO, "0009-x", "t")
    _install(mod, monkeypatch, StubForge(_paginate(list(reversed(items)))))
    b = mod.newest_closed_marked_pr(REPO, "0009-x", "t")
    assert a["number"] == b["number"] == 5


def test_open_marked_plans_agrees_across_page_order(mod, monkeypatch):
    """Two OPEN PRs mark the same plan id (e.g. a stray duplicate dispatch):
    `_open_marked_plans` must name the OLDEST one regardless of which page
    order it is read in, the same answer `marked_pr` itself would give."""
    items = [_issue(3, "0009-x", state="open"),
             _issue(9, "0009-x", state="open")]
    _install(mod, monkeypatch, StubForge(_paginate(items)))
    a = mod._open_marked_plans(REPO, "t")
    _install(mod, monkeypatch, StubForge(_paginate(list(reversed(items)))))
    b = mod._open_marked_plans(REPO, "t")
    assert a == b == {"0009-x": 3}


# -------------------------------------------------------- repeats, skips

def test_a_repeated_item_across_pages_is_tolerated(mod, monkeypatch):
    """A pull request opened mid-walk can land on page 1 after it was read
    and still appear on a later page too — the walk must not choke on it."""
    pr = _issue(7, "0009-x", state="open")
    _install(mod, monkeypatch, StubForge([[pr], [pr], []]))
    got = mod.marked_pr(REPO, "0009-x", "t")
    assert got["number"] == 7


def test_an_item_with_no_pull_request_key_is_skipped(mod, monkeypatch):
    plain_issue = {"number": 1, "state": "open", "user": {"id": 42},
                   "body": "Plan: 0009-x\n"}        # no `pull_request` key
    _install(mod, monkeypatch, StubForge([[plain_issue]]))
    assert mod.marked_pr(REPO, "0009-x", "t") is None


def test_a_page_of_only_skipped_items_is_not_the_end(mod, monkeypatch):
    """Page 1 is entirely non-pull-request items; the real match is on page
    2. The walk must not read an all-dropped page as `end`."""
    plain_issue = {"number": 1, "state": "open", "user": {"id": 1},
                   "body": "not a pull request"}
    match = _issue(2, "0009-x", state="open")
    forge = _install(mod, monkeypatch, StubForge([[plain_issue], [match], []]))
    got = mod.marked_pr(REPO, "0009-x", "t")
    assert got["number"] == 2
    assert forge.calls == [1, 2, 3], "page 2 must be read, not mistaken for the end"


# ----------------------------------------------------------------- failure

def test_a_failed_page_is_unreadable(mod, monkeypatch):
    _install(mod, monkeypatch, StubForge([[_issue(1, "0009-x")]], fail_on=1))
    assert mod.marked_pr(REPO, "0009-x", "t") == "unreadable"


# ------------------------------------------------------------------ shape

def test_shape_matches_list_pulls_for_merged_closed_open(mod, monkeypatch):
    """`_pull_pages`'s reshaped item carries every field the pre-migration
    `/pulls` shape did (`number`, `user`, `body`, `state`, `closed_at`,
    `created_at`, `updated_at`, `merged`, `merged_at`) — and no `head`."""
    merged = _issue(1, "x", state="closed", merged=True, merged_at="2026-01-01T00:00:00Z")
    closed_unmerged = _issue(2, "x", state="closed", merged=False)
    open_pr = _issue(3, "x", state="open")
    _install(mod, monkeypatch, StubForge([[merged, closed_unmerged, open_pr]]))
    pages = list(mod._pull_pages(REPO, "t"))
    assert pages[-1] == "end"
    got = {p["number"]: p for p in pages[0]}

    for raw, mapped in ((merged, got[1]), (closed_unmerged, got[2]), (open_pr, got[3])):
        assert mapped["number"] == raw["number"]
        assert mapped["user"] == raw["user"]
        assert mapped["body"] == raw["body"]
        assert mapped["state"] == raw["state"]
        assert mapped["closed_at"] == raw["closed_at"]
        assert mapped["created_at"] == raw["created_at"]
        assert mapped["updated_at"] == raw["updated_at"]
        assert mapped["merged"] == raw["pull_request"]["merged"]
        assert mapped["merged_at"] == raw["pull_request"]["merged_at"]
        assert "head" not in mapped


def test_a_caller_needing_head_gets_it_from_get_pull(mod, monkeypatch):
    """`marked_pr`'s answer carries no `head` (plan 0218 D3) — a caller that
    needs it (e.g. `meeting_sweep`) fetches that one pull request."""
    forge = _install(mod, monkeypatch,
                     StubForge([[_issue(7, "0009-x", state="closed", merged=True,
                                        merged_at="t")]]))
    forge.pulls_by_number[7] = {"number": 7, "head": {"sha": "a" * 40}}
    pr = mod.marked_pr(REPO, "0009-x", "t")
    assert "head" not in pr
    st, body = forge.get_pull(REPO, pr["number"])
    assert st == 200 and body["head"]["sha"] == "a" * 40


# -------------------------------------------------------------------- cap

def test_the_default_issue_page_cap_is_200(mod):
    assert mod.ISSUE_PAGE_CAP == 200
