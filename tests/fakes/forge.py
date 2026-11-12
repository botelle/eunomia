"""An in-memory Forgejo for rehearsing the review loop (plan 0099 D1).

Serves the `/api/v1` paths that `bin/fleetforge.py`'s Forgejo backend builds for
the verbs the loop uses, on 127.0.0.1 with an OS-chosen port, and NOTHING else:
any other path answers 501 and is recorded, and `assert_no_unmodelled()` turns
that record into a failure that names the path. A fake that answered an unknown
path with `{}` would let a new forge call pass with no model of it.

The forge and the scenario's bare git remote are one thing here, as they are in
production: a pull's head sha is whatever `refs/heads/<head>` is in the bare
repo right now, and `contents` is read from it. So an implementer that pushes a
fix moves the head with no bookkeeping, and a review posted against the old sha
is then stale by construction.

Auth is checked, not ignored: a request whose `Authorization: token X` is not in
the token table gets 401, and the login the token maps to becomes the author of
whatever it creates. That is what lets a row assert who posted a review.
"""
import base64
import json
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

API = "/api/v1"
_REPO = r"/repos/(?P<o>[^/]+)/(?P<r>[^/]+)"

# login -> forge user id. Only the logins the loop distinguishes.
USER_IDS = {"implbot": 2, "revbot": 3, "operator": 1}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class FakeForge:
    def __init__(self, repos, tokens, ci=None, merge_on_approve=True):
        """`repos`: {"owner/name": path to its bare git repo}.
        `tokens`: {token: login}.
        `ci`: the combined status served for every sha unless `set_ci` says
        otherwise; default is one green `CI / tests` context.
        `merge_on_approve`: an APPROVED review marks its pull merged, standing
        in for the human who merges the moment approval lands. Without it the
        steward would poll forever, which is the one thing a rehearsal of the
        approve path must not do."""
        self.repos = dict(repos)
        self.tokens = dict(tokens)
        self.merge_on_approve = merge_on_approve
        self._ci = ci if ci is not None else {
            "state": "success",
            "statuses": [{"context": "CI / tests", "status": "success"}]}
        self._ci_by_sha = {}
        self.pulls = []                  # dicts, number = index + 1
        self.reviews = {}                # pr -> [review]
        self.comments = {}               # pr -> [comment]
        self.statuses = {}               # sha -> [status] posted through the API
        self.action_runs = []            # workflow runs, newest first; empty = no CI history
        self.requests = []               # every request, in order
        self.unmodelled = []             # "METHOD /path" the fake did not serve
        self.ntfy = []                   # bodies POSTed to /ntfy
        self._id = 0
        self._lock = threading.RLock()
        self._httpd = None
        self.url = None

    # ------------------------------------------------------------ lifecycle
    def start(self):
        forge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep pytest output clean
                pass

            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                status, payload = forge._handle(
                    self.command, self.path, raw,
                    self.headers.get("Authorization", ""))
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _serve

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    # ------------------------------------------------------------- test API
    def set_ci(self, sha, state, statuses):
        self._ci_by_sha[sha] = {"state": state, "statuses": statuses}

    def set_default_ci(self, state, statuses):
        self._ci = {"state": state, "statuses": statuses}

    def merge(self, number):
        with self._lock:
            self._pull(number)["merged"] = True
            self._pull(number)["state"] = "closed"

    def reviews_of(self, number):
        with self._lock:
            return [dict(r) for r in self.reviews.get(number, [])]

    def comments_of(self, number):
        with self._lock:
            return [dict(c) for c in self.comments.get(number, [])]

    def requests_matching(self, method, pattern):
        rx = re.compile(pattern)
        with self._lock:
            return [r for r in self.requests
                    if r["method"] == method and rx.search(r["path"])]

    def assert_no_unmodelled(self):
        """A row that made the loop call something this fake does not model
        fails HERE, naming the path — never as a wrong-looking outcome three
        assertions later."""
        with self._lock:
            if self.unmodelled:
                raise AssertionError(
                    "the fake forge does not model: " + ", ".join(self.unmodelled)
                    + " — add it to tests/fakes/forge.py with a model of what "
                      "the forge does, or the loop's new call is untested")

    # ------------------------------------------------------------- internals
    def _next_id(self):
        self._id += 1
        return self._id

    def _pull(self, number):
        return self.pulls[int(number) - 1]

    def _git(self, repo, *args):
        r = subprocess.run(["git", "-C", self.repos[repo], *args],
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout

    def _head_sha(self, repo, ref):
        rc, out = self._git(repo, "rev-parse", "--verify", "--quiet",
                            f"refs/heads/{ref}")
        return out.strip() if rc == 0 else None

    def _user(self, login):
        return {"id": USER_IDS.get(login, 99), "login": login}

    def _pull_json(self, repo, p):
        out = dict(p)
        out["head"] = {"ref": p["head_ref"],
                       "sha": self._head_sha(repo, p["head_ref"]) or ""}
        out["base"] = {"ref": p["base_ref"]}
        for k in ("head_ref", "base_ref", "repo"):
            out.pop(k, None)
        return out

    def _handle(self, method, target, raw, auth):
        parts = urlsplit(target)
        path, query = unquote(parts.path), parse_qs(parts.query)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        with self._lock:
            token = auth.split(" ", 1)[1].strip() if auth.startswith("token ") else ""
            login = self.tokens.get(token)
            self.requests.append({"method": method, "path": path + (
                "?" + parts.query if parts.query else ""), "body": body,
                "login": login})
            if method == "POST" and path == "/ntfy":
                self.ntfy.append(raw.decode("utf-8", "replace"))
                return 200, {}
            if not path.startswith(API + "/"):
                return self._unmodelled(method, path)
            if login is None:
                return 401, None
            return self._route(method, path[len(API):], query, body, login)

    def _unmodelled(self, method, path):
        self.unmodelled.append(f"{method} {path}")
        return 501, {"message": "not modelled by tests/fakes/forge.py"}

    def _page(self, items, query):
        page = int((query.get("page") or ["1"])[0])
        limit = int((query.get("limit") or ["50"])[0])
        return items[(page - 1) * limit: page * limit]

    def _route(self, method, path, query, body, login):
        m = re.fullmatch(_REPO, path)
        if m and method == "GET":
            repo = f"{m['o']}/{m['r']}"
            if repo not in self.repos:
                return 404, None
            return 200, {"full_name": repo, "default_branch": "main"}

        m = re.fullmatch(_REPO + r"/branch_protections", path)
        if m and method == "GET":
            return 200, [{"rule_name": "main", "required_approvals": 2}]

        m = re.fullmatch(_REPO + r"/pulls", path)
        if m:
            repo = f"{m['o']}/{m['r']}"
            if repo not in self.repos:
                return 404, None
            if method == "GET":
                state = (query.get("state") or ["all"])[0]
                rows = [self._pull_json(repo, p) for p in self.pulls
                        if p["repo"] == repo and state in ("all", p["state"])]
                rows.reverse()                   # newest first, as Forgejo does
                return 200, self._page(rows, query)
            if method == "POST" and isinstance(body, dict):
                p = {"number": len(self.pulls) + 1, "repo": repo,
                     "title": body.get("title", ""), "body": body.get("body", ""),
                     "state": "open", "merged": False, "draft": False,
                     "head_ref": body.get("head", ""),
                     "base_ref": body.get("base", "main"),
                     "user": self._user(login), "created_at": _now()}
                self.pulls.append(p)
                return 201, self._pull_json(repo, p)

        m = re.fullmatch(_REPO + r"/pulls/(?P<n>\d+)", path)
        if m:
            repo, n = f"{m['o']}/{m['r']}", int(m["n"])
            if n > len(self.pulls) or self._pull(n)["repo"] != repo:
                return 404, None
            if method == "GET":
                return 200, self._pull_json(repo, self._pull(n))
            if method == "PATCH" and isinstance(body, dict):
                # body only — the same narrowing fleetforge.edit_pull_body has
                if "body" in body:
                    self._pull(n)["body"] = body["body"]
                return 201, self._pull_json(repo, self._pull(n))

        m = re.fullmatch(_REPO + r"/pulls/(?P<n>\d+)/reviews", path)
        if m:
            repo, n = f"{m['o']}/{m['r']}", int(m["n"])
            if n > len(self.pulls) or self._pull(n)["repo"] != repo:
                return 404, None
            if method == "GET":
                return 200, self._page(list(self.reviews.get(n, [])), query)
            if method == "POST" and isinstance(body, dict):
                event = (body.get("event") or "COMMENT").upper()
                state = {"APPROVE": "APPROVED"}.get(event, event)
                if state not in ("APPROVED", "REQUEST_CHANGES", "COMMENT"):
                    return 422, None
                review = {"id": self._next_id(), "state": state,
                          "body": body.get("body", ""),
                          # Forgejo binds a review to the commit it was made on
                          "commit_id": body.get("commit_id")
                          or self._head_sha(repo, self._pull(n)["head_ref"]),
                          "stale": False, "dismissed": False,
                          "user": self._user(login), "submitted_at": _now()}
                self.reviews.setdefault(n, []).append(review)
                if state == "APPROVED" and self.merge_on_approve:
                    self._pull(n)["merged"] = True
                    self._pull(n)["state"] = "closed"
                return 200, review

        # Checked after `/pulls/<n>` and `/pulls/<n>/reviews` above, both
        # anchored to digits-only, so a numeric PR path never reaches here —
        # this one's `.+` would otherwise swallow `/pulls/<n>/reviews` too,
        # since a head branch name may itself contain a `/`.
        m = re.fullmatch(_REPO + r"/pulls/(?P<base>[^/]+)/(?P<head>.+)", path)
        if m and method == "GET":
            repo = f"{m['o']}/{m['r']}"
            if repo not in self.repos:
                return 404, None
            candidates = [p for p in self.pulls
                          if p["repo"] == repo and p["base_ref"] == m["base"]
                          and p["head_ref"] == m["head"]]
            if not candidates:
                return 404, None
            # No live case has two pulls sharing one head (plan 0215's
            # `get_pull_by_branch` docstring) — an open one wins over a
            # closed one, and the newest wins any remaining tie, matching
            # what the head filter it replaces effectively did.
            candidates.sort(key=lambda p: (p["state"] == "open", p["number"]))
            return 200, self._pull_json(repo, candidates[-1])

        m = re.fullmatch(_REPO + r"/issues/(?P<n>\d+)/comments", path)
        if m:
            n = int(m["n"])
            if method == "GET":
                return 200, self._page(list(self.comments.get(n, [])), query)
            if method == "POST" and isinstance(body, dict):
                c = {"id": self._next_id(), "body": body.get("body", ""),
                     "user": self._user(login), "created_at": _now()}
                self.comments.setdefault(n, []).append(c)
                return 201, c

        m = re.fullmatch(_REPO + r"/commits/(?P<sha>[^/]+)/status", path)
        if m and method == "GET":
            sha = m["sha"]
            combined = self._ci_by_sha.get(sha, self._ci)
            posted = self.statuses.get(sha, [])
            statuses = list(combined["statuses"]) + posted
            return 200, {"state": combined["state"], "sha": sha,
                         "statuses": statuses}

        m = re.fullmatch(_REPO + r"/actions/runs", path)
        if m and method == "GET":
            if f"{m['o']}/{m['r']}" not in self.repos:
                return 404, None
            # Forgejo's wrapper, not a bare list; paged by hand by the caller
            return 200, {"total_count": len(self.action_runs),
                         "workflow_runs": self._page(
                             list(self.action_runs), query)}

        m = re.fullmatch(_REPO + r"/statuses/(?P<sha>[^/]+)", path)
        if m and method == "POST" and isinstance(body, dict):
            self.statuses.setdefault(m["sha"], []).append(
                {"context": body.get("context", ""),
                 "status": body.get("state", ""),
                 "description": body.get("description", "")})
            return 201, {"context": body.get("context", "")}

        m = re.fullmatch(_REPO + r"/contents/(?P<p>.+)", path)
        if m and method == "GET":
            repo = f"{m['o']}/{m['r']}"
            if repo not in self.repos:
                return 404, None
            ref = (query.get("ref") or ["main"])[0]
            rc, out = self._git(repo, "cat-file", "-e", f"{ref}:{m['p']}")
            if rc != 0:
                return 404, None
            r = subprocess.run(["git", "-C", self.repos[repo], "show",
                                f"{ref}:{m['p']}"], capture_output=True,
                               timeout=30)
            return 200, {"type": "file", "path": m["p"], "encoding": "base64",
                         "content": base64.b64encode(r.stdout).decode()}

        return self._unmodelled(method, API + path)
