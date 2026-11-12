"""Plan 0178: a reviewer seat says whether it reads the repository or only the change."""
import importlib.machinery
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO = "operator/eunomia"


def _load(name):
    mod_name = name.replace("-", "_") + "_sa"
    loader = importlib.machinery.SourceFileLoader(mod_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def fm(fleet):
    return _load("fleet-models")


@pytest.fixture
def con(fm):
    c = fm.connect()
    yield c
    c.close()


def _seat(con, fm, position, runner, ordinal=1, repo="default"):
    fm.set_runner(con, repo, position, runner, ordinal=ordinal, actor="cli:t")


def _events(fleet):
    p = fleet / "events.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []
    return [e for e in rows if e["type"] == "seat-access-changed"]


def test_no_rows_every_seat_gets_its_harness_default_or_ceiling(fm, con):
    _seat(con, fm, "plan_reviewer", "sub-fable")
    _seat(con, fm, "code_reviewer", "sub-fable")
    _seat(con, fm, "code_reviewer", "sub-codex", ordinal=2)
    _seat(con, fm, "style_reviewer", "sub-fable")
    _seat(con, fm, "lane_reviewer", "sub-fable")
    got = {(s["position"], s["ordinal"]): s for s in fm.access_show(con, REPO)}
    assert got[("plan_reviewer", 1)]["access"] == "repo"
    assert got[("code_reviewer", 1)]["access"] == "repo"
    assert got[("code_reviewer", 2)]["access"] == "change"
    assert got[("code_reviewer", 2)]["reason"] == "codex reviews without tools"
    for pos in ("style_reviewer", "lane_reviewer"):
        assert got[(pos, 1)]["access"] == "change"
        assert got[(pos, 1)]["settable"] is False
    assert got[("code_reviewer", 1)]["note"].startswith("lane pull requests")
    assert got[("style_reviewer", 1)]["note"] is None


def test_a_repo_row_wins_then_the_default_row(fm, con):
    _seat(con, fm, "code_reviewer", "sub-fable")
    assert fm.set_access(con, "default", "code_reviewer", 1, "change")
    assert fm.resolve_access(con, REPO, "code_reviewer", 1)[:2] == ("change", "default")
    assert fm.set_access(con, REPO, "code_reviewer", 1, "repo")
    assert fm.resolve_access(con, REPO, "code_reviewer", 1)[:2] == ("repo", "repo")
    assert fm.resolve_access(con, "operator/other", "code_reviewer", 1)[:2] == ("change", "default")


def test_ceilings_refuse_repo_and_write_nothing(fm, con, fleet):
    _seat(con, fm, "code_reviewer", "sub-codex")
    _seat(con, fm, "lane_reviewer", "sub-fable")
    _seat(con, fm, "style_reviewer", "sub-fable")
    for pos, word in (("code_reviewer", "codex"), ("lane_reviewer", "0028"),
                      ("style_reviewer", "0031")):
        with pytest.raises(ValueError, match=word):
            fm.set_access(con, REPO, pos, 1, "repo")
        assert fm.set_access(con, REPO, pos, 1, "change")
    n = con.execute("SELECT COUNT(*) FROM seat_access_change").fetchone()[0]
    with pytest.raises(ValueError):
        fm.set_access(con, REPO, "lane_reviewer", 1, "repo")
    assert con.execute("SELECT COUNT(*) FROM seat_access_change").fetchone()[0] == n
    assert len(_events(fleet)) == n


def test_inherit_keeps_the_row_and_resolves_like_no_row(fm, con):
    _seat(con, fm, "code_reviewer", "sub-fable")
    fm.set_access(con, "default", "code_reviewer", 1, "change")
    fm.set_access(con, REPO, "code_reviewer", 1, "repo")
    assert fm.set_access(con, REPO, "code_reviewer", 1, "inherit")
    assert con.execute("SELECT access FROM seat_access WHERE repo=?", (REPO,)).fetchone() == ("inherit",)
    assert fm.resolve_access(con, REPO, "code_reviewer", 1)[:2] == ("change", "default")
    assert con.execute("SELECT old_access, new_access FROM seat_access_change "
                       "WHERE repo=? ORDER BY ts, rowid DESC", (REPO,)).fetchall()
    assert ("repo", "inherit") in con.execute(
        "SELECT old_access, new_access FROM seat_access_change WHERE repo=?", (REPO,)).fetchall()


def test_every_set_writes_one_change_row_and_moves_updated_at(fm, con, fleet):
    _seat(con, fm, "code_reviewer", "sub-fable")
    fm.set_access(con, REPO, "code_reviewer", 1, "change", actor="cli:a")
    first = con.execute("SELECT created_at, updated_at FROM seat_access").fetchone()
    fm.set_access(con, REPO, "code_reviewer", 1, "repo", actor="cli:b")
    created, updated = con.execute("SELECT created_at, updated_at FROM seat_access").fetchone()
    assert created == first[0] and updated >= first[1]
    rows = con.execute("SELECT actor, old_access, new_access FROM seat_access_change "
                       "ORDER BY rowid").fetchall()
    assert rows == [("cli:a", None, "change"), ("cli:b", "change", "repo")]
    assert [e["detail"]["new_access"] for e in _events(fleet)] == ["change", "repo"]
    assert set(_events(fleet)[0]["detail"]) == set(fm.ACCESS_EVENT_DETAIL_KEYS)


def test_reviewers_json_carries_access(fm, con, capsys):
    _seat(con, fm, "code_reviewer", "sub-fable")
    _seat(con, fm, "code_reviewer", "sub-codex", ordinal=2)
    capsys.readouterr()
    assert fm.main(["reviewers", REPO, "code_reviewer", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["access"] for r in out] == ["repo", "change"]


def test_reconstruct_restores_every_row_including_a_cleared_one(fleet, monkeypatch):
    fm = _load("fleet-models")
    con = fm.connect()
    _seat(con, fm, "code_reviewer", "sub-fable")
    _seat(con, fm, "plan_reviewer", "sub-fable")
    fm.set_access(con, REPO, "code_reviewer", 1, "change")
    fm.set_access(con, REPO, "plan_reviewer", 1, "change")
    fm.set_access(con, REPO, "plan_reviewer", 1, "inherit")
    fm.set_access(con, "default", "code_reviewer", 1, "repo")
    q = "SELECT repo, position, ordinal, access, created_at, updated_at FROM seat_access ORDER BY 1,2,3"
    before = con.execute(q).fetchall()
    assert len(before) == 3 and "inherit" in {r[3] for r in before}
    con.close()

    collect = _load("fleet-collect")
    assert {"seat_access", "seat_access_change"} <= collect.DURABLE_TABLES
    monkeypatch.setattr(collect, "DB", fleet / "fleet.db")
    monkeypatch.setattr(collect, "PROJECTS", fleet / "no-projects")
    monkeypatch.setattr(collect, "FLEET_DIR", fleet)
    monkeypatch.setattr(collect, "WORK_ROOT", fleet / "no-work-root")
    collect.main()
    db = sqlite3.connect(fleet / "fleet.db")
    db.execute("DROP TABLE seat_access")
    db.commit()
    db.close()
    collect.main()
    db = sqlite3.connect(fleet / "fleet.db")
    after = db.execute(q).fetchall()
    assert [r[:4] for r in after] == [r[:4] for r in before]
    for a, b in zip(after, before):
        assert abs(a[4] - b[4]) < 2 and abs(a[5] - b[5]) < 2


def test_a_bad_runner_is_not_settable_and_does_not_fail_show_all(fm, con, capsys, monkeypatch):
    _seat(con, fm, "code_reviewer", "sub-fable")
    _seat(con, fm, "code_reviewer", "sub-fable", ordinal=2)
    con.execute("UPDATE repo_model SET runner='retired-runner' WHERE ordinal=2")
    con.commit()
    got = {s["ordinal"]: s for s in fm.access_show(con, "default")}
    assert got[2]["settable"] is False and "retired-runner" in got[2]["reason"]
    assert got[2]["access"] == "change"
    assert got[1]["settable"] is True and got[1]["access"] == "repo"
    with pytest.raises(ValueError):
        fm.set_access(con, "default", "code_reviewer", 2, "repo")
    monkeypatch.setattr(fm, "_conf_repos", lambda: [REPO])
    assert fm.main(["access", "show", "--all", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert {(s["repo"], s["ordinal"]) for s in out} == {(REPO, 1), (REPO, 2), ("default", 1), ("default", 2)}


def test_a_seat_with_no_runner_is_change_and_not_settable(fm, con):
    fm.set_access(con, REPO, "code_reviewer", 1, "change")
    access, _layer, reason, settable = fm.resolve_access(con, REPO, "code_reviewer", 1)
    assert (access, reason, settable) == ("change", "no runner at this seat", False)
    with pytest.raises(ValueError):
        fm.set_access(con, REPO, "code_reviewer", 1, "repo")
