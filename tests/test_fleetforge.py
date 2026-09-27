"""Tests for fleetforge — the one door to the forge (plan 0022).

Two kinds of stub stand in for Forgejo:

  * `_stub_server()` — a real `http.server.HTTPServer` in a background thread,
    driven by a small routing callback, for everything that is "does the
    module issue the exact method/path/query a real HTTP round-trip sees" —
    the assertion table in plan 0022 §2/§4 and the percent-encoding cases.
  * `_raw_server()` — a bare TCP listener that answers with hand-written
    bytes (or nothing), for the two failure shapes an `HTTPServer` cannot
    produce on demand: a socket the peer closes with no response, and a
    response that is not a legal HTTP status line.

Both are real sockets, not mocked `urlopen` — a mock can only be wrong about
`urlopen`'s contract in the same way the code under test is; a socket cannot.
"""
import collections
import contextlib
import http.server
import importlib.machinery
import importlib.util
import json
import re
import socket
import threading
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin"
REPO_ROOT = Path(__file__).resolve().parent.parent

_loader = importlib.machinery.SourceFileLoader("fleetforge", str(BIN / "fleetforge.py"))
_spec = importlib.util.spec_from_loader("fleetforge", _loader)
fleetforge = importlib.util.module_from_spec(_spec)
_loader.exec_module(fleetforge)

TOKEN = "SECRET-TOK-abc123"


# --------------------------------------------------------------- stub server
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {"method": self.command, "path": self.path,
             "auth": self.headers.get("Authorization"), "body": raw_body})
        status, body = self.server.route(self.command, self.path, raw_body)
        if isinstance(body, (bytes, bytearray)):
            payload, ctype = bytes(body), None
        elif body is None:
            payload, ctype = b"", None
        else:
            payload, ctype = json.dumps(body).encode(), "application/json"
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle
    do_PATCH = _handle
    do_DELETE = _handle


@contextlib.contextmanager
def _stub_server(route):
    """`route(method, path, body_bytes) -> (status, body)`, `body` a dict/list
    (sent as JSON), raw bytes, or None (empty body)."""
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


@contextlib.contextmanager
def _raw_server(on_connect):
    """A bare listener: `on_connect(conn)` gets the accepted socket and must
    close it. Used for failure shapes `http.server` cannot produce: a peer
    that closes without a response, and a response that is not a legal HTTP
    status line."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            conn.recv(65536)
            on_connect(conn)
        finally:
            conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.close()
        t.join(timeout=5)


def _forge(base_url, token=TOKEN):
    return fleetforge.Forge(base_url, token)


# --------------------------------------------------------- verb → wire shape
# One row per verb in plan 0022 §2's inventory: the exact method + path (with
# query, as a dict so param order is not asserted) each verb must put on the
# wire, matching the open-coded call site it replaces.
def test_get_pull_hits_the_pull_path():
    seen = {}

    def route(method, path, body):
        seen.update(method=method, path=path)
        return 200, {"id": 1}

    with _stub_server(route) as (_, base):
        status, resp = _forge(base).get_pull("owner/repo", 42)
    assert (status, resp) == (200, {"id": 1})
    assert seen == {"method": "GET", "path": "/api/v1/repos/owner/repo/pulls/42"}


def test_list_pulls_hits_the_pulls_list_with_state_page_limit():
    seen = {}

    def route(method, path, body):
        p, _, q = path.partition("?")
        seen.update(method=method, p=p, q=_parse_qs(q))
        return 200, []

    with _stub_server(route) as (_, base):
        _forge(base).list_pulls("owner/repo", state="all", page=2, limit=50)
    assert seen["method"] == "GET"
    assert seen["p"] == "/api/v1/repos/owner/repo/pulls"
    assert seen["q"] == {"state": "all", "page": "2", "limit": "50"}


def test_list_pulls_sort_oldest_puts_exactly_that_on_the_wire():
    """fleet-watch:734-740 sends sort=oldest and its r2 M2 records that as
    correctness-critical for dedupe — the parameter must reach the wire
    unmodified, and must be ABSENT when not given (not sort=None as a
    literal string)."""
    seen = []

    def route(method, path, body):
        seen.append(path)
        return 200, []

    with _stub_server(route) as (_, base):
        forge = _forge(base)
        forge.list_pulls("owner/repo", state="all", page=1, limit=50, sort="oldest")
        forge.list_pulls("owner/repo", state="all", page=1, limit=50)
    q0 = _parse_qs(seen[0].partition("?")[2])
    q1 = _parse_qs(seen[1].partition("?")[2])
    assert q0["sort"] == "oldest"
    assert "sort" not in q1


def test_list_reviews_hits_the_reviews_path():
    seen = {}

    def route(method, path, body):
        seen.update(method=method, path=path.partition("?")[0])
        return 200, []

    with _stub_server(route) as (_, base):
        _forge(base).list_reviews("owner/repo", 7)
    assert seen == {"method": "GET", "path": "/api/v1/repos/owner/repo/pulls/7/reviews"}


def test_list_issue_comments_hits_the_comments_path_and_carries_since():
    """fleet-watch:1091-1093: `since` is a server-side bound, not decoration —
    it must reach the wire exactly, and be absent when not given."""
    seen = []

    def route(method, path, body):
        seen.append(path)
        return 200, []

    with _stub_server(route) as (_, base):
        forge = _forge(base)
        forge.list_issue_comments("owner/repo", 9, since="2026-09-01T00:00:00Z")
        forge.list_issue_comments("owner/repo", 9)
    p0, q0 = seen[0].split("?", 1)
    assert p0 == "/api/v1/repos/owner/repo/issues/9/comments"
    assert _parse_qs(q0)["since"] == "2026-09-01T00:00:00Z"
    p1 = seen[1].partition("?")[0]
    assert p1 == "/api/v1/repos/owner/repo/issues/9/comments"
    assert "since=" not in seen[1]


def test_get_branch_hits_the_branch_path():
    seen = {}

    def route(method, path, body):
        seen.update(method=method, path=path)
        return 200, {"commit": {"id": "abc"}}

    with _stub_server(route) as (_, base):
        _forge(base).get_branch("owner/repo", "main")
    assert seen == {"method": "GET", "path": "/api/v1/repos/owner/repo/branches/main"}


def test_get_branch_protections_hits_the_protections_path():
    seen = {}

    def route(method, path, body):
        seen.update(method=method, path=path)
        return 200, []

    with _stub_server(route) as (_, base):
        _forge(base).get_branch_protections("owner/repo")
    assert seen == {"method": "GET", "path": "/api/v1/repos/owner/repo/branch_protections"}


def test_get_contents_hits_the_contents_path_with_ref():
    seen = {}

    def route(method, path, body):
        p, _, q = path.partition("?")
        seen.update(method=method, p=p, q=_parse_qs(q))
        return 200, {"content": ""}

    with _stub_server(route) as (_, base):
        _forge(base).get_contents("owner/repo", "plans/0022-forge-module.md", ref="main")
    assert seen["method"] == "GET"
    assert seen["p"] == "/api/v1/repos/owner/repo/contents/plans/0022-forge-module.md"
    assert seen["q"] == {"ref": "main"}


def test_list_contents_hits_the_contents_path_for_a_directory():
    seen = {}

    def route(method, path, body):
        p, _, q = path.partition("?")
        seen.update(method=method, p=p, q=_parse_qs(q))
        return 200, []

    with _stub_server(route) as (_, base):
        _forge(base).list_contents("owner/repo", "plans", ref="main")
    assert seen["method"] == "GET"
    assert seen["p"] == "/api/v1/repos/owner/repo/contents/plans"
    assert seen["q"] == {"ref": "main"}


def test_post_status_hits_the_statuses_path_with_the_right_body():
    seen = {}

    def route(method, path, body):
        seen.update(method=method, path=path, body=json.loads(body))
        return 201, {}

    with _stub_server(route) as (_, base):
        status, _resp = _forge(base).post_status(
            "owner/repo", "deadbeef", "success", "base=abc merged clean", "candidate")
    assert status == 201
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/repos/owner/repo/statuses/deadbeef"
    assert seen["body"] == {"state": "success", "context": "candidate",
                            "description": "base=abc merged clean"}


def test_create_pull_hits_the_pulls_path_with_the_right_body():
    seen = {}

    def route(method, path, body):
        seen.update(method=method, path=path, body=json.loads(body))
        return 201, {"number": 9}

    with _stub_server(route) as (_, base):
        status, resp = _forge(base).create_pull(
            "owner/repo", "WIP: tests", "agent/tests/0070-x/sub-antigravity",
            "main", "Tests-for: 0070-x\n")
    assert status == 201
    assert resp == {"number": 9}
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/repos/owner/repo/pulls"
    assert seen["body"] == {"title": "WIP: tests",
                            "head": "agent/tests/0070-x/sub-antigravity",
                            "base": "main", "body": "Tests-for: 0070-x\n"}
    assert "draft" not in seen["body"]


def _parse_qs(q):
    import urllib.parse
    return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}


# ----------------------------------------------------------- the auth header
def test_the_token_reaches_the_authorization_header_and_nowhere_else():
    seen = {}

    def route(method, path, body):
        seen["auth"] = None
        return 200, {}

    with _stub_server(lambda m, p, b: (200, {})) as (httpd, base):
        _forge(base).get_pull("owner/repo", 1)
        req = httpd.requests[0]
    assert req["auth"] == f"token {TOKEN}"
    assert TOKEN not in req["path"]


# ------------------------------------------------------------- malformed repo
def test_a_malformed_repo_never_reaches_the_network():
    """`fleetlib.validate_repo` IS what checks shape (review 2119 on PR #116
    — see the module docstring) — it just never gets to raise past a verb:
    `validate_repo` calls `sys.exit`, each verb catches that `SystemExit`
    itself, and a malformed repo therefore forms no request at all rather
    than crashing or reaching the network."""
    def route(method, path, body):
        raise AssertionError("a malformed repo must never reach the network")

    with _stub_server(route) as (_, base):
        forge = _forge(base)
        assert forge.get_pull("not-a-repo", 1) == (0, None)
        assert forge.get_pull("too/many/slashes", 1) == (0, None)
        assert forge.list_pulls("") == (0, None)
        assert forge.post_status("", "sha", "success", "d", "c") == (0, None)
        assert forge.create_pull("", "t", "h", "main", "b") == (0, None)


def test_a_non_numeric_pr_or_issue_number_never_raises():
    """`int(n)` used to run in an f-string outside any try, so `get_pull`,
    `list_reviews` and `list_issue_comments` raised straight past the
    module's own never-raises boundary on a bad `n` (review 2146 LOW)."""
    def route(method, path, body):
        raise AssertionError("a malformed n must never reach the network")

    with _stub_server(route) as (_, base):
        forge = _forge(base)
        assert forge.get_pull("owner/repo", "abc") == (0, None)
        assert forge.list_reviews("owner/repo", None) == (0, None)
        assert forge.list_issue_comments("owner/repo", object()) == (0, None)


# ------------------------------------------------------------- HTTP failures
def test_http_error_statuses_are_status_none_and_never_raise():
    for code in (401, 403, 500):
        with _stub_server(lambda m, p, b, code=code: (code, b"nope")) as (_, base):
            status, body = _forge(base).get_pull("o/r", 1)
        assert (status, body) == (code, None)


def test_a_non_json_body_is_status_and_none_not_a_crash():
    with _stub_server(lambda m, p, b: (200, b"<html>sign in</html>")) as (_, base):
        status, body = _forge(base).get_pull("o/r", 1)
    assert (status, body) == (200, None)


def test_a_closed_socket_is_zero_none_and_never_raises():
    def on_connect(conn):
        conn.close()          # RST/EOF with no response at all

    with _raw_server(on_connect) as base:
        status, body = _forge(base).get_pull("o/r", 1)
    assert (status, body) == (0, None)


def test_a_malformed_status_line_is_zero_none_and_never_raises():
    def on_connect(conn):
        conn.sendall(b"NOT AN HTTP RESPONSE AT ALL\r\n\r\n")

    with _raw_server(on_connect) as base:
        status, body = _forge(base).get_pull("o/r", 1)
    assert (status, body) == (0, None)


def test_a_token_with_a_newline_fails_closed_and_never_reaches_the_wire(capsys):
    """urlopen's header-injection guard raises ValueError with the token
    embedded in str(e) ("Invalid header value b'token tok\\nX: 1'") — the
    exact reason that text is discarded rather than logged. No request should
    even be attempted: the header can't be built."""
    def route(method, path, body):
        raise AssertionError("a header-injecting token must never reach the network")

    with _stub_server(route) as (_, base):
        forge = _forge(base, token="tok\nX-Evil: 1")
        status, body = forge.get_pull("o/r", 1)
    assert (status, body) == (0, None)
    err = capsys.readouterr().err
    assert "tok" not in err and "Evil" not in err


# ---------------------------------------------------------- non-ASCII paths
def test_a_non_ascii_branch_name_is_percent_encoded_and_succeeds():
    """Unquoted, `urlopen` raises UnicodeEncodeError on a non-ASCII path
    segment (fleet-watch:566-568 documents the escape; fleet-candidate:163
    still had it before this migration — a non-ASCII base branch straight out
    of PR JSON). Before this module existed, `fleet-candidate`'s `_api`
    interpolated `base_ref` into the URL unquoted, so this exact input would
    have raised UnicodeEncodeError out of `urlopen` instead of returning
    (comment only, per plan 0022 §4 — that code no longer exists to call)."""
    seen = {}

    def route(method, path, body):
        seen["path"] = path
        return 200, {"commit": {"id": "abc"}}

    with _stub_server(route) as (_, base):
        status, resp = _forge(base).get_branch("owner/repo", "feature/café")
    assert status == 200
    assert seen["path"] == "/api/v1/repos/owner/repo/branches/feature%2Fcaf%C3%A9"


def test_a_non_ascii_contents_path_is_percent_encoded_segment_by_segment():
    seen = {}

    def route(method, path, body):
        seen["path"] = path.partition("?")[0]
        return 200, {"content": ""}

    with _stub_server(route) as (_, base):
        status, resp = _forge(base).get_contents("owner/repo", "plans/café/0001-x.md",
                                                  ref="main")
    assert status == 200
    assert seen["path"] == ("/api/v1/repos/owner/repo/contents/"
                            "plans/caf%C3%A9/0001-x.md")


# --------------------------------------------------------------------- paged
def test_paged_yields_pages_and_reports_exhausted_on_a_short_page():
    pages = {1: [{"n": i} for i in range(3)], 2: [{"n": 99}]}

    def verb(repo, page=1, limit=3):
        return 200, pages.get(page, [])

    p = fleetforge.paged(verb, "owner/repo", page_size=3)
    out = [page for page in p]
    assert out == [pages[1], pages[2]]
    assert p.outcome == fleetforge.EXHAUSTED


def test_paged_reports_cap_hit_distinctly_from_exhaustion():
    """docs/plan-dispatch.md: hitting the cap must read as unreadable, never
    as "no more results" — a truncated scan must not authorise a duplicate
    dispatch. cap-hit and exhausted must be distinguishable WITHOUT looking at
    the page contents, i.e. without comparing page length at the call site."""
    calls = []

    def verb(repo, page=1, limit=2):
        calls.append(page)
        return 200, [{"n": page * 10 + i} for i in range(2)]   # always full

    p = fleetforge.paged(verb, "owner/repo", cap=2, page_size=2)
    out = list(p)
    assert len(out) == 2
    assert p.outcome == fleetforge.CAP_HIT
    assert calls == [1, 2]


def test_paged_reports_failed_on_an_unreadable_page_and_stops():
    calls = []

    def verb(repo, page=1, limit=2):
        calls.append(page)
        if page == 1:
            return 200, [{"n": 1}, {"n": 2}]
        return 500, None

    p = fleetforge.paged(verb, "owner/repo", page_size=2)
    out = list(p)
    assert out == [[{"n": 1}, {"n": 2}]]
    assert p.outcome == fleetforge.FAILED
    assert p.failed_status == 500
    assert calls == [1, 2]


def test_paged_failed_status_distinguishes_an_http_error_from_a_transport_failure():
    """Collapsing every FAILED page into one string made an expired token
    (HTTP 401) indistinguishable from Forgejo being unreachable (status 0) —
    the pre-module `fleet-reviews` recorded `-> HTTP 401` / `-> URLError` for
    exactly this reason (review 2146 M1). `.failed_status` is how a caller
    tells them apart without inspecting page contents."""
    def verb_401(repo, page=1, limit=2):
        return 401, None

    p = fleetforge.paged(verb_401, "owner/repo", page_size=2)
    list(p)
    assert p.outcome == fleetforge.FAILED
    assert p.failed_status == 401

    def verb_down(repo, page=1, limit=2):
        return 0, None                      # what a verb returns on a
                                             # transport failure (never raises)

    p2 = fleetforge.paged(verb_down, "owner/repo", page_size=2)
    list(p2)
    assert p2.outcome == fleetforge.FAILED
    assert p2.failed_status == 0


def test_paged_failed_status_is_none_for_cap_hit_and_exhausted():
    def verb(repo, page=1, limit=2):
        return 200, [{"n": 1}, {"n": 2}]

    p = fleetforge.paged(verb, "owner/repo", cap=1, page_size=2)
    list(p)
    assert p.outcome == fleetforge.CAP_HIT
    assert p.failed_status is None

    def verb_short(repo, page=1, limit=2):
        return 200, [{"n": 1}]

    p2 = fleetforge.paged(verb_short, "owner/repo", page_size=2)
    list(p2)
    assert p2.outcome == fleetforge.EXHAUSTED
    assert p2.failed_status is None


def test_paged_unbounded_never_reports_cap_hit():
    """fleet-reviews' backfill: a read that stops early and reports 'complete'
    is worse than one that runs long, so its cap is None — unbounded."""
    def verb(repo, page=1, limit=50):
        return (200, [{"n": 1}] * 50) if page < 5 else (200, [{"n": 1}])

    p = fleetforge.paged(verb, "owner/repo", cap=None, page_size=50)
    list(p)
    assert p.outcome == fleetforge.EXHAUSTED


# ------------------------------------------------------------ repr / secrecy
def test_repr_and_vars_never_carry_the_token():
    forge = _forge("http://example.invalid")
    r = repr(forge)
    assert TOKEN not in r
    assert "example.invalid" in r
    try:
        v = vars(forge)
    except TypeError:
        pass                    # __slots__, no __dict__ — nothing to leak
    else:
        assert TOKEN not in repr(v)


def test_unsupported_kind_is_refused_at_construction():
    """`github` became a second real backend in plan 0052 — `gitlab` is the
    unsupported one now, and the message must name both the bad value and
    the kinds that do exist (plan 0052 D3)."""
    try:
        fleetforge.Forge("http://x", "t", kind="gitlab")
    except ValueError as e:
        assert "gitlab" in str(e)
        assert "forgejo" in str(e) and "github" in str(e)
    else:
        raise AssertionError("a third, unbuilt backend must not silently no-op")


def test_github_kind_is_a_real_backend_not_a_silent_forgejo():
    """The construction-time check must not treat every non-forgejo string as
    an error — `github` is a real, working backend (plan 0052), distinct from
    `forgejo` in the URL it builds."""
    forge = fleetforge.Forge("http://x", "t", kind="github")
    assert "api/v1" not in repr(forge)
    assert fleetforge.Forge("http://x", "t", kind="forgejo")._backend.prefix == "/api/v1"
    assert forge._backend.prefix == ""


# --------------------------------------------------------------- timeout
def test_forge_default_timeout_matches_fleet_candidates_pre_migration_value():
    """fleet-candidate's own urlopen() ran at 20s before this module existed;
    that stays the module default so fleet-candidate needed no change to
    keep it (review 2146 LOW)."""
    assert fleetforge.Forge("http://x", "t")._timeout == 20


def test_forge_honors_a_caller_supplied_timeout():
    """fleet-reviews' backfill ran at 30s pre-migration; a single hardcoded
    timeout in the module would have silently shortened it. A real slow
    peer (not a mock of urlopen) proves the value is actually wired to the
    request, not just stored (review 2146 LOW)."""
    import time

    def on_connect(conn):
        time.sleep(0.5)

    with _raw_server(on_connect) as base:
        forge = fleetforge.Forge(base, TOKEN, timeout=0.05)
        assert forge.get_pull("owner/repo", 1) == (0, None)


# ------------------------------------------------------- non-serializable body
def test_a_non_serializable_post_body_never_raises():
    """json.dumps(body) used to run outside the try in _request — a body
    that is not JSON-serializable raised TypeError straight past the
    module's never-raises boundary before a request was even attempted
    (review 2146 LOW)."""
    def route(method, path, body):
        raise AssertionError("a request must never be attempted")

    with _stub_server(route) as (_, base):
        forge = _forge(base)
        # post_status's body is built internally from JSON-safe strings, so
        # reach _request directly with a body that is not serializable.
        assert forge._request("POST", "/x", body={1, 2, 3}) == (0, None)


# --------------------------------------------- docs/forge.md migration ledger
# The DoD (plan 0022 §4) is explicit that this must NOT grep "api/v1" (that
# string is legitimate inside every _api()/_get() definition and inside
# fleet-reviews' own URL normaliser, and occurs zero times at a real call
# site) — it greps the call-shaped patterns instead, across bin/ outside
# fleetforge.py, and the hit set must equal a hand-enumerated set written
# here. A new open-coded call fails this test; so does a migrated one that
# was not removed from the enumeration.
#
# Pinned by FILE + the matched line's own TEXT, never by line number.
# Review 2146 H1: this used to be `"bin/fleet-watch:340"` etc., and every one
# of those numbers moved by +15 the moment `main` picked up unrelated commits
# above them — five draft plans (0004/0009/0013/0015/0016) lease this exact
# file, so that drift is not hypothetical, it is scheduled. Text is what
# actually identifies a call site; the line it happens to sit on today is not
# part of its identity. A real change (a call site added, removed, or its
# own text edited) still moves this pin — only unrelated edits elsewhere in
# the file no longer do (review 2146 M2, "the real fix for H1").
_ENUMERATED_CALL_SITES = [
    # fleet-watch's six Forgejo call sites migrated onto fleetforge.Forge in
    # plan 0051 (the watcher's own `_api` is gone); what remains below is the
    # LOCAL forgejo-broker socket (a different door, never migrating — see
    # the block further down) and the ntfy notification (not a forge call).
    # orchestrator: ANGELIA, not the forge. The fleet's push service, reached
    # over HTTP with its own per-app service token. It is enumerated here rather
    # than exempted from the grep because the value of this list is that ANY new
    # outbound call in bin/ gets a human decision — but it will never migrate to
    # fleetforge, which speaks only Forgejo.
    ("orchestrator", 'with urllib.request.urlopen(req, timeout=timeout) as resp:'),
    # fleet-watch: the LOCAL forgejo-broker (a unix-socket daemon, not a
    # direct Forgejo call) — a different door, not this plan's door.
    ("fleet-watch", 'class _UnixHTTPConnection(http.client.HTTPConnection):'),
    ("fleet-watch", '"""http.client over AF_UNIX. Fourteen lines of stdlib beats a dependency in'),
    ("fleet-watch", 'def _broker_get(path):'),
    ("fleet-watch", 'except (OSError, http.client.HTTPException):'),
    ("fleet-watch", 'st, body = _broker_get("/health")'),
    ("fleet-watch", 'st, body = _broker_get(f"/forgejo/protection/{owner_repo}")'),
    # fleet-watch: bare `import http.client`, needed by the two classes above.
    ("fleet-watch", "import http.client"),
    # ntfy notifications — not forge calls at all (PR #116's amendment to
    # plan 0022 names these three explicitly as out of scope).
    ("fleet-watch", "urllib.request.urlopen(req, timeout=5).read()"),
    ("fleet-leak-watch", "urllib.request.urlopen(req, timeout=5).read()"),
    ("orchestrator", "urllib.request.urlopen(req, timeout=5).read()"),
    # fleet-orphans (plan 0057): migrated. Its four previously-unreachable
    # reads (a bare repo GET, a paginated branches LIST, a commits list
    # filtered by sha/path, and /repos/search) are now `get_repo`,
    # `list_branches`, `list_commits` and `search_repos` on `Forge`; its
    # `_api`/`_get` are gone. Its own pagination boundary (page to a
    # CONFIRMED empty page, never stop on a short one — stricter than
    # `paged()`'s short-page-ends-the-scan shortcut) is preserved as a LOCAL
    # loop over the new verbs rather than `paged()` (plan 0057 D3;
    # docs/forge.md). What is left below is the ntfy notification — not a
    # forge call, same exception as the three tools above.
    ("fleet-orphans", "urllib.request.urlopen(req, timeout=5).read()"),
]


def _grep_call_sites():
    """Every bin/ line outside fleetforge.py matching one of the four
    open-coding shapes the module exists to replace, as a multiset of
    (filename, stripped line text) — not (filename, line number): a site's
    identity is what it says, not where it currently sits (review 2146
    H1/M2)."""
    pattern = re.compile(r"_api\(|_get\(|urlopen\(|http\.client")
    hits = collections.Counter()
    bindir = REPO_ROOT / "bin"
    for path in sorted(bindir.iterdir()):
        if not path.is_file() or path.name == "fleetforge.py":
            continue
        for line in path.read_text().splitlines():
            if pattern.search(line):
                hits[(path.name, line.strip())] += 1
    return hits


def test_the_hand_enumerated_call_site_set_matches_bin_today():
    """This is the test that fails the moment a new open-coded call appears,
    a migrated one is not removed from the enumeration above, or an
    enumerated site's own text changes — but NOT when an unrelated edit
    merely shifts these lines up or down within their file."""
    found = _grep_call_sites()
    enumerated = collections.Counter(_ENUMERATED_CALL_SITES)
    missing = found - enumerated
    stale = enumerated - found
    assert not missing, f"open-coded call site(s) not enumerated: {sorted(missing)}"
    assert not stale, f"enumerated site(s) no longer exist: {sorted(stale)}"


def test_fleet_reviews_and_fleet_candidate_have_no_open_coded_forge_calls():
    """The DoD's grep for `api/v1` (fleet-candidate) / `_api(`, `_get(` etc.
    (both) finds nothing at a call site outside fleetforge.py."""
    for name in ("fleet-reviews", "fleet-candidate"):
        text = (BIN / name).read_text()
        assert "_api(" not in text
        assert "_get(" not in text
        assert "urlopen(" not in text
        assert "http.client" not in text
    # fleet-candidate never legitimately says api/v1 (fleet-reviews does, in
    # its own normaliser comment/code, which is why only fleet-candidate is
    # checked here — see plan 0022 §4's amendment).
    cand = (BIN / "fleet-candidate").read_text()
    for i, line in enumerate(cand.splitlines(), 1):
        assert "api/v1" not in line, f"fleet-candidate:{i} still says api/v1: {line!r}"


def test_docs_forge_md_ledger_names_every_remaining_call_site():
    """The migration ledger's `file:line` table is a human-readable snapshot,
    explicitly NOT numerically enforced (review 2146 H1/M2 — see the table's
    own preamble in docs/forge.md): a plan editing `bin/fleet-watch` above
    one of these lines is expected and does not owe this doc or this test an
    update. What IS enforced, at the coarser grain a `file:line` table can
    actually promise, is per-FILE count: every bin/ file with an open-coded
    call is named in the ledger exactly as many times as it has matches live
    — not a superset (a file ledgered with more lines than it has left) and
    not a subset (a file with live matches absent from the ledger, or under-
    counted). A new open-coded call in a file already at its ledgered count,
    or a migration that removes a call without trimming its file's count,
    both fail this. The hand-enumerated-set test above is the finer-grained,
    per-call-site check that line-number drift cannot break."""
    doc = (REPO_ROOT / "docs" / "forge.md").read_text()
    ledgered = collections.Counter(
        m.split(":")[0] for m in re.findall(r"bin/[A-Za-z0-9_.-]+:\d+", doc))
    live = collections.Counter()
    for (name, _text), count in _grep_call_sites().items():
        live[f"bin/{name}"] += count
    missing = live - ledgered
    stale = ledgered - live
    assert not missing, f"docs/forge.md's ledger undercounts: {dict(missing)}"
    assert not stale, f"docs/forge.md's ledger overcounts (stale row?): {dict(stale)}"


# ------------------------------------------------------- path traversal
def test_a_dot_dot_component_is_refused_rather_than_encoded_or_dropped():
    """Encoding does not solve this. `urllib.parse.quote` treats `.` as an
    unreserved character and returns `..` verbatim at EVERY `safe=` setting, so
    the component reaches the forge intact and the contents API resolves it
    server-side — reading a path the caller never named.

    Refused, not dropped: rewriting `plans/../secrets` into `plans/secrets`
    answers a question nobody asked and the caller cannot tell it happened.
    `(0, None)` says the read did not occur, which is the one answer that must
    never be confused with "the file is not there"."""
    assert fleetforge._quote_path("plans/../../etc") is None
    assert fleetforge._quote_path("..") is None
    # the ordinary cases are unchanged, and a bare "." component is just noise
    assert fleetforge._quote_path("config/repos.conf") == "config/repos.conf"
    assert fleetforge._quote_path("./plans") == "plans"


def test_the_contents_verbs_refuse_a_traversal_without_calling_out():
    calls = []

    class _F(fleetforge.Forge):
        def _request(self, method, path, body=None, query=None):
            calls.append(path)
            return 200, []

    f = _F("http://example.invalid", "tok")
    assert f.get_contents("owner/repo", "plans/../../etc", ref="main") == (0, None)
    assert f.list_contents("owner/repo", "../secrets", ref="main") == (0, None)
    assert calls == [], "a traversal still put a request on the wire"
    # and the legitimate read still goes out
    assert f.get_contents("owner/repo", "plans", ref="main")[0] == 200
    assert calls == ["/repos/owner/repo/contents/plans"]
