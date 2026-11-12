"""Plan 0162 (ADR-0027 §3, §4): an `Answer:` from the owner or intakebot resumes the plan.

The forge is the shared `_api_factory` stub, the spawned orchestrator is a shell
stub that records its argv and EUNOMIA_ANSWER_COMMENT, and the dead lease is a
real one. What is claimed is what fleet-watch DOES with a thread: events, request
rows, spawns, pages.
"""
import json
import sqlite3
import stat

import pytest

from test_fleet_watch import (
    _load as _fw_load,
    _wire as _fw_wire,
    _api_factory,
    _wait_spawns,
)
from test_mopsus_review_retry import _orphaned_lease_local

OWNER, INTAKEBOT, STRANGER = "11", "22", "33"
ASKED = "2025-01-01T00:00:00Z"
LATER = "2025-01-02T00:00:00Z"
EARLIER = "2024-12-31T00:00:00Z"


def _stub(tmp_path):
    log = tmp_path / "spawns.log"
    s = tmp_path / "orch-stub.sh"
    s.write_text('#!/bin/sh\n[ "$1" = "--ready" ] && exit 0\n'
                 f'echo "$1 $2 $3 $EUNOMIA_SESSION ans=$EUNOMIA_ANSWER_COMMENT" >> {log}\n')
    s.chmod(s.stat().st_mode | stat.S_IXUSR)
    return str(s), log


def _setup(tmp_path, comments, intakebot=INTAKEBOT, asked=ASKED, where=None):
    orch, log = _stub(tmp_path)
    fd = tmp_path / "fleet"
    mod = _fw_load(fd, FLEET_WATCH_REPOS="operator/sniff", FLEET_OPERATOR_UID=OWNER,
                   FLEET_INTAKEBOT_UID=intakebot)
    pulls = [{"user": {"login": "implbot"}, "state": "open", "number": 7,
              "body": "Plan: 0009-thing"}]
    _fw_wire(mod, _api_factory(pulls=pulls, comments=comments), orch)
    dead = _orphaned_lease_local(mod, fd)
    qid = f"q-{dead}-1"
    ev = {"type": "plan-question", "ts": asked, "repo": "operator/sniff",
          "detail": {"plan": "0009-thing", "question_id": qid, "section": "3",
                     "question": "Which?", "blocking": True,
                     "where": where or {"pr": 7}}}
    with open(fd / "events.jsonl", "a") as fh:
        fh.write(json.dumps(ev) + "\n")
    pages = []
    mod.notify = lambda t, b, *a, **k: pages.append((t, b)) or True
    return mod, fd, log, dead, qid, pages


def _c(cid, uid, body, at=LATER, login="x"):
    return {"id": cid, "user": {"id": int(uid), "login": login},
            "body": body, "created_at": at}


def _rows(fd):
    con = sqlite3.connect(str(fd / "fleet.db"))
    try:
        return con.execute("SELECT lease, reason, comment_id, consumed_at "
                           "FROM resume_request").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _answered(fd):
    p = fd / "events.jsonl"
    evs = [json.loads(l) for l in p.read_text().splitlines()]
    return [e for e in evs if e["type"] == "question-answered"]


def _with_answer(tmp_path, make, intakebot=INTAKEBOT):
    """Build the thread AFTER the lease exists, since the question id embeds it."""
    comments = []
    mod, fd, log, dead, qid, pages = _setup(tmp_path, comments, intakebot=intakebot)
    comments.extend(make(qid))
    mod.comments = comments
    return mod, fd, log, dead, qid, pages


@pytest.mark.parametrize("uid", [OWNER, INTAKEBOT])
def test_an_answer_from_the_owner_or_intakebot_resumes_the_plan_once(tmp_path, uid):
    mod, fd, log, dead, qid, _ = _with_answer(
        tmp_path, lambda q: [_c(40, uid, f"Answer: {q}\nuse table b")])
    assert mod.cycle() == 1
    spawns = _wait_spawns(log, 2)
    assert spawns[-1].startswith("--resume ") and spawns[-1].endswith("ans=40")
    (ev,) = _answered(fd)
    assert ev["detail"] == {"question_id": qid, "answerer_uid": uid,
                            "comment_id": 40, "answered_at": LATER}
    assert "use table b" not in json.dumps(ev)
    (row,) = _rows(fd)
    assert row[:3] == (dead, "question-answered", 40) and row[3] is not None
    # a second valid Answer after consumption writes no second request
    mod.comments.append(_c(41, uid, f"Answer: {qid}\nactually c"))
    assert mod.cycle() == 0
    assert len(_rows(fd)) == 1 and len(_answered(fd)) == 1


@pytest.mark.parametrize("make,intakebot", [
    (lambda q: [_c(40, STRANGER, f"Answer: {q}")], INTAKEBOT),
    # the login matches the owner's name; the uid does not
    (lambda q: [_c(40, STRANGER, f"Answer: {q}", login="operator")], INTAKEBOT),
    # intakebot's uid with FLEET_INTAKEBOT_UID unset
    (lambda q: [_c(40, INTAKEBOT, f"Answer: {q}")], ""),
    # older than the question
    (lambda q: [_c(40, OWNER, f"Answer: {q}", at=EARLIER)], INTAKEBOT),
    # another question's id
    (lambda q: [_c(40, OWNER, "Answer: q-other-lease-1")], INTAKEBOT),
    # not the first line
    (lambda q: [_c(40, OWNER, f"thanks\nAnswer: {q}")], INTAKEBOT),
    # a intakebot comment that is not an Answer is not a respawn either
    (lambda q: [_c(40, INTAKEBOT, "respawn")], INTAKEBOT),
])
def test_everything_else_does_nothing(tmp_path, make, intakebot):
    mod, fd, log, dead, qid, _ = _with_answer(tmp_path, make, intakebot)
    assert mod.cycle() == 0
    assert _answered(fd) == [] if (fd / "events.jsonl").exists() else True
    assert _rows(fd) == []
    assert len(_wait_spawns(log, 1)) == 1


def test_an_answer_on_an_issue_thread_resumes_without_a_pr(tmp_path):
    comments = []
    mod, fd, log, dead, qid, _ = _setup(tmp_path, comments, where={"issue": 31})
    mod.marked_pr = lambda *a, **k: None       # no PR exists for this plan
    comments.append(_c(50, OWNER, f"Answer: {qid}"))
    assert mod.cycle() == 1
    assert _wait_spawns(log, 2)[-1].endswith("ans=50")


def test_an_unanswered_question_pages_at_n_and_four_n_and_never_again(tmp_path):
    mod, fd, log, dead, qid, pages = _setup(tmp_path, [], asked=ASKED)
    mod.fleetconfig.resolve_setting = lambda k, d, s, **kw: (240, "default", None)
    mod.lib.now_utc = lambda: mod._ts_utc("2025-01-01T03:00:00Z")      # 180 min
    mod.cycle()
    assert [p for p in pages if "unanswered" in p[0]] == []
    mod.lib.now_utc = lambda: mod._ts_utc("2025-01-01T04:30:00Z")      # 270 min
    mod.cycle(); mod.cycle()
    q = [p for p in pages if "unanswered" in p[0]]
    assert len(q) == 1 and qid in q[0][1] and "PR #7" in q[0][1]
    mod.lib.now_utc = lambda: mod._ts_utc("2025-01-01T20:00:00Z")      # 20h
    mod.cycle(); mod.cycle()
    assert len([p for p in pages if "unanswered" in p[0]]) == 2
    mod.lib.now_utc = lambda: mod._ts_utc("2025-02-01T00:00:00Z")
    mod.cycle()
    assert len([p for p in pages if "unanswered" in p[0]]) == 2


def test_fleet_config_bounds_question_page_minutes():
    import importlib.machinery, importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "bin" / "fleet-config"
    ldr = importlib.machinery.SourceFileLoader("fc162", str(p))
    fc = importlib.util.module_from_spec(importlib.util.spec_from_loader("fc162", ldr))
    ldr.exec_module(fc)
    v = fc.SETTINGS["question_page_minutes"]["validate"]
    assert v("30") == 30 and v(2880) == 2880 and fc.SETTINGS[
        "question_page_minutes"]["default"] == 240
    for bad in ("10", "5000", True, "x"):
        with pytest.raises(ValueError):
            v(bad)


# ------------------------------------------------------------- plan 0164
def _limit(mod, n):
    real = mod.fleetconfig.resolve_setting
    mod.fleetconfig.resolve_setting = lambda k, d, s, **kw: (
        (n, "default", None) if k == "question_limit" else real(k, d, s, **kw))


def _prior_intakebot_answers(fd, qid_plan_events):
    with open(fd / "events.jsonl", "a") as fh:
        for i in range(qid_plan_events):
            q = f"q-old-{i}"
            for typ, d in (("plan-question", {"plan": "0009-thing", "question_id": q}),
                           ("question-answered", {"question_id": q, "comment_id": 900 + i,
                                                  "answerer_uid": INTAKEBOT})):
                fh.write(json.dumps({"type": typ, "ts": ASKED, "repo": "operator/sniff",
                                     "detail": d}) + "\n")


def test_past_the_limit_intakebot_writes_nothing_and_pages_once_then_owner_resumes(tmp_path):
    mod, fd, log, dead, qid, pages = _with_answer(
        tmp_path, lambda q: [_c(40, INTAKEBOT, f"Answer: {q}")])
    _limit(mod, 2)
    _prior_intakebot_answers(fd, 2)
    assert mod.cycle() == 0 and mod.cycle() == 0
    assert _rows(fd) == []
    lim = [p for p in pages if "limit" in p[0]]
    assert len(lim) == 1 and "0009-thing" in lim[0][1] and "2" in lim[0][1]
    mod.comments.append(_c(41, OWNER, f"Answer: {qid}\nGo"))
    assert mod.cycle() == 1
    assert _wait_spawns(log, 2)[-1].endswith("ans=41")


def test_the_second_intakebot_answer_with_limit_two_is_accepted(tmp_path):
    mod, fd, log, dead, qid, pages = _with_answer(
        tmp_path, lambda q: [_c(40, INTAKEBOT, f"Answer: {q}")])
    _limit(mod, 2)
    _prior_intakebot_answers(fd, 1)
    assert mod.cycle() == 1
    assert [p for p in pages if "limit" in p[0]] == []


def test_an_owner_answer_after_a_consumed_intakebot_answer_resumes_once_more(tmp_path):
    mod, fd, log, dead, qid, _ = _with_answer(
        tmp_path, lambda q: [_c(40, INTAKEBOT, f"Answer: {q}\nuse a")])
    assert mod.cycle() == 1
    _wait_spawns(log, 2)
    for lf in (fd / "leases").glob("*.json"):       # the resumed session has ended
        rec = json.loads(lf.read_text())
        rec["created"] = "2020-01-01T00:00:00Z"
        lf.write_text(json.dumps(rec))
    mod.comments.append(_c(41, OWNER, f"Answer: {qid}\nno, b"))
    assert mod.cycle() == 1
    rows = _rows(fd)
    assert [r[1] for r in rows] == ["question-answered", "question-answered-by-owner"]
    assert _wait_spawns(log, 3)[-1].endswith("ans=41")
    mod.comments.append(_c(42, OWNER, f"Answer: {qid}\nactually c"))
    assert mod.cycle() == 0
    assert len(_rows(fd)) == 2


def test_an_owner_answer_before_intakebot_resumed_replaces_it(tmp_path):
    # r1 H1: intakebot's resume is deferred (here the review path is closed); the
    # owner's correction lands first, so the resume carries the owner's answer.
    mod, fd, log, dead, qid, _ = _with_answer(
        tmp_path, lambda q: [_c(40, INTAKEBOT, f"Answer: {q}\nuse a")])
    allows = mod._review_path_allows
    mod._review_path_allows = lambda *a, **k: False
    assert mod.cycle() == 0
    assert [r[:2] + (r[3],) for r in _rows(fd)] == [(dead, "question-answered", None)]
    mod.comments.append(_c(41, OWNER, f"Answer: {qid}\nno, b"))
    mod._review_path_allows = allows
    assert mod.cycle() == 1
    assert _wait_spawns(log, 2)[-1].endswith("ans=41")
    rows = _rows(fd)
    assert [r[1:3] for r in rows] == [("question-answered", 40),
                                      ("question-answered-by-owner", 41)]
    assert all(r[3] is not None for r in rows)
    mod.comments.append(_c(42, OWNER, f"Answer: {qid}\nactually c"))
    assert mod.cycle() == 0
    assert len(_rows(fd)) == 2


def test_intakebot_after_the_owner_does_nothing(tmp_path):
    mod, fd, log, dead, qid, _ = _with_answer(
        tmp_path, lambda q: [_c(40, OWNER, f"Answer: {q}")])
    assert mod.cycle() == 1
    mod.comments.append(_c(41, INTAKEBOT, f"Answer: {qid}\nother"))
    assert mod.cycle() == 0
    assert len(_rows(fd)) == 1


def test_an_answer_from_an_implementer_uid_is_refused_and_logged(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_IMPLEMENTER_UIDS", OWNER)
    mod, fd, log, dead, qid, _ = _with_answer(
        tmp_path, lambda q: [_c(40, OWNER, f"Answer: {q}")])
    warns = []
    mod.warn = lambda m, *a, **k: warns.append(m)
    mod.IMPLEMENTER_UIDS = {OWNER}
    assert mod.cycle() == 0
    assert _rows(fd) == []
    assert any("CONFIGURATION ERROR" in w for w in warns)


def test_fleet_config_bounds_question_limit():
    import importlib.machinery, importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "bin" / "fleet-config"
    ldr = importlib.machinery.SourceFileLoader("fc164", str(p))
    fc = importlib.util.module_from_spec(importlib.util.spec_from_loader("fc164", ldr))
    ldr.exec_module(fc)
    v = fc.SETTINGS["question_limit"]["validate"]
    assert v("1") == 1 and v(10) == 10 and fc.SETTINGS["question_limit"]["default"] == 3
    for bad in ("0", "11", True, "x"):
        with pytest.raises(ValueError):
            v(bad)


def test_question_limit_reads_the_env_var_like_fleet_config(tmp_path, monkeypatch):
    mod, fd, log, dead, qid, pages = _with_answer(
        tmp_path, lambda q: [_c(40, INTAKEBOT, f"Answer: {q}")])
    monkeypatch.setenv("FLEET_QUESTION_LIMIT", "2")
    assert mod._question_limit() == 2


def test_an_owner_correction_on_a_released_lease_pages_once(tmp_path):
    mod, fd, log, dead, qid, pages = _with_answer(
        tmp_path, lambda q: [_c(40, INTAKEBOT, f"Answer: {q}\nuse a")])
    assert mod.cycle() == 1
    _wait_spawns(log, 2)
    mod.comments.append(_c(41, OWNER, f"Answer: {qid}\nno, b"))
    mod.cycle()
    for lf in (fd / "leases").glob("*.json"):       # the session ends normally
        lf.unlink()
    mod.cycle()
    mod.cycle()
    lost = [p for p in pages if "not applied" in p[0]]
    assert len(lost) == 1
