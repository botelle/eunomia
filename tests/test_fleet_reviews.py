"""Tests for fleet-reviews — the review ledger.

This tool had no tests, which is why review 1823 found three defects in it
that all share a shape: the record looked complete when it was not. Every
case below is one of those, pinned so it cannot come back quietly.

Plan 0022 migrated this tool onto `fleetforge.Forge`. The forge is now a real
stub `http.server` in a background thread — the module attributes these
tests used to inject (`_get`) no longer exist — and the DB is a tmp file."""
import contextlib
import http.server
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import threading
import urllib.parse
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"

_ENV = ("FLEET_DB", "FORGEJO_API", "FLEET_FORGEJO_URL", "FLEET_FORGE_OWNER",
        "FLEET_REVIEW_REPOS", "FLEET_REVIEWER", "FLEET_OVERDUE_SECONDS")


def _load(db, **env):
    for k in _ENV:
        os.environ.pop(k, None)
    os.environ["FLEET_DB"] = str(db)
    os.environ["FLEET_REVIEW_REPOS"] = "demo"
    os.environ["FLEET_REVIEWER"] = "revbot"
    for k, v in env.items():
        os.environ[k] = v
    loader = importlib.machinery.SourceFileLoader("fleet_reviews", str(BIN / "fleet-reviews"))
    spec = importlib.util.spec_from_loader("fleet_reviews", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod._FAILURES.clear()
    return mod


def _con(mod, db):
    con = sqlite3.connect(db)
    con.executescript(mod.SCHEMA)
    mod._migrate(con)
    return con


def _review(rid, state="APPROVED", at="2026-09-01T10:00:00Z", dismissed=False, body=""):
    return {"id": rid, "user": {"login": "revbot"}, "state": state,
            "submitted_at": at, "commit_id": "abc1234", "dismissed": dismissed,
            "stale": False, "body": body}


# ------------------------------------------------------------- stub forge API
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        self.server.requests.append(self.path)
        status, body = self.server.route(self.command, self.path, raw_body)
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _handle


@contextlib.contextmanager
def _stub_server(route):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    httpd.route = route
    httpd.requests = []
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        t.join(timeout=5)


def _page_of(path):
    q = urllib.parse.parse_qs(path.partition("?")[2])
    return int(q.get("page", ["1"])[0])


def _pulls_and_reviews_route(pulls_by_page, reviews_by_page):
    """A route dispatching on path shape: .../pulls -> pulls_by_page[page],
    .../pulls/N/reviews -> reviews_by_page[page]. Either value may be a plain
    list/None(->500) or a callable(page) for stateful tests."""
    def route(method, path, body):
        p = path.partition("?")[0]
        page = _page_of(path)
        if p.endswith("/reviews"):
            src = reviews_by_page
        elif p.endswith("/pulls"):
            src = pulls_by_page
        else:
            raise AssertionError(f"unexpected path {path}")
        val = src(page) if callable(src) else src.get(page, [])
        if val is None:
            return 500, None
        return 200, val
    return route


# ------------------------------------------------------------------ pagination
def test_paged_all_follows_pages_and_stops_on_a_short_one(tmp_path):
    mod = _load(tmp_path / "f.db")
    pages = {1: [{"n": i} for i in range(50)], 2: [{"n": 99}]}
    seen = []

    def verb(repo, page=1, limit=50):
        seen.append(page)
        return 200, pages.get(page, [])

    out = mod._paged_all(mod.fleetforge.paged(verb, "o/r"), "x")
    assert len(out) == 51
    assert seen == [1, 2], "stopped early or kept asking past a short page"


def test_a_failed_page_makes_the_whole_read_none(tmp_path):
    """A truncated read must not be indistinguishable from a short one."""
    mod = _load(tmp_path / "f.db")

    def verb(repo, page=1, limit=50):
        return (200, [{"n": i} for i in range(50)]) if page == 1 else (500, None)

    assert mod._paged_all(mod.fleetforge.paged(verb, "o/r"), "x") is None
    # HTTP 500, not the bare "failed" string: review 2146 M1 — collapsing
    # every FAILED page into one word made an expired token (401)
    # indistinguishable from Forgejo being down (0). _paged_all now surfaces
    # Paged.failed_status when it is a real (truthy) HTTP code.
    assert mod._FAILURES == ["x -> HTTP 500"]


def test_a_transport_failure_page_falls_back_to_the_bare_outcome(tmp_path):
    """status 0 (a transport failure — never an HTTP code) has no useful
    number to report, so _paged_all falls back to the outcome string, same
    as before review 2146 M1's fix for the HTTP-status case."""
    mod = _load(tmp_path / "f.db")

    def verb(repo, page=1, limit=50):
        return (200, [{"n": i} for i in range(50)]) if page == 1 else (0, None)

    assert mod._paged_all(mod.fleetforge.paged(verb, "o/r"), "x") is None
    assert mod._FAILURES == [f"x -> {mod.fleetforge.FAILED}"]


def test_backfill_pages_the_reviews_endpoint(tmp_path):
    """The REVIEWER filter runs after the fetch, so an unpaged read silently
    dropped this reviewer's later rounds on a busy PR."""
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    revs = {1: [_review(1000 + i, at=f"2026-09-01T10:{i:02d}:00Z") for i in range(50)],
            2: [_review(2000, at="2026-09-02T10:00:00Z")]}
    route = _pulls_and_reviews_route({1: [{"number": 7}], 2: []}, revs)

    with _stub_server(route) as (_, base):
        forge = mod.fleetforge.Forge(base, "tok")
        assert mod.backfill(con, forge) == 0
    assert con.execute("SELECT COUNT(*) FROM review").fetchone()[0] == 51


# ------------------------------------------------------------------ failures
def test_a_failed_request_makes_backfill_exit_non_zero(tmp_path, capsys):
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    route = _pulls_and_reviews_route({1: [{"number": 7}], 2: []}, lambda page: None)  # reviews -> 500

    with _stub_server(route) as (_, base):
        forge = mod.fleetforge.Forge(base, "tok")
        assert mod.backfill(con, forge) == 1
    assert "incomplete" in capsys.readouterr().err


# ------------------------------------------------------------------ dismissal
def test_a_dismissed_approval_is_not_counted_as_an_approval(tmp_path, capsys):
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict, "
                "dismissed) VALUES (1,'operator/demo',7,1,'revbot','APPROVED',1)")
    con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict, "
                "dismissed) VALUES (2,'operator/demo',7,2,'revbot','APPROVED',0)")
    con.commit()
    mod.report(con)
    out = capsys.readouterr().out
    row = [l for l in out.splitlines() if "operator/demo" in l][0]
    assert row.split()[2:4] == ["2", "1"], f"rounds/appr wrong: {row!r}"


def test_a_rerun_corrects_an_approval_that_was_later_dismissed(tmp_path):
    """Forgejo sets `dismissed` and leaves `state` as APPROVED, so the flag
    changes AFTER the row is written — any push to a branch does it. INSERT OR
    IGNORE froze the first observation and no re-run could ever correct it."""
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    state = {"dismissed": False}

    def reviews(page):
        return [_review(101, dismissed=state["dismissed"])] if page == 1 else []

    route = _pulls_and_reviews_route({1: [{"number": 7}], 2: []}, reviews)

    with _stub_server(route) as (_, base):
        forge = mod.fleetforge.Forge(base, "tok")
        assert mod.backfill(con, forge) == 0
        assert con.execute("SELECT dismissed FROM review WHERE forgejo_id=101").fetchone()[0] == 0

        state["dismissed"] = True
        mod._FAILURES.clear()
        assert mod.backfill(con, forge) == 0
    assert con.execute("SELECT dismissed FROM review WHERE forgejo_id=101").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM review").fetchone()[0] == 1, "duplicated"


# ------------------------------------------------------------------ migration
def test_migrate_adds_the_columns_to_a_pre_existing_table(tmp_path):
    """CREATE TABLE IF NOT EXISTS adds no columns to a table that exists, and a
    NULL `dismissed` makes `NOT r.dismissed` drop every historical row."""
    db = tmp_path / "f.db"
    con = sqlite3.connect(db)
    con.executescript("""CREATE TABLE review (forgejo_id INTEGER PRIMARY KEY,
        repo TEXT NOT NULL, pr INTEGER NOT NULL, round INTEGER,
        reviewer TEXT NOT NULL, model TEXT, provenance TEXT, verdict TEXT,
        head_sha TEXT, submitted_at TEXT, session_uuid TEXT);""")
    con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict) "
                "VALUES (1,'operator/demo',7,1,'revbot','APPROVED')")
    con.commit()
    mod = _load(db)
    mod._migrate(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(review)")}
    assert {"dismissed", "stale"} <= cols
    assert con.execute("SELECT dismissed FROM review WHERE forgejo_id=1").fetchone()[0] == 0


def test_a_bare_repo_name_is_qualified_rather_than_matching_nothing(tmp_path, capsys):
    """Rows are stored as operator/<repo>, so `--repo eunomia` matched nothing
    and printed a clean "0 pull requests" — a wrong answer wearing the shape of
    a finding."""
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict) "
                "VALUES (1,'operator/demo',7,1,'revbot','APPROVED')")
    con.commit()
    mod.report(con, "demo")
    assert "operator/demo" in capsys.readouterr().out


def test_an_unknown_repo_says_so_instead_of_reporting_an_empty_census(tmp_path, capsys):
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    con.execute("INSERT INTO review (forgejo_id, repo, pr, round, reviewer, verdict) "
                "VALUES (1,'operator/demo',7,1,'revbot','APPROVED')")
    con.commit()
    mod.report(con, "nosuchrepo")
    cap = capsys.readouterr()
    assert "no reviews recorded" in cap.err and "operator/demo" in cap.err
    assert "pull requests" not in cap.out


# --------------------------------------------------------------- plan 0022
def test_qualify_prefixes_a_bare_name_and_passes_a_full_one_through(tmp_path):
    mod = _load(tmp_path / "f.db", FLEET_FORGE_OWNER="acme")
    assert mod._qualify("widgets") == "acme/widgets"
    assert mod._qualify("other/widgets") == "other/widgets"


def test_a_bare_repo_and_a_full_repo_hit_the_same_backfill_path(tmp_path):
    """`fleet-reviews repo-name` and `fleet-reviews owner/repo-name` must hit
    the same path — the owner-prefixing happens once, before the verb, not per
    request-building call site."""
    hits = []

    def route(method, path, body):
        hits.append(path.partition("?")[0])
        return 200, []

    for repo_entry in ("demo", "operator/demo"):
        db = tmp_path / f"{repo_entry.replace('/', '_')}.db"
        mod = _load(db, FLEET_REVIEW_REPOS=repo_entry)
        con = _con(mod, db)
        with _stub_server(route) as (_, base):
            forge = mod.fleetforge.Forge(base, "tok")
            mod.backfill(con, forge)
    assert len(hits) == 2 and hits[0] == hits[1] == "/api/v1/repos/operator/demo/pulls"


def test_forgejo_url_is_the_default_when_nothing_is_set(tmp_path):
    mod = _load(tmp_path / "f.db")
    assert mod._forgejo_base_url() == mod._DEFAULT_FORGEJO_URL


def test_fleet_forgejo_url_wins_and_is_silent(tmp_path, capsys):
    mod = _load(tmp_path / "f.db", FLEET_FORGEJO_URL="http://example.invalid:9/")
    assert mod._forgejo_base_url() == "http://example.invalid:9"
    assert capsys.readouterr().err == ""


def test_forgejo_api_fallback_is_normalised_and_warns_once(tmp_path, capsys):
    """The existing fixture historically set the BARE origin for FORGEJO_API,
    which would pass this even with no normalisation at all. The suffix here
    is the point of the test: FORGEJO_API is shaped <origin>/api/v1, and a
    naive fallback that concatenates it again requests /api/v1/api/v1/... and
    404s on exactly the host this fallback exists for."""
    mod = _load(tmp_path / "f.db", FORGEJO_API="http://example.invalid:9/api/v1")
    assert mod._forgejo_base_url() == "http://example.invalid:9"
    err = capsys.readouterr().err
    assert err.count("FLEET_FORGEJO_URL") >= 1
    assert err.strip().count("\n") == 0, "must warn exactly once per invocation"


def test_fleet_forgejo_url_wins_over_forgejo_api_when_both_are_set(tmp_path, capsys):
    mod = _load(tmp_path / "f.db", FLEET_FORGEJO_URL="http://new.invalid:1",
                FORGEJO_API="http://old.invalid:2/api/v1")
    assert mod._forgejo_base_url() == "http://new.invalid:1"


def test_a_malformed_repo_in_the_list_is_a_failure_not_a_crash(tmp_path, capsys):
    """A malformed repo string reaching a verb must record one _FAILURES
    entry and let backfill continue with the remaining repos — never die
    inside a verb (plan 0022 §4's amendment)."""
    db = tmp_path / "f.db"
    mod = _load(db, FLEET_REVIEW_REPOS="not/a/valid/repo,demo")
    con = _con(mod, db)
    route = _pulls_and_reviews_route({1: [{"number": 7}], 2: []},
                                     {1: [_review(1)], 2: []})

    with _stub_server(route) as (httpd, base):
        forge = mod.fleetforge.Forge(base, "tok")
        rc = mod.backfill(con, forge)
    assert rc == 1
    assert len(mod._FAILURES) == 1
    # the good repo was still processed — one bad entry did not end the run
    assert con.execute("SELECT COUNT(*) FROM review").fetchone()[0] == 1
    assert "incomplete" in capsys.readouterr().err


def test_no_open_coded_forge_calls_outside_the_normaliser(tmp_path):
    """The DoD grep for `_api(`/`_get(`/`urlopen(`/`http.client` finds nothing
    outside fleetforge.py; `api/v1` legitimately appears in this file's own
    URL normaliser (comment and code), so that string is NOT asserted absent
    here (unlike fleet-candidate, which never says it at all)."""
    text = (BIN / "fleet-reviews").read_text()
    assert "_api(" not in text
    assert "_get(" not in text
    assert "urlopen(" not in text
    assert "http.client" not in text


# ------------------------------------------- plan 0076: the reviewer's session

SESSION = "a41acbce-5428-423e-b522-bac0235ce8f9"
OTHER_SESSION = "6dfffa17-f31c-4b3f-81f1-2cfbe735d6f1"


def _backfill_bodies(mod, con, bodies):
    """Backfill PR 7 whose reviews carry `bodies` ({review id: body})."""
    revs = [_review(rid, at=f"2026-09-01T10:{i:02d}:00Z", body=b)
            for i, (rid, b) in enumerate(bodies.items())]
    route = _pulls_and_reviews_route({1: [{"number": 7}], 2: []}, {1: revs, 2: []})
    mod._FAILURES.clear()
    with _stub_server(route) as (_, base):
        assert mod.backfill(con, mod.fleetforge.Forge(base, "tok")) == 0


def _row(con, rid):
    return con.execute("SELECT model, provenance, session_uuid FROM review "
                       "WHERE forgejo_id=?", (rid,)).fetchone()


def test_a_three_field_provenance_line_fills_the_session_column(tmp_path):
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    _backfill_bodies(mod, con, {
        1: f"looks fine\n\nReview-provenance: model=claude-opus-5; session={SESSION}; "
           f"same-session-as-author=no",
        2: "Review-provenance: model=claude-opus-5; same-session-as-author=no",
        3: "no provenance at all"})
    assert _row(con, 1) == ("claude-opus-5", f"session={SESSION}; same-session-as-author=no", SESSION)
    # the two-field shape parses exactly as before, and leaves the column NULL
    assert _row(con, 2) == ("claude-opus-5", "same-session-as-author=no", None)
    assert _row(con, 3) == (None, None, None)


def test_the_session_is_read_wherever_it_sits_in_the_line_and_never_from_prose(tmp_path):
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    _backfill_bodies(mod, con, {
        1: f"Review-provenance: model=m; same-session-as-author=no; session={SESSION}",
        2: f"Review-provenance: model=m; x-session={OTHER_SESSION}",       # a different key
        3: f"Review-provenance: model=m\nsession={OTHER_SESSION}"})        # on another line
    assert _row(con, 1)[2] == SESSION
    assert _row(con, 2)[2] is None
    assert _row(con, 3)[2] is None


def test_a_backfill_fills_a_null_session_and_never_replaces_a_value(tmp_path):
    db = tmp_path / "f.db"
    mod = _load(db)
    con = _con(mod, db)
    _backfill_bodies(mod, con, {1: "Review-provenance: model=m; same-session-as-author=no"})
    assert _row(con, 1)[2] is None

    # the same review, re-read with the field now present: the NULL is filled
    _backfill_bodies(mod, con, {1: f"Review-provenance: model=m; session={SESSION}"})
    assert _row(con, 1)[2] == SESSION

    # re-read with the field absent, or with a different one: the value stays
    _backfill_bodies(mod, con, {1: "Review-provenance: model=m; same-session-as-author=no"})
    assert _row(con, 1)[2] == SESSION
    _backfill_bodies(mod, con, {1: f"Review-provenance: model=m; session={OTHER_SESSION}"})
    assert _row(con, 1)[2] == SESSION
    assert con.execute("SELECT COUNT(*) FROM review").fetchone()[0] == 1
