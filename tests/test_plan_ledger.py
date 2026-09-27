"""Tests for `fleet-plan ledger` (plan 0061).

The database in these tests is built through fleet-collect's own `SCHEMA` and
`DISPATCH_VIEW`, so the columns the ledger selects are the columns the real
`dispatch` view has — a fixture with a hand-made `dispatch` table would keep
passing after the view changed shape.

The pin numbers in plan 0061 §2 (45 plans, 12 `claimed`, 2 `stalled`, `0057`
naming #443) were measured on 2026-09-17. `plans/` and `fleet.db` have both
moved since — `0045` was re-dispatched and merged as #479, and `0063` has since
failed — so the test that reproduces them uses a frozen synthetic set rather
than the live tree, and the live test below asserts only what cannot drift.
"""
import ast
import importlib.machinery
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"
FLEET_PLAN = BIN / "fleet-plan"
REPO_ROOT = BIN.parent


def _load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fp = _load(FLEET_PLAN, "fleet_plan_ledger")
fc = _load(BIN / "fleet-collect", "fleet_collect_for_ledger")

REPO = "operator/eunomia"


# ----------------------------------------------------------------- fixtures

def _plan(pid, status="ready", repo=REPO):
    return f"""---
id: {pid}
status: {status}
repo: {repo}
zone: public
tier: 2
paths:
  - "bin/x"
---

## 1. Goal

g
"""


def _plans(tmp_path, spec):
    """`spec` is [(id, status)] or [(id, status, repo)]; returns the file list."""
    d = tmp_path / "plans"
    d.mkdir(exist_ok=True)
    files = []
    for entry in spec:
        pid, status, *rest = entry
        f = d / f"{pid}.md"
        f.write_text(_plan(pid, status, *(rest or [REPO])))
        files.append(f)
    return files


class Db:
    """A fleet.db whose `dispatch` view is the real one."""

    def __init__(self, tmp_path, name="fleet.db"):
        self.path = tmp_path / name
        self.con = sqlite3.connect(str(self.path))
        self.con.executescript(fc.SCHEMA)
        self.con.executescript(fc.DISPATCH_VIEW)
        self._line = 0

    def _phase(self, lease, seq, ts, phase, pr=None, seconds=None, **facts):
        self._line += 1
        self.con.execute(
            "INSERT INTO dispatch_phase VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lease, seq, ts, phase, pr, None, None, seconds, None, None, None,
             json.dumps(facts), f"logs/{lease}.log", self._line))

    def _event(self, ts, etype, lease, repo, **detail):
        self._line += 1
        self.con.execute(
            "INSERT INTO event VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, "watch", etype, repo, None, None, None,
             json.dumps({"lease": lease, **detail}), "events.jsonl", self._line))

    def run(self, plan, outcome, repo=REPO, n=1, pr=None, rounds=0, impl=(1000,),
            reason=None, day="2026-09-10", lease=None):
        """One dispatch. `outcome` is merged | closed | failed | None (no terminal)."""
        lease = lease or f"branch--feat-{plan}--{n:03d}"
        ts = f"{day}T10:00:00Z"
        seq = 1
        self._phase(lease, seq, ts, "start", repo=repo, plan=plan, session="s")
        for secs in impl:
            seq += 1
            self._phase(lease, seq, ts, "implementer-exit", seconds=secs)
        for r in range(rounds):
            seq += 1
            self._phase(lease, seq, ts, "review-round", round=str(r + 1))
        if pr is not None:
            seq += 1
            self._phase(lease, seq, ts, "verified", pr=pr)
        if outcome in ("merged", "closed"):
            seq += 1
            self._phase(lease, seq, f"{day}T11:00:00Z", "steward-end", state=outcome)
        elif outcome == "failed":
            self._event(f"{day}T11:00:00Z", "plan-failed", lease, repo,
                        reason=reason or "boom")
        return self

    def commit(self):
        self.con.commit()
        self.con.close()
        return self.path


def _ledger(files, db, **kw):
    return fp.ledger(files, db=db, repo=REPO, **kw)


def _rows(data):
    return {r["id"]: r for r in data["rows"]}


# ------------------------------------------------- the two classes, and the pin

def test_the_pin_shape_is_12_claimed_and_2_stalled_and_0057_names_443(tmp_path):
    """Plan 0061 §4: against the pin, 12 `claimed`, 2 `stalled`, and 0057's row
    names pull request #443. A frozen synthetic pin — the live one has moved."""
    spec, db = [], Db(tmp_path)
    for i in range(1, 13):                                  # 12: done, never dispatched
        spec.append((f"{i:04d}-by-hand", "done"))
    for i in range(13, 42):                                 # 29: done, dispatched, merged
        spec.append((f"{i:04d}-dispatched", "done"))
        db.run(f"{i:04d}-dispatched", "merged", pr=100 + i)
    spec.append(("0042-not-yet", "ready"))                  # ready, never dispatched
    spec.append(("0043-abandoned", "abandoned"))
    spec.append(("0045-ci-logs", "ready"))                  # failed, no success
    db.run("0045-ci-logs", "failed", reason="plan text carries credential shapes")
    spec.append(("0057-the-verb-inventory", "ready"))       # failed, no success
    db.run("0057-the-verb-inventory", "failed", pr=443,
           reason="round 1: fix session timed out after 90 min")
    # other repositories' rows, as the live database has
    for k in range(23):
        db.run(f"09{k:02d}-other-repo-plan", "merged", repo="operator/pr-proxy", pr=k)

    data = _ledger(_plans(tmp_path, spec), db.commit())
    rows = _rows(data)
    assert len(rows) == 45
    assert data["counts"]["claimed"] == 12
    assert data["counts"]["stalled"] == 2
    assert {i for i, r in rows.items() if r["class"] == "stalled"} == \
        {"0045-ci-logs", "0057-the-verb-inventory"}
    r57 = rows["0057-the-verb-inventory"]
    assert r57["pr"] == 443
    assert r57["reason"] == "round 1: fix session timed out after 90 min"
    assert r57["outcome"] == "failed"


def test_a_failed_dispatch_that_was_later_retried_to_success_is_not_stalled(tmp_path):
    """0045 on the live database: failed on 2026-09-12, merged as #479 on
    2026-09-21. Failed-and-no-successful-one is the whole definition."""
    files = _plans(tmp_path, [("0045-a", "done")])
    db = Db(tmp_path)
    db.run("0045-a", "failed", n=1, day="2026-09-12")
    db.run("0045-a", "merged", n=2, pr=479, day="2026-09-21")
    row = _rows(_ledger(files, db.commit()))["0045-a"]
    assert row["class"] is None
    assert row["runs"] == 2 and row["outcome"] == "merged" and row["pr"] == 479
    assert row["reason"] is None


def test_done_with_only_a_failed_dispatch_is_stalled_not_claimed(tmp_path):
    """`claimed` is *no* dispatch record. A plan that has one, failed, is the
    other class — one row never carries both."""
    files = _plans(tmp_path, [("0007-a", "done")])
    db = Db(tmp_path)
    db.run("0007-a", "failed")
    assert _rows(_ledger(files, db.commit()))["0007-a"]["class"] == "stalled"


def test_ready_with_no_dispatch_is_neither_class(tmp_path):
    files = _plans(tmp_path, [("0007-a", "ready"), ("0008-b", "draft"),
                              ("0009-c", "abandoned")])
    rows = _rows(_ledger(files, Db(tmp_path).commit()))
    assert all(r["class"] is None and r["outcome"] == "none" and r["runs"] == 0
               for r in rows.values())


def test_a_lease_with_no_terminal_record_is_not_called_running(tmp_path):
    """The dispatch view's own caveat: no terminal record may mean running or
    may mean it died without writing one. The ledger says what is known."""
    files = _plans(tmp_path, [("0007-a", "ready")])
    db = Db(tmp_path)
    db.run("0007-a", None)
    row = _rows(_ledger(files, db.commit()))["0007-a"]
    assert row["outcome"] == "not-terminal" and row["class"] is None


def test_rounds_and_implementer_seconds_sum_across_every_run_and_exit(tmp_path):
    """`dispatch.seconds` is only the LAST implementer-exit, so a fix round's
    session would hide the first. Cost is the sum."""
    files = _plans(tmp_path, [("0007-a", "done")])
    db = Db(tmp_path)
    db.run("0007-a", "failed", n=1, rounds=1, impl=(1000, 5400))
    db.run("0007-a", "merged", n=2, pr=9, rounds=2, impl=(700,))
    row = _rows(_ledger(files, db.commit()))["0007-a"]
    assert row["rounds"] == 3
    assert row["impl_seconds"] == 7100


# ------------------------------------------------------------------- scoping

def test_another_repositorys_plan_id_never_appears(tmp_path):
    """D4. The live database carries operator/pr-proxy rows; none of their plan
    ids may show up here — as a row, as a join, or as a plan 'missing from the
    pin'. One of them deliberately shares an id with a eunomia plan."""
    files = _plans(tmp_path, [("0001-shared", "done"),
                              ("0002-elsewhere", "done", "operator/pr-proxy")])
    db = Db(tmp_path)
    db.run("0001-shared", "merged", repo="operator/pr-proxy", pr=7)   # same id, other repo
    db.run("0500-proxy-only", "failed", repo="operator/pr-proxy")
    data = _ledger(files, db.commit())
    rows = _rows(data)

    assert set(rows) == {"0001-shared"}                 # the pr-proxy plan file is out of scope
    assert rows["0001-shared"]["runs"] == 0             # pr-proxy's run did not join
    assert rows["0001-shared"]["class"] == "claimed"
    assert data["unmatched"] == []                      # nor is 0500 called missing from the pin
    assert "0500-proxy-only" not in json.dumps(data)
    assert data["out_of_scope"] == 1
    assert data["scope"] == REPO


def test_a_dispatch_of_this_repo_with_no_plan_file_is_named_not_dropped(tmp_path):
    files = _plans(tmp_path, [("0001-a", "ready")])
    db = Db(tmp_path)
    db.run("0099-renamed-away", "merged", pr=5)
    data = _ledger(files, db.commit())
    assert data["unmatched"] == ["0099-renamed-away"]
    assert set(_rows(data)) == {"0001-a"}


# ---------------------------------------------- marker joins, branch name never

def test_a_branch_name_prefix_does_not_join_only_the_plan_id_does(tmp_path):
    """Two plans share a prefix. The run of `0009-auth-v2` sits on a lease whose
    (truncated) branch name is a prefix of `0009-auth`'s — the collision the
    plan names. It must join to `-v2` and to nothing else."""
    files = _plans(tmp_path, [("0009-auth", "ready"), ("0009-auth-v2", "ready")])
    db = Db(tmp_path)
    db.run("0009-auth-v2", "merged", pr=31, lease="branch--feat-0009-auth--001")
    rows = _rows(_ledger(files, db.commit()))
    assert rows["0009-auth"]["runs"] == 0 and rows["0009-auth"]["pr"] is None
    assert rows["0009-auth-v2"]["runs"] == 1 and rows["0009-auth-v2"]["pr"] == 31


class _FakeForge:
    def __init__(self, pulls):
        self.pulls = pulls
        self.calls = []

    def get_pull(self, repo, n):
        self.calls.append(("get_pull", n))
        hit = [p for p in self.pulls if p["number"] == n]
        return (200, hit[0]) if hit else (404, None)

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        self.calls.append(("list_pulls", page))
        return (200, self.pulls) if page == 1 else (200, [])


def _watch(tmp_path, monkeypatch, pulls):
    monkeypatch.setenv("EUNOMIA_FLEET_DIR", str(tmp_path / "fleet"))
    for k in ("FLEET_WATCH_REPOS", "FLEET_IMPLEMENTER_UIDS", "FLEET_IMPLEMENTER_LOGINS",
              "FLEET_OPERATOR_UID", "FLEET_AGENT_ACCOUNTS", "FLEET_PINS"):
        monkeypatch.delenv(k, raising=False)
    watch = _load(BIN / "fleet-watch", "fleet_watch_for_ledger")
    forge = _FakeForge(pulls)
    monkeypatch.setattr(watch, "_forge", lambda token: forge)
    watch.fake_forge = forge
    return watch


def test_the_marker_lookup_is_line_anchored_so_a_longer_id_does_not_match(tmp_path, monkeypatch):
    """The forge half of the same rule: `marked_pr` resolves `Plan: <id>` on a
    line of its own, so `0009-auth`'s lookup cannot land on `0009-auth-v2`'s PR."""
    pulls = [
        {"number": 30, "state": "closed", "user": {"login": "implbot"},
         "body": "x\n\nPlan: 0009-auth-v2\n"},
        {"number": 31, "state": "open", "user": {"login": "implbot"},
         "body": "y\n\nPlan: 0009-auth\n"},
    ]
    lookup = fp._marker_lookup(_watch(tmp_path, monkeypatch, pulls), "tok")
    assert lookup(REPO, "0009-auth")["number"] == 31
    assert lookup(REPO, "0009-auth-v2")["number"] == 30
    assert lookup(REPO, "0009-au") is None


def test_a_stored_pr_carrying_the_marker_is_read_directly_not_scanned_for(tmp_path, monkeypatch):
    """One request, not a walk of every pull request in the repository."""
    pulls = [{"number": 31, "state": "closed", "merged": True, "user": {"login": "implbot"},
              "body": "y\n\nPlan: 0009-auth\n"}]
    watch = _watch(tmp_path, monkeypatch, pulls)
    got = fp._marker_lookup(watch, "tok")(REPO, "0009-auth", 31)
    assert got["number"] == 31
    assert watch.fake_forge.calls == [("get_pull", 31)]


def test_a_stored_pr_without_this_plans_marker_falls_back_to_the_scan(tmp_path, monkeypatch):
    """The stored number proves nothing by itself: it must carry THIS id on a
    line of its own, written by an implementer. `-v2`'s pull request, an
    outsider's, and a missing one all fall through to `marked_pr`."""
    pulls = [
        {"number": 30, "state": "closed", "user": {"login": "implbot"},
         "body": "x\n\nPlan: 0009-auth-v2\n"},
        {"number": 32, "state": "open", "user": {"login": "someone-else"},
         "body": "Plan: 0009-auth\n"},
        {"number": 31, "state": "open", "user": {"login": "implbot"},
         "body": "y\n\nPlan: 0009-auth\n"},
    ]
    for stored in (30, 32, 99):
        watch = _watch(tmp_path, monkeypatch, pulls)
        got = fp._marker_lookup(watch, "tok")(REPO, "0009-auth", stored)
        assert got["number"] == 31, stored
        assert ("list_pulls", 1) in watch.fake_forge.calls, stored


def test_the_marker_lookup_writes_nothing(tmp_path, monkeypatch):
    """`marked_pr` clears a page-suppression marker at the end of a full scan
    unless told it is a dry run. Plant the marker and check it survives."""
    watch = _watch(tmp_path, monkeypatch, [])
    leases = tmp_path / "fleet" / "leases"
    leases.mkdir(parents=True)
    key = watch._safe_lease_key(f"pagecap-operator-eunomia-{watch.PAGE_CAP}")
    marker = leases / f"{key}.watch-notified"
    marker.write_text("")
    assert fp._marker_lookup(watch, "tok")(REPO, "0009-auth") is None
    assert marker.exists()
    # ...and the same call without dry_run does clear it, so the check can fail
    watch.marked_pr(REPO, "0009-auth", "tok")
    assert not marker.exists()


# ---------------------------------------------------- a missing database is not a finding

@pytest.mark.parametrize("make", ["absent", "garbage", "no-view"])
def test_no_usable_database_means_every_plan_is_unknown_and_exit_zero(tmp_path, make):
    files = _plans(tmp_path, [("0001-a", "done"), ("0002-b", "ready"), ("0003-c", "done")])
    db = tmp_path / "fleet.db"
    if make == "garbage":
        db.write_bytes(b"this is not a sqlite file" * 100)
    elif make == "no-view":
        sqlite3.connect(str(db)).executescript("CREATE TABLE unrelated (x);")
    before = db.read_bytes() if db.exists() else None

    data = _ledger(files, db)
    assert all(r["outcome"] == "unknown" and r["class"] is None and r["runs"] is None
               for r in data["rows"])
    assert data["counts"]["claimed"] == 0           # absence of a record is not evidence
    assert data["counts"]["unknown"] == 3
    assert data["db"]["state"] in ("absent", "unreadable")
    assert (db.read_bytes() if db.exists() else None) == before     # and it was not touched

    out = subprocess.run([sys.executable, str(FLEET_PLAN), "ledger", "--db", str(db),
                          "--repo", REPO, *map(str, files)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "unknown" in out.stdout


def test_the_database_is_opened_read_only(tmp_path, monkeypatch):
    """Asserted on what is actually passed to sqlite — a text match on the word
    `mode=ro` is satisfied by a comment, which the mutation `mode=rw` proved."""
    files = _plans(tmp_path, [("0001-a", "ready")])
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=1)
    path = db.commit()
    before = path.read_bytes()

    seen = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        seen.append((target, kw))
        return real(target, *a, **kw)

    monkeypatch.setattr(fp.sqlite3, "connect", spy)
    _ledger(files, path)
    assert len(seen) == 1
    target, kw = seen[0]
    assert kw.get("uri") is True and target.endswith("?mode=ro"), target
    assert path.read_bytes() == before


# --------------------------------------------------------------- the two formats

def _cli(tmp_path, files, db, *extra):
    return subprocess.run([sys.executable, str(FLEET_PLAN), "ledger", "--db", str(db),
                           "--repo", REPO, *extra, *map(str, files)],
                          capture_output=True, text=True)


def _parse_table(text):
    lines = text.splitlines()
    head = next(i for i, l in enumerate(lines) if l.startswith("id "))
    cols = [c.strip() for c in lines[head].split("|")]
    out = []
    for l in lines[head + 2:]:
        if not l.strip():
            break
        cells = [c.strip() for c in l.split(" | ", len(cols) - 1)]
        out.append(dict(zip(cols, cells)))
    return cols, out


def _mixed(tmp_path):
    files = _plans(tmp_path, [("0001-a", "done"), ("0002-b", "ready"),
                              ("0003-c", "done"), ("0004-d", "ready")])
    db = Db(tmp_path)
    db.run("0002-b", "merged", pr=12, rounds=2, impl=(300, 40))
    db.run("0003-c", "failed", pr=13, reason="round 1: fix session timed out after 90 min")
    db.run("0004-d", None)
    return files, db.commit()


def test_json_and_table_carry_the_same_values(tmp_path):
    files, db = _mixed(tmp_path)
    as_json = json.loads(_cli(tmp_path, files, db, "--format", "json").stdout)
    cols, table = _parse_table(_cli(tmp_path, files, db).stdout)

    assert cols == list(fp.LEDGER_COLUMNS)
    assert [r["id"] for r in as_json["rows"]] == [t["id"] for t in table]
    for row, cells in zip(as_json["rows"], table):
        for col in cols:
            want = "-" if row[col] is None else str(row[col])
            assert cells[col] == want, (row["id"], col)
    assert {r["id"]: r["class"] for r in as_json["rows"]} == \
        {"0001-a": "claimed", "0002-b": None, "0003-c": "stalled", "0004-d": None}


def test_the_reading_note_is_in_both_formats(tmp_path):
    """§3: the report must carry the sentence, or `claimed` reads as a list of lies."""
    files, db = _mixed(tmp_path)
    sentence = "did someone decide this was finished"
    assert sentence in _cli(tmp_path, files, db).stdout
    assert sentence in " ".join(json.loads(_cli(tmp_path, files, db, "--format", "json").stdout)["notes"])


# --------------------------------------------- reports, never asserts, never writes

_LEDGER_SPAN = re.compile(r"# -+ ledger\n(.*?)# -+ end ledger", re.S)


def _section():
    return _LEDGER_SPAN.search(FLEET_PLAN.read_text()).group(1)


def test_the_ledger_has_no_write_path():
    """Grepped from the source: no file write, no removal, no SQL that mutates,
    and the only sqlite connection is `mode=ro`."""
    src = _section().replace("sys.stdout.write(", "")   # the report itself
    for token in (".write_text", ".write_bytes", "write(", "unlink(", "os.remove", "rmtree",
                  "shutil", "rename(", "os.replace", "touch(", "chmod", "os.open",
                  "INSERT", "UPDATE ", "DELETE", "DROP", "CREATE ", "ALTER", "COMMIT",
                  "executescript", "executemany"):
        assert token not in src, token
    assert not re.search(r"open\(", src)
    assert src.count("sqlite3.connect(") == 1 and "mode=ro" in src
    # git is only ever read, and only through helpers that already exist
    assert "subprocess" not in src


def test_running_the_ledger_leaves_plans_byte_identical(tmp_path):
    files, db = _mixed(tmp_path)
    before = {f: f.read_bytes() for f in files}
    for extra in ((), ("--format", "json")):
        assert _cli(tmp_path, files, db, *extra).returncode == 0
    assert {f: f.read_bytes() for f in files} == before


_VERDICT_WORDS = re.compile(r"\b(built|not_built|unbuilt|unfinished|incomplete|wrong|"
                            r"incorrect|lie|lies|lying|false|bogus|fake)\b", re.I)


def test_no_column_or_string_in_the_ledger_asserts_built_or_not():
    """The ledger's own vocabulary is provenance. Checked over every string
    literal and name in the section (docstrings and comments are prose about the
    rule, not output)."""
    tree = ast.parse("\n".join(
        l for l in _section().splitlines() if not l.lstrip().startswith("#")))
    strings = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant)
                    and isinstance(child.value.value, str)):
                continue                                    # a docstring
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                strings.append(child.value)
            elif isinstance(child, ast.Name):
                strings.append(child.id)
            walk(child)
    walk(tree)
    hits = [s for s in strings if _VERDICT_WORDS.search(s)]
    assert not hits, hits


def test_the_classes_and_outcomes_come_from_a_closed_vocabulary(tmp_path):
    files, db = _mixed(tmp_path)
    data = json.loads(_cli(tmp_path, files, db, "--format", "json").stdout)
    assert {r["class"] for r in data["rows"]} <= {"claimed", "stalled", None}
    assert {r["outcome"] for r in data["rows"]} <= \
        {"none", "unknown", "merged", "closed", "failed", "not-terminal"}
    assert not [c for c in fp.LEDGER_COLUMNS if _VERDICT_WORDS.search(c)]


# ------------------------------------------------- a stored PR state is as-of

def test_a_stored_pr_state_is_labelled_as_of_and_never_fresh(tmp_path):
    files = _plans(tmp_path, [("0001-a", "done")])
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=443)
    row = _rows(_ledger(files, db.commit()))["0001-a"]
    assert (row["pr"], row["pr_state"], row["pr_source"]) == (443, "merged", "fleet.db")
    assert row["pr_as_of"] == "2026-09-10T11:00:00Z"


def test_verdict_reports_the_forges_answer_when_it_disagrees_with_the_record(tmp_path):
    """Stored outcome `merged`, forge now says closed."""
    files = _plans(tmp_path, [("0001-a", "done")])
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=443)
    path = db.commit()

    asked = []

    def lookup(repo, pid, pr=None):
        asked.append((repo, pid))
        return {"number": 443, "state": "closed", "merged": False}

    without = _rows(_ledger(files, path))["0001-a"]
    with_v = _rows(_ledger(files, path, verdict=True, pr_lookup=lookup,
                           probe=lambda pid: {"state": "x", "detail": ""}))["0001-a"]
    assert without["pr_state"] == "merged" and without["pr_source"] == "fleet.db"
    assert with_v["pr_state"] == "closed" and with_v["pr_source"] == "forge"
    assert with_v["pr_as_of"] is None
    assert asked == [(REPO, "0001-a")]           # resolved by the plan id, i.e. the marker


def test_verdict_takes_the_pr_number_from_the_forge_when_the_marker_resolves_elsewhere(tmp_path):
    """A respawn can leave two PRs carrying one marker; the record kept the
    last one it saw. Under --verdict the forge's resolution wins, source and all."""
    files = _plans(tmp_path, [("0001-a", "done")])
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=443)
    row = _rows(_ledger(files, db.commit(), verdict=True,
                        probe=lambda pid: {"state": "x", "detail": ""},
                        pr_lookup=lambda r, p, n=None: {"number": 450, "state": "open"}))["0001-a"]
    assert (row["pr"], row["pr_state"], row["pr_source"]) == (450, "open", "forge")


def test_verdict_reads_the_forge_only_for_rows_that_have_a_stored_pr(tmp_path):
    files = _plans(tmp_path, [("0001-a", "done"), ("0002-b", "ready")])
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=5)
    asked = []
    _ledger(files, db.commit(), verdict=True, probe=lambda pid: {"state": "x", "detail": ""},
            pr_lookup=lambda r, p, n=None: asked.append(p) or {"number": 5, "state": "closed",
                                                        "merged": True})
    assert asked == ["0001-a"]


def test_an_unreadable_forge_leaves_the_state_as_of_and_says_so(tmp_path):
    files = _plans(tmp_path, [("0001-a", "done")])
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=5)
    data = _ledger(files, db.commit(), verdict=True, pr_lookup=lambda r, p, n=None: "unreadable",
                   probe=lambda pid: {"state": "x", "detail": ""})
    row = _rows(data)["0001-a"]
    assert row["pr_source"] == "fleet.db" and row["pr_state"] == "merged"
    assert data["forge"]["unreadable"] == 1
    assert "unreadable" in fp._ledger_table(data)


def test_a_marker_the_forge_cannot_find_is_reported_not_papered_over(tmp_path):
    files = _plans(tmp_path, [("0001-a", "done")])
    db = Db(tmp_path)
    db.run("0001-a", "merged", pr=5)
    row = _rows(_ledger(files, db.commit(), verdict=True, pr_lookup=lambda r, p, n=None: None,
                        probe=lambda pid: {"state": "x", "detail": ""}))["0001-a"]
    assert (row["pr_state"], row["pr_source"]) == ("no-marked-pr", "forge")


# ------------------------------------------------ --verdict: audit's probe, opt-in

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True)


def _git_repo(tmp_path, files):
    repo = tmp_path / "checkout"
    (repo / "plans").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    for f in files:
        (repo / "plans" / f.name).write_text(f.read_text())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "file plans")
    return repo


def test_verdict_runs_audits_probe_for_the_two_classes_only(tmp_path):
    files = _plans(tmp_path, [("0001-claimed", "done"), ("0002-stalled", "ready"),
                              ("0003-fine", "done")])
    db = Db(tmp_path)
    db.run("0002-stalled", "failed")
    db.run("0003-fine", "merged", pr=1)
    path = db.commit()
    repo = _git_repo(tmp_path, files)
    plan_files = [repo / "plans" / f.name for f in files]

    plain = _rows(fp.ledger(plan_files, db=path, repo=REPO, repo_root=repo, ref="main"))
    assert all(r["probe"] is None for r in plain.values())          # opt-in

    rows = _rows(fp.ledger(plan_files, db=path, repo=REPO, repo_root=repo, ref="main",
                           verdict=True, pr_lookup=lambda r, p, n=None: None))
    assert rows["0003-fine"]["probe"] is None
    for pid in ("0001-claimed", "0002-stalled"):
        # declared path bin/x exists nowhere in this checkout: audit's own answer,
        # quoted in its own column and never folded into `class` or `outcome`
        assert rows[pid]["probe"]["state"] == "not_built"
        assert "bin/x" in rows[pid]["probe"]["detail"]
    assert rows["0001-claimed"]["class"] == "claimed"


def test_the_probe_refuses_on_a_shallow_clone_rather_than_grading(tmp_path, monkeypatch):
    files = _plans(tmp_path, [("0001-claimed", "done")])
    path = Db(tmp_path).commit()
    monkeypatch.setattr(fp, "_is_shallow", lambda repo: True)
    row = _rows(fp.ledger(files, db=path, repo=REPO, verdict=True,
                          pr_lookup=lambda r, p, n=None: None))["0001-claimed"]
    assert row["probe"]["state"] == "refused"


# ------------------------------------------------------------ the real checkout

def test_the_live_ledger_scopes_to_this_repo_and_carries_only_what_it_can_prove():
    """Reads the real `plans/` and, when the host has one, the real fleet.db.
    Asserts only what cannot drift with either."""
    files = sorted((REPO_ROOT / "plans").glob("*.md"))
    live = os.environ.get("FLEET_DB") or str(Path.home() / "dev" / ".fleet" / "fleet.db")
    data = fp.ledger(files, db=Path(live), repo=REPO)

    ours = {fp.parse(f)[0]["id"] for f in files
            if (fp.parse(f)[0] or {}).get("repo", "").lower() == REPO}
    assert set(_rows(data)) == ours
    for r in data["rows"]:
        if r["class"] == "claimed":
            assert r["status"] == "done" and r["runs"] == 0
        if data["db"]["state"] != "ok":
            assert r["outcome"] == "unknown" and r["class"] is None
    assert data["counts"]["claimed"] == sum(r["class"] == "claimed" for r in data["rows"])


# ------------------------------------------------ plan 0076: what a run cost
#
# The fixture is the real shape: plan 0068's second run, its real `cwd`, and the
# `session.project` slug the CLI actually wrote for it (copied from fleet.db, not
# derived here). The numbers are chosen to print as the plan's `2.2M` / `30k`.

fr = _load(BIN / "fleet-reviews", "fleet_reviews_for_ledger")

REAL_CWD = ("/Users/operator/dev/.fleet/work/operator_s_eunomia/"
            "branch--feat-0068-the-dispatch-cap-is-configurat--002")
REAL_SLUG = ("-Users-operator-dev--fleet-work-operator-s-eunomia-"
             "branch--feat-0068-the-dispatch-cap-is-configurat--002")
COST_LEASE = "branch--feat-0068-the-dispatch-cap-is-configurat--002"
COST_PLAN = "0068-the-dispatch-cap"
UUID_RUN = "a41acbce-5428-423e-b522-bac0235ce8f9"
UUID_FIX = "6dfffa17-f31c-4b3f-81f1-2cfbe735d6f1"
UUID_REV = "0b1c2d3e-0000-4000-8000-000000000001"


def _sess(con, uuid, project, first, last, turns, in_tok=1000, out=30_000,
          cache_read=2_000_000, cache_write=200_000, parent=None):
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{project}/{uuid}.jsonl", uuid, project, parent,
                 "subagent" if parent else "session", first, last, turns, 0,
                 in_tok, out, cache_read, cache_write, "[]", "", ""))


def _cost_db(tmp_path, fix_round=True, fix_session=True):
    """One dispatch: a run pair and (optionally) a fix-round pair, on the same
    worktree; a PR with two review rounds, the first linked to its reviewer."""
    db = Db(tmp_path)
    fc.register_functions(db.con)
    db.con.executescript(fc.DISPATCH_SESSION_VIEW)
    db.con.executescript(fc.DISPATCH_COST_VIEW)
    db.con.executescript(fr.SCHEMA)
    L = COST_LEASE
    db._phase(L, 1, "2026-09-22T01:53:00Z", "start", repo=REPO, plan=COST_PLAN, session="s")
    db._phase(L, 2, "2026-09-22T01:53:19Z", "implementer-start", cwd=REAL_CWD)
    db._phase(L, 3, "2026-09-22T02:28:12Z", "implementer-exit", seconds=2093)
    _sess(db.con, UUID_RUN, REAL_SLUG, "2026-09-22T01:53:23.461Z",
          "2026-09-22T02:08:23.461Z", 32)
    seq = 3
    if fix_round:
        db._phase(L, 4, "2026-09-22T03:00:00Z", "implementer-start", cwd=REAL_CWD)
        db._phase(L, 5, "2026-09-22T03:10:00Z", "implementer-exit", seconds=600)
        seq = 5
        if fix_session:
            _sess(db.con, UUID_FIX, REAL_SLUG, "2026-09-22T03:00:04.000Z",
                  "2026-09-22T03:10:04.000Z", 10, cache_read=500_000, cache_write=0,
                  out=5_000)
    db._phase(L, seq + 1, "2026-09-22T03:20:00Z", "verified", pr=68)
    db._phase(L, seq + 2, "2026-09-22T03:30:00Z", "steward-end", state="merged")
    _sess(db.con, UUID_REV, "-Users-operator-dev--fleet-work-operator-s-eunomia-dispatch-review",
          "2026-09-22T03:11:00.000Z", "2026-09-22T03:16:00.000Z", 12,
          in_tok=50_000, out=21_000, cache_read=600_000, cache_write=0)
    for rid, rnd, verdict, sess in ((1, 1, "REQUEST_CHANGES", UUID_REV), (2, 2, "APPROVED", None)):
        db.con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict, "
                       "session_uuid) VALUES (?,?,?,?,?,?,?)",
                       (rid, REPO, 68, rnd, "revbot", verdict, sess))
    return db


def test_a_linked_run_and_its_rounds_print_beneath_the_summary_row(tmp_path):
    files = _plans(tmp_path, [(COST_PLAN, "ready")])
    path = _cost_db(tmp_path, fix_round=False).commit()
    out = _cli(tmp_path, files, path).stdout
    lines = out.splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith(COST_PLAN))
    assert lines[i - 2].startswith("id ")                  # the summary row and header: as before
    block = lines[i + 1:i + 5]
    assert block[0].startswith("    " + COST_LEASE) and "merged" in block[0]
    assert "processed 2.2M  output 30k  turns 32  minutes 15.0  sessions 1" in block[0]
    assert "partial" not in block[0]
    assert block[1].strip().startswith("round 1  REQUEST_CHANGES")
    assert "processed 650k" in block[1] and "output 21k" in block[1] and "minutes 5.0" in block[1]
    assert block[2].strip() == "round 2  APPROVED  processed —  output —  minutes —"
    # the total sums the run and the linked round, and says it is partial: round 2 is unlinked
    assert block[3].strip() == "total  processed 2.9M  output 51k  partial"


def test_two_linked_pairs_are_one_dispatch_line_with_the_sum(tmp_path):
    files = _plans(tmp_path, [(COST_PLAN, "ready")])
    path = _cost_db(tmp_path).commit()
    data = json.loads(_cli(tmp_path, files, path, "--format", "json").stdout)
    (row,) = data["rows"]
    (d,) = row["dispatches"]
    assert (d["lease"], d["outcome"], d["sessions"], d["unlinked"]) == (COST_LEASE, "merged", 2, 0)
    # the run: 1000 + 2,000,000 + 200,000; the fix round: 1000 + 500,000 + 0
    assert d["cost"] == {"processed": 2_201_000 + 501_000, "output": 35_000,
                         "turns": 42, "minutes": 25.0}
    assert isinstance(d["cost"]["processed"], int)


def test_an_unlinked_fix_round_leaves_the_run_numbers_and_says_partial(tmp_path):
    files = _plans(tmp_path, [(COST_PLAN, "ready")])
    path = _cost_db(tmp_path, fix_session=False).commit()
    out = _cli(tmp_path, files, path).stdout
    line = next(l for l in out.splitlines() if l.startswith("    " + COST_LEASE))
    assert "processed 2.2M  output 30k  turns 32  minutes 15.0  sessions 1  partial" in line
    data = json.loads(_cli(tmp_path, files, path, "--format", "json").stdout)
    (row,) = data["rows"]
    assert row["dispatches"][0]["sessions"] == 1 and row["dispatches"][0]["unlinked"] == 1
    assert row["total"]["partial"] is True


def test_a_fully_linked_plan_is_not_partial(tmp_path):
    files = _plans(tmp_path, [(COST_PLAN, "ready")])
    db = _cost_db(tmp_path)
    db.con.execute("UPDATE review SET session_uuid = ? WHERE forgejo_id = 2", (UUID_REV,))
    path = db.commit()
    (row,) = json.loads(_cli(tmp_path, files, path, "--format", "json").stdout)["rows"]
    assert row["total"]["partial"] is False
    assert [r["cost"] is not None for r in row["review_rounds"]] == [True, True]


def test_json_carries_dispatches_rounds_and_total_with_integers(tmp_path):
    files = _plans(tmp_path, [(COST_PLAN, "ready")])
    path = _cost_db(tmp_path, fix_round=False).commit()
    (row,) = json.loads(_cli(tmp_path, files, path, "--format", "json").stdout)["rows"]
    assert row["rounds"] is None or isinstance(row["rounds"], int)   # the summary count is untouched
    r1, r2 = row["review_rounds"]
    assert (r1["pr"], r1["round"], r1["verdict"]) == (68, 1, "REQUEST_CHANGES")
    assert r1["cost"]["processed"] == 650_000 and r1["cost"]["output"] == 21_000
    assert r2["cost"] is None
    assert row["total"]["processed"] == 2_201_000 + 650_000
    assert isinstance(row["total"]["output"], int)


def test_no_linked_session_and_no_flag_prints_only_the_summary(tmp_path):
    files = _plans(tmp_path, [("0002-b", "ready"), ("0003-c", "done")])
    db = Db(tmp_path)
    fc.register_functions(db.con)
    for sql in (fc.DISPATCH_SESSION_VIEW, fc.DISPATCH_COST_VIEW, fr.SCHEMA):
        db.con.executescript(sql)
    db.run("0002-b", "merged", pr=12, rounds=2, impl=(300, 40))
    db.run("0003-c", "failed", pr=13, reason="round 1: fix session timed out")
    path = db.commit()
    plain = _cli(tmp_path, files, path).stdout
    assert not [l for l in plain.splitlines() if l.startswith("    ")]
    assert "total" not in plain.split("\n\n", 1)[1].split("claimed")[0]
    with_cost = _cli(tmp_path, files, path, "--cost").stdout
    extra = [l for l in with_cost.splitlines() if l.startswith("    ")]
    assert extra, "--cost prints the block for every plan with dispatches"
    # taking the block back out leaves today's output, byte for byte
    kept = "\n".join(l for l in with_cost.splitlines() if not l.startswith("    ")) + "\n"
    assert kept == plain
    # a run nothing linked to prints `—`, never 0
    assert all("processed —" in l for l in extra if "sessions 0" in l)


def test_a_database_the_collector_has_not_extended_still_prints_the_summary(tmp_path):
    files, path = _mixed(tmp_path)         # Db() builds SCHEMA + `dispatch` only
    r = _cli(tmp_path, files, path, "--cost")
    assert r.returncode == 0
    assert "cost: unavailable" in r.stdout


def test_two_sessions_in_one_window_are_unlinked_not_guessed(tmp_path):
    files = _plans(tmp_path, [(COST_PLAN, "ready")])
    db = _cost_db(tmp_path, fix_round=False)
    _sess(db.con, "9999aaaa-0000-4000-8000-000000000009", REAL_SLUG,
          "2026-09-22T01:54:00.000Z", "2026-09-22T01:55:00.000Z", 3)
    path = db.commit()
    (row,) = json.loads(_cli(tmp_path, files, path, "--format", "json").stdout)["rows"]
    d = row["dispatches"][0]
    assert (d["sessions"], d["unlinked"], d["cost"]) == (0, 1, None)
