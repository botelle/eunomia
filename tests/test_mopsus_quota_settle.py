"""A usage-limit stop is settled only by its own reviewer (plan 0168, ADR-0028 §1).

The rule is the one plan 0149 gave `no-verdict`: a later `review_attempt` at the
same repo, PR, head and ordinal, or a live `review` from that ordinal's account.
The other reviewer's review at the head proves nothing about this stop.
"""
import datetime
import sqlite3

import pytest

from test_mopsus_review_retry import (
    _StubForge,
    _dbpath,
    _load,
    _open_pr,
    _seed_attempt,
    _seed_review,
    _wire,  # noqa: F401  (autouse fixture)
    BIN,
)


@pytest.fixture
def mp():
    return _load("mopsus_quota_settle", BIN / "mopsus")


def _accounts(mp):
    roles = mp.lib.agent_roles()
    return roles["reviewer"].account, roles["second_reviewer"].account


def _forge(mp, reviews=()):
    forge = _StubForge(pulls={("operator/sniff", 7): _open_pr()},
                       reviews={("operator/sniff", 7): list(reviews)})
    mp.fwatch._forge = lambda token: forge


def _fr(login, head="deadbeef"):
    return {"commit_id": head, "stale": False, "dismissed": False,
            "user": {"login": login}}


def test_a_quota_row_with_only_the_other_reviewers_review_is_held(mp, tmp_path):
    """The 2026-10-27 case: ordinal 1 stopped, rev2bot reviewed the same head."""
    first, second = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=1)
    _seed_review(db, reviewer=second)
    assert len(mp._not_superseded_review_attempts(db)) == 1
    _forge(mp, [_fr(second)])
    assert len(mp.held_review_queue("tok", db=db)) == 1


def test_a_live_review_by_its_own_account_settles_a_quota_row(mp, tmp_path):
    first, _ = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=1)
    _seed_review(db, reviewer=first)
    assert mp._not_superseded_review_attempts(db) == []


def test_the_forge_review_of_its_own_account_settles_a_quota_row(mp, tmp_path):
    first, _ = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=1)
    _forge(mp, [_fr(first)])
    assert mp.held_review_queue("tok", db=db) == []


def test_a_dismissed_own_review_does_not_settle_a_quota_row(mp, tmp_path):
    first, _ = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=1)
    _seed_review(db, reviewer=first, dismissed=1)
    assert len(mp._not_superseded_review_attempts(db)) == 1


def test_a_stale_own_review_does_not_settle_a_quota_row(mp, tmp_path):
    first, _ = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=1)
    _seed_review(db, reviewer=first)
    con = sqlite3.connect(str(db))
    con.execute("UPDATE review SET stale=1")
    con.commit()
    con.close()
    assert len(mp._not_superseded_review_attempts(db)) == 1
    # the forge withdraws a stale review too, so reviews_at does not see it
    stale = dict(_fr(first), stale=True)
    _forge(mp, [stale])
    assert len(mp.held_review_queue("tok", db=db)) == 1


def test_a_later_attempt_settles_a_quota_row_only_at_the_same_ordinal(mp, tmp_path):
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=1, session="s1")
    _seed_attempt(db, ordinal=2, outcome="ok", session="s2")
    assert len(mp._not_superseded_review_attempts(db)) == 1
    _seed_attempt(db, ordinal=1, outcome="ok", session="s3")
    assert mp._not_superseded_review_attempts(db) == []


def test_a_quota_row_at_ordinal_two_is_held_by_an_ordinal_one_review(mp, tmp_path):
    first, _ = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, ordinal=2)
    _seed_review(db, reviewer=first)
    assert len(mp._not_superseded_review_attempts(db)) == 1
    _forge(mp, [_fr(first)])
    assert len(mp.held_review_queue("tok", db=db)) == 1


def test_a_budget_row_is_settled_like_the_others(mp, tmp_path):
    first, second = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="budget", ordinal=1)
    _seed_review(db, reviewer=second)
    assert len(mp._not_superseded_review_attempts(db)) == 1
    _seed_review(db, reviewer=first)
    assert mp._not_superseded_review_attempts(db) == []


def test_a_no_verdict_row_is_settled_by_its_own_reviewer_on_the_forge(mp, tmp_path):
    """D2's new check: no `review` row in fleet.db yet, the forge has one."""
    _, second = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="no-verdict", ordinal=2)
    _forge(mp, [_fr(second)])
    assert mp.held_review_queue("tok", db=db) == []


def test_a_no_verdict_row_with_only_the_other_reviewer_on_the_forge_is_held(mp, tmp_path):
    first, _ = _accounts(mp)
    db = _dbpath(tmp_path)
    _seed_attempt(db, outcome="no-verdict", ordinal=2)
    _forge(mp, [_fr(first)])
    assert len(mp.held_review_queue("tok", db=db)) == 1


# --------------------------------------------------------------------- D2b


def _gate_world(mp, tmp_path, stopped):
    db = _dbpath(tmp_path)
    assert mp.fquota.close("model:fable", None, "stop", "ev-0", db=str(db))
    assert mp.fquota.open_gate("model:fable", "owner", db=str(db))
    _seed_attempt(db, model="claude-fable-5-1", started=stopped)
    return db


def test_a_stop_before_the_last_reopening_closes_nothing(mp, tmp_path):
    db = _gate_world(mp, tmp_path, "2026-09-27T00:00:00Z")
    assert len(mp._not_superseded_review_attempts(db, ("quota",))) == 1
    mp.review_stop_sweep(db=str(db))
    assert mp.fquota.gate("model:fable", str(db))["state"] == "open"


def test_a_stop_after_the_last_reopening_closes_the_gate(mp, tmp_path):
    later = (datetime.datetime.now(datetime.timezone.utc)
             + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db = _gate_world(mp, tmp_path, later)
    mp.review_stop_sweep(db=str(db))
    assert mp.fquota.gate("model:fable", str(db))["state"] == "closed"
