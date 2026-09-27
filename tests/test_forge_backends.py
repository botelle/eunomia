"""Cross-backend contract tests for fleetforge (plan 0052).

`tests/test_fleetforge.py` already pins the Forgejo backend's exact wire
shape, verb by verb — that file is untouched here. This file exists for the
property plan 0052 D4 asks for and that file cannot express on its own: that
`kind="forgejo"` and `kind="github"` answer the SAME fourteen verbs with the
SAME `(status, body)` contract — same shape on success, same `(code, None)`
on an HTTP error, same `(0, None)` on a transport failure that never got a
status — so the two backends cannot silently drift apart from each other.

Every recorded response body below is a hand-written shape matching the
public GitHub and Forgejo/Gitea REST API documentation as of 2026-09-16, not
a live capture — plan 0052's boundary forbids testing against the real
GitHub API (no outbound credential for it on the cihost runner, and a
network-dependent failure would fail for a reason unrelated to the code
under test). They are close to identical for most verbs, which is itself
part of the point: fleetforge.py never inspects a body's fields, it only
ever decodes JSON and hands it back, so the two platforms' real field-level
differences (GitHub's branch commit key is `sha`, Forgejo's is `id`, for
instance) are none of this module's business.
"""
import contextlib
import http.server
import importlib.machinery
import importlib.util
import json
import socket
import threading
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent.parent / "bin"

_loader = importlib.machinery.SourceFileLoader("fleetforge", str(BIN / "fleetforge.py"))
_spec = importlib.util.spec_from_loader("fleetforge", _loader)
fleetforge = importlib.util.module_from_spec(_spec)
_loader.exec_module(fleetforge)

TOKEN = "SECRET-TOK-xyz789"
KINDS = ("forgejo", "github")


# --------------------------------------------------------------- stub server
class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        self.server.requests.append({"method": self.command, "path": self.path,
                                      "body": raw_body})
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
    do_POST = _handle
    do_PATCH = _handle


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


@contextlib.contextmanager
def _closed_socket_server():
    """A bare listener that accepts and immediately closes — the transport
    failure an `HTTPServer` cannot be made to produce on demand."""
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
        conn.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.close()
        t.join(timeout=5)


def _forge(base_url, kind):
    return fleetforge.Forge(base_url, TOKEN, kind=kind)


# ------------------------------------------------------- recorded shapes
# Hand-written, matching each platform's public docs (2026-09-16) — see the
# module docstring for why these are not live captures.
RECORDED = {
    "get_pull": {
        "forgejo": {"id": 1, "number": 42, "title": "x", "state": "open",
                    "merged": False, "base": {"ref": "main"}},
        "github": {"id": 1, "number": 42, "title": "x", "state": "open",
                   "merged": False, "base": {"ref": "main"}},
    },
    "list_pulls": {
        "forgejo": [{"id": 1, "number": 42, "state": "open"}],
        "github": [{"id": 1, "number": 42, "state": "open"}],
    },
    "list_reviews": {
        "forgejo": [{"id": 5, "state": "APPROVED"}],
        "github": [{"id": 5, "state": "APPROVED"}],
    },
    "list_issue_comments": {
        "forgejo": [{"id": 9, "body": "hi"}],
        "github": [{"id": 9, "body": "hi"}],
    },
    "get_branch": {
        # GitHub's branch commit key is `sha`; Forgejo's is `id`. fleetforge
        # never looks inside either — it is the caller's business, not the
        # transport's — which is exactly what this divergence is here to
        # prove.
        "forgejo": {"name": "main", "commit": {"id": "abc123"}, "protected": True},
        "github": {"name": "main", "commit": {"sha": "abc123"}, "protected": True},
    },
    "get_contents": {
        "forgejo": {"name": "x.md", "path": "plans/x.md", "type": "file",
                    "encoding": "base64", "content": "aGVsbG8="},
        "github": {"name": "x.md", "path": "plans/x.md", "type": "file",
                   "encoding": "base64", "content": "aGVsbG8="},
    },
    "list_contents": {
        "forgejo": [{"name": "x.md", "path": "plans/x.md", "type": "file"}],
        "github": [{"name": "x.md", "path": "plans/x.md", "type": "file"}],
    },
    "create_issue_comment": {
        "forgejo": {"id": 10, "body": "hi"},
        "github": {"id": 10, "body": "hi"},
    },
    "edit_pull_body": {
        "forgejo": {"id": 1, "number": 42, "body": "new body"},
        "github": {"id": 1, "number": 42, "body": "new body"},
    },
    "get_commit_status": {
        "forgejo": {"state": "success", "statuses": [{"context": "candidate"}]},
        "github": {"state": "success", "statuses": [{"context": "candidate"}],
                   "total_count": 1},
    },
    "post_status": {
        "forgejo": {"id": 1, "state": "success", "context": "candidate"},
        "github": {"id": 1, "state": "success", "context": "candidate"},
    },
    "get_repo": {
        "forgejo": {"id": 1, "name": "repo", "full_name": "owner/repo",
                    "default_branch": "main"},
        "github": {"id": 1, "name": "repo", "full_name": "owner/repo",
                   "default_branch": "main"},
    },
    "list_branches": {
        # Same commit-key divergence as get_branch above (`id` vs `sha`) —
        # fleetforge never looks inside either.
        "forgejo": [{"name": "main", "commit": {"id": "abc123"}, "protected": True}],
        "github": [{"name": "main", "commit": {"sha": "abc123"}, "protected": True}],
    },
    "list_commits": {
        "forgejo": [{"sha": "abc123", "commit": {"message": "x"}}],
        "github": [{"sha": "abc123", "commit": {"message": "x"}}],
    },
}

# (verb name, args to call it with) — one row per verb EXCEPT
# get_branch_protections, whose whole point is that it does NOT share this
# contract (see test_get_branch_protections_diverges_by_design below), and
# search_repos, whose path does not share the `/repos/` prefix every other
# verb does (see test_search_repos_diverges_by_wrapping_key_and_path below).
VERB_CALLS = [
    ("get_pull", ("owner/repo", 42)),
    ("list_pulls", ("owner/repo",)),
    ("list_reviews", ("owner/repo", 42)),
    ("list_issue_comments", ("owner/repo", 42)),
    ("get_branch", ("owner/repo", "main")),
    ("get_contents", ("owner/repo", "plans/x.md")),
    ("list_contents", ("owner/repo", "plans")),
    ("create_issue_comment", ("owner/repo", 42, "hi")),
    ("edit_pull_body", ("owner/repo", 42, "new body")),
    ("get_commit_status", ("owner/repo", "deadbeef")),
    ("post_status", ("owner/repo", "deadbeef", "success", "d", "candidate")),
    ("get_repo", ("owner/repo",)),
    ("list_branches", ("owner/repo",)),
    ("list_commits", ("owner/repo",)),
]

assert set(RECORDED) == {name for name, _ in VERB_CALLS}, (
    "every verb under test needs a recorded shape for both backends")


@pytest.mark.parametrize("verb_name,args", VERB_CALLS)
@pytest.mark.parametrize("kind", KINDS)
def test_the_verb_returns_status_and_the_recorded_body_on_success(kind, verb_name, args):
    recorded = RECORDED[verb_name][kind]

    def route(method, path, body):
        return 200, recorded

    with _stub_server(route) as (_, base):
        status, resp = getattr(_forge(base, kind), verb_name)(*args)
    assert status == 200
    assert resp == recorded


@pytest.mark.parametrize("verb_name,args", VERB_CALLS)
@pytest.mark.parametrize("kind", KINDS)
def test_the_verb_returns_code_none_on_a_404_regardless_of_backend(kind, verb_name, args):
    def route(method, path, body):
        return 404, {"message": "not found"}

    with _stub_server(route) as (_, base):
        status, resp = getattr(_forge(base, kind), verb_name)(*args)
    assert (status, resp) == (404, None)


@pytest.mark.parametrize("verb_name,args", VERB_CALLS)
@pytest.mark.parametrize("kind", KINDS)
def test_the_verb_returns_zero_none_on_a_transport_failure_regardless_of_backend(
        kind, verb_name, args):
    with _closed_socket_server() as base:
        status, resp = getattr(_forge(base, kind), verb_name)(*args)
    assert (status, resp) == (0, None)


# ------------------------------------------------ the prefix, D1's whole point
@pytest.mark.parametrize("verb_name,args", VERB_CALLS)
def test_forgejo_puts_api_v1_on_the_wire_and_github_never_does(verb_name, args):
    seen = {}

    def route(method, path, body):
        seen["path"] = path
        return 200, RECORDED[verb_name]["forgejo"]

    with _stub_server(route) as (_, base):
        getattr(_forge(base, "forgejo"), verb_name)(*args)
    assert seen["path"].startswith("/api/v1/")

    seen.clear()

    def route_gh(method, path, body):
        seen["path"] = path
        return 200, RECORDED[verb_name]["github"]

    with _stub_server(route_gh) as (_, base):
        getattr(_forge(base, "github"), verb_name)(*args)
    assert not seen["path"].startswith("/api/v1/")
    assert seen["path"].startswith("/repos/")


# --------------------------------------------------------- search_repos diverges
# Hand-written, matching each platform's public docs (2026-09-16) — see the
# module docstring. Forgejo's `/repos/search` wraps rows under `data`;
# GitHub's Search API wraps them under `items` (plus its own `total_count`/
# `incomplete_results` fields fleetforge never reads).
SEARCH_RECORDED = {
    "forgejo": {"ok": True, "data": [{"id": 1, "full_name": "owner/repo"}]},
    "github": {"total_count": 1, "incomplete_results": False,
               "items": [{"id": 1, "full_name": "owner/repo"}]},
}


@pytest.mark.parametrize("kind", KINDS)
def test_search_repos_returns_the_recorded_body_on_success(kind):
    recorded = SEARCH_RECORDED[kind]

    def route(method, path, body):
        return 200, recorded

    with _stub_server(route) as (_, base):
        status, resp = _forge(base, kind).search_repos()
    assert status == 200
    assert resp == recorded


@pytest.mark.parametrize("kind", KINDS)
def test_search_repos_returns_code_none_on_a_404_regardless_of_backend(kind):
    def route(method, path, body):
        return 404, {"message": "not found"}

    with _stub_server(route) as (_, base):
        status, resp = _forge(base, kind).search_repos()
    assert (status, resp) == (404, None)


@pytest.mark.parametrize("kind", KINDS)
def test_search_repos_returns_zero_none_on_a_transport_failure_regardless_of_backend(kind):
    with _closed_socket_server() as base:
        status, resp = _forge(base, kind).search_repos()
    assert (status, resp) == (0, None)


def test_search_repos_diverges_by_wrapping_key_and_path():
    """The one read verb this module documents as genuinely differently
    shaped across backends (plan 0057 boundary, alongside
    get_branch_protections above): Forgejo's `/repos/search` wraps its rows
    under `data`, and an empty query lists every repo the token can see;
    GitHub's nearest equivalent is its Search API (`/search/repositories`,
    no `/repos/` prefix), which wraps rows under `items` instead and
    requires a non-empty `q` in production — `_GithubBackend.search_repos`
    documents that as a known gap rather than inventing a query no caller
    here has ever asked for. fleetforge does not normalise the two into one
    shape; a caller reading either body already has to know which platform
    it is talking to."""
    seen = {}

    def route(method, path, body):
        seen["path"] = path
        return 200, SEARCH_RECORDED["forgejo"]

    with _stub_server(route) as (_, base):
        status, resp = _forge(base, "forgejo").search_repos()
    assert status == 200
    assert seen["path"].startswith("/api/v1/repos/search")
    assert "data" in resp and "items" not in resp

    seen.clear()

    def route_gh(method, path, body):
        seen["path"] = path
        return 200, SEARCH_RECORDED["github"]

    with _stub_server(route_gh) as (_, base):
        status, resp = _forge(base, "github").search_repos()
    assert status == 200
    assert seen["path"].startswith("/search/repositories")
    assert not seen["path"].startswith("/repos/")
    assert "items" in resp and "data" not in resp


# --------------------------------------------- get_branch_protections diverges
def test_get_branch_protections_diverges_by_design():
    """The one verb plan 0052 D2 documents as genuinely unable to share the
    contract: Forgejo answers with a list of protection rules; GitHub has no
    repo-wide equivalent (protection is per-branch, and shaped nothing like
    Forgejo's rule fields — see `_GithubBackend.get_branch_protections`'s own
    docstring). The github backend must return `(0, None)` WITHOUT putting a
    request on the wire at all, not silently reinterpret 404-as-unprotected or
    fabricate a rule list."""
    def route(method, path, body):
        return 200, [{"branch_name": "main", "required_approvals": 2,
                      "enable_push": False, "apply_to_admins": True}]

    with _stub_server(route) as (httpd, base):
        status, body = _forge(base, "forgejo").get_branch_protections("owner/repo")
        assert status == 200
        assert isinstance(body, list) and body[0]["branch_name"] == "main"
        forgejo_request_count = len(httpd.requests)

    def route_never(method, path, body):
        raise AssertionError("github's get_branch_protections must never hit the wire")

    with _stub_server(route_never) as (httpd, base):
        status, body = _forge(base, "github").get_branch_protections("owner/repo")
        assert (status, body) == (0, None)
        assert httpd.requests == []

    assert forgejo_request_count == 1


# --------------------------------------------------- never raises, either way
@pytest.mark.parametrize("kind", KINDS)
def test_a_malformed_repo_never_raises_on_either_backend(kind):
    forge = _forge("http://example.invalid", kind)
    assert forge.get_pull("not-a-repo", 1) == (0, None)
    assert forge.list_pulls("too/many/slashes") == (0, None)


@pytest.mark.parametrize("kind", KINDS)
def test_repr_never_carries_the_token_on_either_backend(kind):
    forge = _forge("http://example.invalid", kind)
    r = repr(forge)
    assert TOKEN not in r
    assert kind in r
