"""fleetforge — the one door to the forge (plan 0022).

Every forge call the repository makes goes through this module: one named
verb per API shape today's callers actually use, so adding a second forge is
a second backend class behind the same verbs, not a sweep across six files.

**The contract every verb holds, unconditionally:**
  * Returns `(status, body)`. Nothing raises — not `URLError`, not `OSError`,
    not `http.client.HTTPException`, not `ValueError` (which is what
    `urlopen` raises for a non-ASCII header value or a header containing a
    control character — the header-injection guard's "Invalid header value"
    carries the token verbatim in its `str(e)`, which is exactly why the text
    is discarded rather than logged). Transport failure of any of those kinds
    is `(0, None)`. An HTTP error status is `(code, None)`. `0` means "could
    not tell", never "no" — see plan 0022 §3: a caller that reads it as "no"
    fails closed in a way that looks like a policy decision, so the module
    hands back the code and lets the caller decide.
  * The token lives in the `Authorization` header and nowhere else it could
    leak from: not in a URL, not in an exception message, not in `repr()` or
    `vars()` of a `Forge` instance. It is held in a `__slots__` field with no
    `__dict__` backing it (so `vars(forge)` raises `TypeError` rather than
    handing back a dict with the token in it) and a hand-written `__repr__`
    that never mentions it.
  * The module reads no environment variable and runs no token command.
    Which token a caller holds (admin vs implbot) is the caller's business;
    a module that fetched its own token would re-open the "which token did
    this process run with" question `feat/split-fleet-tokens` closed.
  * Every path segment the module interpolates into a URL is percent-encoded,
    including the `owner`/`repo` split. `urlopen` raises `UnicodeEncodeError`
    — a `ValueError` subclass — on a non-ASCII unquoted segment; encoding
    every segment is what lets a non-ASCII branch name or contents path
    succeed instead of crashing the caller.
  * `repo` is always `owner/repo`. The module never supplies an owner, and it
    validates shape with the existing `fleetlib.validate_repo` — not a second,
    subtly different regex (plan 0022 §3: a second one is how `_ID_RE` once
    allowed `/`, review round 1 of PR #9). `validate_repo` calls `sys.exit`,
    and `SystemExit` reaching a verb would be the crash-loop class plan 0022
    §3 ("Don't raise") exists to forbid — one malformed repo string must not
    end the whole run for every OTHER repo a caller is working through. So
    every verb calls it under `try/except SystemExit` and turns a rejection
    into `(0, None)`, the same outcome any other malformed-request failure
    gets — never letting the exit reach the caller (review 2119 on PR #116,
    resolving the fold-in's self-contradiction between "at the CLI edge or in
    the `Forge` constructor" and a per-call `repo` argument: the constructor
    takes no repo, so validation cannot live there. Each consumer keeps its
    own edge behaviour on top — `fleet-reviews` records a `_FAILURES` entry
    and continues, `fleet-candidate` exits 2 with its own message — this
    module only guarantees the exit never happens *inside* a verb).

No raw `request()` verb is exposed: every call site on `main` at this plan's
filing maps to one of the named verbs below, and an escape hatch invites the
next open-coded path (plan 0022 §2).
"""
import http.client
import importlib.machinery
import importlib.util
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_lib_loader = importlib.machinery.SourceFileLoader(
    "fleetlib", str(Path(__file__).resolve().parent / "fleetlib.py"))
_lib_spec = importlib.util.spec_from_loader("fleetlib", _lib_loader)
if _lib_spec is None:
    # Unreachable with a SourceFileLoader; module_from_spec(None) used to
    # raise AttributeError here, so keep that kind of failure.
    raise AttributeError("could not build an import spec for fleetlib")
fleetlib = importlib.util.module_from_spec(_lib_spec)
_lib_loader.exec_module(fleetlib)

__all__ = ["Forge", "paged", "CAP_HIT", "EXHAUSTED", "FAILED"]

CAP_HIT = "cap-hit"
EXHAUSTED = "exhausted"
FAILED = "failed"


def _quote(segment):
    """Percent-encode one path segment. `safe=""` — a segment is one path
    component, so a literal `/` inside it (e.g. from an unsplit repo string)
    must be encoded too, not treated as a separator."""
    return urllib.parse.quote(str(segment), safe="")


def _quote_path(path):
    """Percent-encode a `/`-separated path, segment by segment, preserving
    the separators. Used for contents paths, which are directory trees.

    `None` when a component is `..`, and the caller turns that into
    `(0, None)`. Encoding does NOT solve this: `urllib.parse.quote` treats `.`
    as an unreserved character and returns `..` verbatim at every `safe=`
    setting, so the component reaches the forge intact and the contents API
    resolves it server-side — reading a path the caller never named.

    Refused rather than dropped. Silently rewriting `plans/../secrets` into
    `plans/secrets` answers a question nobody asked, and the caller cannot tell
    it happened; `(0, None)` says the read did not occur, which is the one
    thing that must not be confused with "the file is not there"."""
    parts = [seg for seg in str(path).split("/") if seg not in ("", ".")]
    if any(seg == ".." for seg in parts):
        return None
    return "/".join(_quote(seg) for seg in parts)


def _int_or_none(n):
    """`int(n)` if that succeeds, else `None`. `get_pull`, `list_reviews` and
    `list_issue_comments` take a PR/issue number and used to call `int(n)`
    directly in an f-string outside any try — a non-numeric `n` (e.g.
    `get_pull("o/r", "abc")`) raised `ValueError` straight out of the verb,
    the one thing every verb promises never to do. Coercion now happens here,
    inside the same boundary as the repo-shape check, so a bad `n` gets the
    same `(0, None)` a bad `repo` gets (review 2146 LOW)."""
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def _split_repo(repo):
    """`(owner, name)` on a valid `owner/repo`, or `None`.

    Shape is checked with `fleetlib.validate_repo` — the one regex plan 0022
    §3 requires, not a second, looser one written here. `validate_repo` raises
    `SystemExit` on rejection; that must never reach a verb's caller (see the
    module docstring), so it is caught here and turned into the `None` every
    verb below reads as "this request cannot be formed" — the same `(0,
    None)` outcome any other malformed request gets."""
    try:
        validated = fleetlib.validate_repo(repo)
    except SystemExit:
        return None
    owner, name = validated.split("/")
    return owner, name


class _ForgejoBackend:
    """Shapes requests for a Forgejo (or Gitea) instance. `/api/v1` lives
    here and nowhere else in the module (plan 0052 D1) — `_request` asks the
    backend for its prefix rather than building it itself.

    Each method returns `(method, path, query, body)` for `Forge._request`,
    or `None` if this backend cannot serve the verb at all (none do, today —
    every verb below has a Forgejo answer)."""

    prefix = "/api/v1"

    def get_pull(self, owner, name, n):
        return "GET", f"/repos/{owner}/{name}/pulls/{n}", None, None

    def get_pull_by_branch(self, owner, name, base, head):
        return "GET", f"/repos/{owner}/{name}/pulls/{base}/{head}", None, None

    def list_pulls(self, owner, name, state, page, limit, sort):
        return ("GET", f"/repos/{owner}/{name}/pulls",
                {"state": state, "page": page, "limit": limit, "sort": sort}, None)

    def list_pull_issues(self, owner, name, state, page, limit):
        """The issues index, filtered to pull requests: `GET
        /repos/{owner}/{repo}/issues?type=pulls`. Plan 0218: the same pull
        requests `list_pulls` returns, but 3 s for eunomia's 17 pages against
        23-33 s a page on `/pulls` (measured 2026-11-08, 832 pull requests) —
        every page of `/pulls` computes each PR's diff stats server-side,
        which this endpoint does not. No `sort`: the issues index orders
        newest-created first and cannot be asked for `oldest`, which is why
        the caller (`_pull_pages`) reads to the end rather than relying on
        page order."""
        return ("GET", f"/repos/{owner}/{name}/issues",
                {"type": "pulls", "state": state, "page": page, "limit": limit}, None)

    def list_reviews(self, owner, name, n, page, limit):
        return ("GET", f"/repos/{owner}/{name}/pulls/{n}/reviews",
                {"page": page, "limit": limit}, None)

    def list_issue_comments(self, owner, name, n, since, page, limit):
        return ("GET", f"/repos/{owner}/{name}/issues/{n}/comments",
                {"since": since, "page": page, "limit": limit}, None)

    def get_branch(self, owner, name, branch):
        return "GET", f"/repos/{owner}/{name}/branches/{branch}", None, None

    def list_branches(self, owner, name, page, limit):
        return ("GET", f"/repos/{owner}/{name}/branches",
                {"page": page, "limit": limit}, None)

    def list_commits(self, owner, name, sha, path, page, limit):
        return ("GET", f"/repos/{owner}/{name}/commits",
                {"sha": sha, "path": path, "limit": limit, "page": page}, None)

    def get_repo(self, owner, name):
        return "GET", f"/repos/{owner}/{name}", None, None

    def search_repos(self, page, limit):
        # No `q` — an empty query is how Forgejo lists every repository the
        # token can see, not a filtered search (bin/fleet-orphans:214's
        # original use of this endpoint). Result rows are under `data`;
        # `_GithubBackend.search_repos` below documents its own key.
        return "GET", "/repos/search", {"page": page, "limit": limit}, None

    def get_branch_protections(self, owner, name):
        return "GET", f"/repos/{owner}/{name}/branch_protections", None, None

    def list_action_runs(self, owner, name, page, limit):
        return ("GET", f"/repos/{owner}/{name}/actions/runs",
                {"page": page, "limit": limit}, None)

    def get_contents(self, owner, name, path, ref):
        return "GET", f"/repos/{owner}/{name}/contents/{path}", {"ref": ref}, None

    def get_git_tree(self, owner, name, sha):
        return ("GET", f"/repos/{owner}/{name}/git/trees/{sha}",
                {"per_page": 1000}, None)

    def get_git_blob(self, owner, name, sha):
        return "GET", f"/repos/{owner}/{name}/git/blobs/{sha}", None, None

    def create_issue_comment(self, owner, name, n, body):
        return ("POST", f"/repos/{owner}/{name}/issues/{n}/comments",
                None, {"body": body})

    def edit_pull_body(self, owner, name, n, body):
        return "PATCH", f"/repos/{owner}/{name}/pulls/{n}", None, {"body": body}

    def get_commit_pull(self, owner, name, sha):
        return "GET", f"/repos/{owner}/{name}/commits/{sha}/pull", None, None

    def get_commit_status(self, owner, name, sha):
        return "GET", f"/repos/{owner}/{name}/commits/{sha}/status", None, None

    def post_status(self, owner, name, sha, state, description, context):
        return ("POST", f"/repos/{owner}/{name}/statuses/{sha}",
                None, {"state": state, "context": context, "description": description})

    def create_pull(self, owner, name, title, head, base, body):
        return ("POST", f"/repos/{owner}/{name}/pulls", None,
                {"title": title, "head": head, "base": base, "body": body})

    def get_user(self):
        return "GET", "/user", None, None

    def list_pull_files(self, owner, name, n, page, limit):
        return ("GET", f"/repos/{owner}/{name}/pulls/{n}/files",
                {"page": page, "limit": limit}, None)

    def list_statuses(self, owner, name, sha, page, limit):
        return ("GET", f"/repos/{owner}/{name}/commits/{sha}/statuses",
                {"page": page, "limit": limit}, None)

    def merge_pull(self, owner, name, n, head_sha):
        # `head_commit_id` makes the forge refuse the merge if the head moved
        # since the caller looked.
        return ("POST", f"/repos/{owner}/{name}/pulls/{n}/merge", None,
                {"Do": "merge", "head_commit_id": head_sha})


class _GithubBackend:
    """Shapes requests for GitHub's REST API. Plan 0052 D2's divergences,
    each noted at the method where it bites (and in `docs/forge.md`'s verb
    table, which is the place to look for the reasoning in one spot):

      * `list_pulls`/`list_reviews`/`list_issue_comments` — GitHub's page-size
        parameter is `per_page`, not `limit`; pagination stays entirely
        inside this backend by translating the name, so `paged()`'s
        short-page-ends-the-scan rule (it never inspects a Link header)
        keeps working unchanged.
      * `get_branch_protections` — unsupported; see the method below.
      * `post_status` — uses the Statuses API, not Check Runs; see the
        method below.
      * `search_repos` — GitHub's Search API wraps its rows under `items`,
        not `data`; see the method below.
    """

    prefix = ""

    def get_pull(self, owner, name, n):
        return "GET", f"/repos/{owner}/{name}/pulls/{n}", None, None

    def get_pull_by_branch(self, owner, name, base, head):
        """Unsupported — returns `None`, which `Forge` turns into `(0, None)`
        without making a request.

        GitHub has no single-PR-by-branch-pair endpoint; its nearest
        equivalent, `GET /pulls?head={owner}:{head}&base={base}`, answers
        with a LIST — the shape `list_pulls` already returns, and reading one
        page of it is exactly the per-branch filtering plan 0215 moves off of
        on Forgejo. No caller on this fleet runs against the `github` backend
        today (search the tree for `kind="github"`: nothing constructs one),
        so there is no live case to decide the list-vs-single-object question
        against, and plan 0052's boundary is to not invent an answer without
        one. `(0, None)` is the same "could not tell" every other unreadable
        verb already produces."""
        return None

    def list_pulls(self, owner, name, state, page, limit, sort):
        query = {"state": state, "page": page, "per_page": limit}
        # Forgejo's `sort="oldest"` is the only value any caller sends
        # (fleet-watch:734-740); GitHub has no `oldest` keyword — the same
        # ordering is `sort=created&direction=asc`. Anything else passes
        # through unmodified, since no caller sends another value.
        if sort == "oldest":
            query["sort"] = "created"
            query["direction"] = "asc"
        elif sort is not None:
            query["sort"] = sort
        return "GET", f"/repos/{owner}/{name}/pulls", query, None

    def list_pull_issues(self, owner, name, state, page, limit):
        """GitHub's `/repos/{owner}/{repo}/issues` lists both issues and pull
        requests for the repo — an item that IS a pull request carries a
        `pull_request` object, which is exactly the test `_pull_pages`
        already applies to drop a non-pull-request item, so no further
        filtering belongs here. GitHub has no query parameter that narrows
        the listing to pull requests only (unlike Forgejo's `type=pulls`);
        the caller filters by the `pull_request` key instead."""
        return ("GET", f"/repos/{owner}/{name}/issues",
                {"state": state, "page": page, "per_page": limit}, None)

    def list_reviews(self, owner, name, n, page, limit):
        return ("GET", f"/repos/{owner}/{name}/pulls/{n}/reviews",
                {"page": page, "per_page": limit}, None)

    def list_issue_comments(self, owner, name, n, since, page, limit):
        return ("GET", f"/repos/{owner}/{name}/issues/{n}/comments",
                {"since": since, "page": page, "per_page": limit}, None)

    def get_branch(self, owner, name, branch):
        return "GET", f"/repos/{owner}/{name}/branches/{branch}", None, None

    def list_branches(self, owner, name, page, limit):
        return ("GET", f"/repos/{owner}/{name}/branches",
                {"page": page, "per_page": limit}, None)

    def list_commits(self, owner, name, sha, path, page, limit):
        return ("GET", f"/repos/{owner}/{name}/commits",
                {"sha": sha, "path": path, "per_page": limit, "page": page}, None)

    def get_repo(self, owner, name):
        return "GET", f"/repos/{owner}/{name}", None, None

    def search_repos(self, page, limit):
        """GitHub has no "every repo this token can see" endpoint with an
        empty filter the way Forgejo's `/repos/search` does — its Search API
        (`GET /search/repositories`) requires a non-empty `q`. This backend
        sends `q=""` rather than inventing a query fleet-orphans never asked
        for; a real GitHub caller wanting this verb would need to supply one,
        the same documented gap `get_branch_protections` has. Response rows
        are wrapped under `items` here, where Forgejo wraps them under
        `data` — the module does not normalise the two into one shape (see
        the boundary in plans/0057 and docs/forge.md)."""
        return "GET", "/search/repositories", {"q": "", "page": page,
                                                "per_page": limit}, None

    def get_branch_protections(self, owner, name):
        """Unsupported — returns `None`, which `Forge` turns into `(0, None)`
        without making a request.

        GitHub has no repo-wide "every branch's rule" endpoint: protection is
        read per branch (`GET /branches/{branch}/protection`), and its body
        shares no fields with Forgejo's rule list — no `required_approvals`,
        `enable_push_whitelist`, `push_whitelist_usernames`, or
        `unprotected_file_patterns`. `bin/fleet-watch`'s `main_is_protected`
        reads exactly those Forgejo fields to decide whether main can be
        written by anything but a reviewed merge; translating GitHub's shape
        into them would mean inventing values GitHub never sent, which is the
        "approximating" plan 0052's boundary forbids. `(0, None)` is read as
        "could not tell" by every existing caller — which for this one
        specific read is the correct, already-fail-closed answer
        (`main_is_protected`'s own docstring: unreadable protections is a
        refusal, not a pass)."""
        return None

    def list_action_runs(self, owner, name, page, limit):
        """Unsupported — returns `None`, which `Forge` turns into `(0, None)`
        without making a request.

        GitHub's workflow-run object has no `stopped` field, and its
        `updated_at` is not a completion time (it moves on re-runs and on
        label changes). The one caller measures created-to-stopped, so
        translating GitHub's shape would invent a value GitHub never sent,
        which plan 0052's boundary forbids. `(0, None)` is read as "could not
        tell", and that caller falls back to its fixed bound."""
        return None

    def get_contents(self, owner, name, path, ref):
        return "GET", f"/repos/{owner}/{name}/contents/{path}", {"ref": ref}, None

    def get_git_tree(self, owner, name, sha):
        return "GET", f"/repos/{owner}/{name}/git/trees/{sha}", None, None

    def get_git_blob(self, owner, name, sha):
        return "GET", f"/repos/{owner}/{name}/git/blobs/{sha}", None, None

    def create_issue_comment(self, owner, name, n, body):
        return ("POST", f"/repos/{owner}/{name}/issues/{n}/comments",
                None, {"body": body})

    def edit_pull_body(self, owner, name, n, body):
        return "PATCH", f"/repos/{owner}/{name}/pulls/{n}", None, {"body": body}

    def get_commit_pull(self, owner, name, sha):
        # GitHub's sibling returns a LIST of pulls, not one object.
        return "GET", f"/repos/{owner}/{name}/commits/{sha}/pulls", None, None

    def get_commit_status(self, owner, name, sha):
        return "GET", f"/repos/{owner}/{name}/commits/{sha}/status", None, None

    def post_status(self, owner, name, sha, state, description, context):
        """GitHub also has a Check Runs API, aimed at GitHub Apps — it wants
        an installation token (`checks:write`), not the plain PAT this module
        holds, and returns a richer object (`conclusion`, `output`, ...) this
        module has no verb shaped to use. The Statuses API takes the same
        three fields Forgejo does and works with the token already in hand,
        so that is the one this backend calls (plan 0052 D2)."""
        return ("POST", f"/repos/{owner}/{name}/statuses/{sha}",
                None, {"state": state, "context": context, "description": description})

    def create_pull(self, owner, name, title, head, base, body):
        """No `draft` field. GitHub's REST API has one; this backend does not
        send it, so `create_pull`'s behaviour stays byte-identical across
        both backends the way every other write verb's does — a caller
        wanting a draft marks it the way plan 0070's lane wrapper does, with
        a `WIP: ` title prefix, which works on both forges."""
        return ("POST", f"/repos/{owner}/{name}/pulls", None,
                {"title": title, "head": head, "base": base, "body": body})

    def get_user(self):
        return "GET", "/user", None, None

    def list_pull_files(self, owner, name, n, page, limit):
        return ("GET", f"/repos/{owner}/{name}/pulls/{n}/files",
                {"page": page, "per_page": limit}, None)

    def list_statuses(self, owner, name, sha, page, limit):
        return ("GET", f"/repos/{owner}/{name}/commits/{sha}/statuses",
                {"page": page, "per_page": limit}, None)

    def merge_pull(self, owner, name, n, head_sha):
        return ("PUT", f"/repos/{owner}/{name}/pulls/{n}/merge", None,
                {"merge_method": "merge", "sha": head_sha})


_BACKENDS = {"forgejo": _ForgejoBackend, "github": _GithubBackend}


class Forge:
    """Two backends, `forgejo` and `github`, behind the same eleven verbs.
    The constructor is the only place `kind` is looked at — every verb below
    just asks `self._backend` to shape its request, so a third backend is a
    third class and nothing else in this module (or, per plan 0052's
    boundary, anywhere outside it) branches on `kind`."""

    __slots__ = ("_base_url", "_token", "_kind", "_timeout", "_backend")

    def __init__(self, base_url, token, kind="forgejo", timeout=20):
        try:
            backend_cls = _BACKENDS[kind]
        except KeyError:
            raise ValueError(
                f"Forge: unsupported kind {kind!r} (must be one of "
                f"{sorted(_BACKENDS)})")
        self._base_url = str(base_url).rstrip("/")
        self._token = token
        self._kind = kind
        self._backend = backend_cls()
        # 20s matches fleet-candidate's pre-migration urlopen timeout, kept as
        # the module default; fleet-reviews' backfill ran at 30s before this
        # module existed and passes timeout=30 explicitly to keep it (review
        # 2146 LOW — a single hardcoded value would have silently shortened
        # one consumer's tolerance for a slow Forgejo).
        self._timeout = timeout

    def __repr__(self):
        # No token. No __dict__ either (see __slots__), so vars(self) raises
        # TypeError rather than a dict that would carry it.
        return f"Forge(base_url={self._base_url!r}, kind={self._kind!r})"

    def _dispatch(self, spec):
        """`spec` is what a backend method returned: `None` (this backend
        cannot serve the verb — the same `(0, None)` a malformed repo or a
        transport failure gets, never a raise or a fabricated body) or a
        `(method, path, query, body)` tuple to send."""
        if spec is None:
            return 0, None
        method, path, query, body = spec
        return self._request(method, path, query=query, body=body)

    # -------------------------------------------------------------- transport
    def _request(self, method, path, query=None, body=None):
        """(status, parsed-json-or-None). Never raises. See module docstring
        for the full contract."""
        url = f"{self._base_url}{self._backend.prefix}{path}"
        if query:
            q = urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None})
            if q:
                url = f"{url}{'&' if '?' in url else '?'}{q}"
        try:
            # json.dumps can raise (TypeError on a non-serializable body,
            # ValueError on e.g. a circular reference) — that must land in
            # the same never-raises boundary as everything else below, not
            # crash the caller before a request is even attempted (review
            # 2146 LOW).
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(url, data=data, method=method)
            # A token containing "\n" (or anything else http.client's header
            # guard rejects) does NOT raise here — add_header only stores the
            # value. The guard itself lives in http.client.putheader(),
            # called further down inside urlopen(), and raises there —
            # "Invalid header value" — with the token embedded in str(e).
            # That text must never be kept, which is exactly why the except
            # below is broad enough to reach urlopen() and not narrowed to
            # this line (review 2146 LOW: the previous comment pointed at the
            # wrong line, which invites narrowing the try to "fix" it).
            req.add_header("Authorization", f"token {self._token}")
            if data:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                try:
                    return resp.status, (json.loads(raw) if raw else None)
                except json.JSONDecodeError:
                    return resp.status, None
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, OSError, http.client.HTTPException,
                ValueError, TypeError):
            # ValueError catches UnicodeEncodeError (a non-ASCII segment that
            # slipped past _quote — defence in depth) and the header-injection
            # guard's ValueError. TypeError catches a non-JSON-serializable
            # body. Every one of these exceptions' own text is discarded
            # here, not formatted, not logged, not re-raised — that text is
            # the one place the token can appear outside the header itself.
            return 0, None

    # ------------------------------------------------------------- read verbs
    def get_pull(self, repo, n):
        ow = _split_repo(repo)
        n = _int_or_none(n)
        if ow is None or n is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.get_pull(_quote(owner), _quote(name), n))

    def get_pull_by_branch(self, repo, base, head):
        """The one pull request whose base branch is `base` AND head branch
        is `head` — both, not head alone — read directly rather than
        filtered out of a listing (plan 0215) — `(200, pr)`, `(404, None)`
        for no such PR, or `(status, None)` for anything else, which is
        unreadable, never "no PR" (the same rule `list_pulls`/`paged()`
        already hold for a listing).

        Measured live against operator/eunomia on 2026-11-07 that `base` is
        matched exactly, not merely informative: querying a real PR's head
        with the WRONG base string (`.../pulls/not-the-real-base/<head>`)
        404s even though a PR with that exact head exists. A caller that
        used to find a pull request by head alone and then check its base
        itself (today's `verify_marked_pr`/`verify_followup_pr`) therefore
        cannot see a PR on the right head but the wrong base through this
        verb at all — it reads as 404, "no PR", not as "found, wrong base".
        Those callers keep their own `base != "main"` check regardless (the
        plan's own instruction, and cheap insurance against this verb's
        matching ever changing), but it is unreachable through this read
        today; PR #1437's body says so.

        `base` and `head` are branch names on THIS repo, not `owner:branch`
        — see `create_pull`'s docstring for why the cross-repo fork syntax is
        out of scope for every verb this module has. Each is one path
        segment to `_quote`, which percent-encodes a literal `/` inside it
        (`agent/tests/<plan>/<runner>` is exactly such a branch) rather than
        letting it split into extra segments; measured live against
        operator/eunomia on 2026-11-07 that Forgejo's router still resolves
        the encoded form correctly (`%2F` decoded back to `/` before the
        base/head match), so a slash-bearing branch name reaches the right
        row.

        Measured the same day: 200 in ~0.5s for a merged PR whose branch has
        since been deleted (#1354, `adr-0026-owner-seat`), and 404 in ~0.03s
        for a branch with no PR at all — one call where `read_pulls` was 17
        pages. What this endpoint returns when a CLOSED and an OPEN pull
        request share one head could NOT be measured directly, and still
        cannot: no branch in eunomia's history has ever carried two. A full
        scan of all 843 pull requests on operator/eunomia on 2026-11-09 (one
        read-only pass, made to settle this, not a path any run takes) found
        843 distinct head branches — zero collisions. That is consistent
        with `_redispatch_branch` in `bin/fleet-watch` (plan 0086 D2b), which
        always picks a branch name with no ref on origin and no live lease,
        never one an earlier run's PR already used.

        ADR-0029 §2 ("redispatch reuses branch names, and an earlier run's
        merged or closed pull request looks like the current run's") still
        names the shape as reachable, so `bin/orchestrator`'s callers
        (`verify_marked_pr`, `verify_followup_pr`, `post_question`,
        `_open_implementation_pr`, and D3's lane lookup) do not rely on this
        verb alone: each result passes through `_prefer_open_on_branch`,
        which — only when this verb's own answer is not an open pull
        request — makes one bounded second read (`state=open`, capped at
        250 pull requests, nowhere near the repository's full history) and
        prefers an open match on the same head over the closed or merged one
        this verb returned. That second read is itself a listing, so it is
        not unconditional: it runs only on the (so far unobserved) shared-head
        path, never on the ordinary one-PR-per-head answer this verb gives
        today, which is what keeps the plan's "no `list_pulls` call" property
        true for the common case.

        Unsupported on the `github` backend — see
        `_GithubBackend.get_pull_by_branch`."""
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.get_pull_by_branch(
            _quote(owner), _quote(name), _quote(base), _quote(head)))

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_pulls(
            _quote(owner), _quote(name), state, page, limit, sort))

    def list_pull_issues(self, repo, state="all", page=1, limit=50):
        """The issues index, raw, scoped to pull requests (plan 0218 D1).
        Each returned item is the issues endpoint's own shape — `merged`/
        `merged_at` live under its `pull_request` key, not at the top level
        the way `list_pulls`' items carry them, and there is no `head`. The
        caller (`_pull_pages` in `bin/fleet-watch`) is the one place that
        reshapes an item and skips one with no `pull_request` key; this verb
        does neither, the same division `list_pulls` keeps with its own
        callers."""
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_pull_issues(
            _quote(owner), _quote(name), state, page, limit))

    def list_reviews(self, repo, n, page=1, limit=50):
        ow = _split_repo(repo)
        n = _int_or_none(n)
        if ow is None or n is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_reviews(
            _quote(owner), _quote(name), n, page, limit))

    def list_issue_comments(self, repo, n, since=None, page=1, limit=50):
        ow = _split_repo(repo)
        n = _int_or_none(n)
        if ow is None or n is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_issue_comments(
            _quote(owner), _quote(name), n, since, page, limit))

    def get_branch(self, repo, name):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, rname = ow
        return self._dispatch(self._backend.get_branch(
            _quote(owner), _quote(rname), _quote(name)))

    def list_branches(self, repo, page=1, limit=50):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_branches(
            _quote(owner), _quote(name), page, limit))

    def list_commits(self, repo, sha=None, path=None, page=1, limit=50):
        """Commits reachable from `sha` (default: the repository's default
        branch, chosen server-side), optionally filtered to those touching
        `path`. `sha` and `path` are query values, not URL path segments —
        `_request`'s own `urlencode` percent-encodes them; no `_quote` here
        (a `path` containing `/`, e.g. `plans/0001-foo.md`, must reach the
        query intact, the same as `get_contents`'s `ref`)."""
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_commits(
            _quote(owner), _quote(name), sha, path, page, limit))

    def get_repo(self, repo):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.get_repo(_quote(owner), _quote(name)))

    def search_repos(self, page=1, limit=50):
        """No `repo` argument — this lists repositories, it does not read
        one. Forgejo returns `{"data": [...], ...}`; the `github` backend's
        shape (`{"items": [...], ...}`) is documented on
        `_GithubBackend.search_repos`. The module does not normalise the two
        into a shared key: a caller reading one platform's body already has
        to know which platform it is talking to, so hiding the difference
        here would not remove that requirement, only relocate the bug to
        whichever caller assumes the wrong key (plan 0057 boundary)."""
        return self._dispatch(self._backend.search_repos(page, limit))

    def get_branch_protections(self, repo):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(
            self._backend.get_branch_protections(_quote(owner), _quote(name)))

    def list_action_runs(self, repo, page=1, limit=50):
        """Workflow runs, newest first. Forgejo's body is returned unchanged,
        `{"total_count": n, "workflow_runs": [...]}`; being a dict, `paged()`
        cannot iterate it and the caller pages by hand. The `github` backend
        is unsupported and answers `(0, None)` — see
        `_GithubBackend.list_action_runs`."""
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_action_runs(
            _quote(owner), _quote(name), page, limit))

    def get_contents(self, repo, path, ref=None):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        enc = _quote_path(path)
        if enc is None:                      # `..` component — see _quote_path
            return 0, None
        return self._dispatch(self._backend.get_contents(
            _quote(owner), _quote(name), enc, ref))

    def list_contents(self, repo, dir, ref=None):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        enc = _quote_path(dir)
        if enc is None:                      # `..` component — see _quote_path
            return 0, None
        # Same backend call as get_contents — both Forgejo and GitHub serve a
        # file and a directory listing from the identical endpoint, telling
        # the two apart by the shape of the body rather than the path.
        return self._dispatch(self._backend.get_contents(
            _quote(owner), _quote(name), enc, ref))

    def get_git_tree(self, repo, sha):
        """One non-recursive git tree object: `(200, {"tree": [{"path",
        "sha", "type"}, ...], "truncated": bool})`, or `(status, None)`.

        `sha` may be a tree sha OR a commit sha — both backends resolve a
        commit sha to its own tree the same way `list_tree` below already
        relies on for a branch's root commit. Plan 0216's second read verb,
        alongside `get_git_blob`: together they let a caller walk a
        directory (root tree → a named subtree's sha → that subtree) and
        fetch a file's exact content by the blob sha a tree entry names,
        without `get_contents`'/`list_contents`'s per-entry last-commit
        computation (33-80 s for eunomia's 194-entry `plans/`, see plan
        0216's "why")."""
        ow = _split_repo(repo)
        if ow is None or not sha:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.get_git_tree(
            _quote(owner), _quote(name), _quote(str(sha))))

    def get_git_blob(self, repo, sha):
        """One git blob object by its OWN sha: `(200, {"content": base64,
        "encoding": "base64", "sha": ..., "size": ...})`, or `(status,
        None)`. Named by content, not by path or ref — the same sha a git
        tree entry carries, so a cache keyed by it (plan 0216's
        `plan-lint-blobs/`) is never stale: a path's content can change
        between two heads, but a blob sha always names the one content it
        was computed from."""
        ow = _split_repo(repo)
        if ow is None or not sha:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.get_git_blob(
            _quote(owner), _quote(name), _quote(str(sha))))

    def list_tree(self, repo, dir, ref="main"):
        """(200, [{name, sha, type}]) for the entries of `dir` at branch `ref`,
        read through the git trees API, or (status, None).

        The contents API (`list_contents`) computes each entry's last commit
        and takes 33-80 s for eunomia's plans/ (2026-10-28, ~150 files), past
        this client's timeout; the trees API answers in about a second. `sha`
        is the blob sha, so a caller can cache what it read from a file by it.
        A truncated tree is (0, None), never a partial listing."""
        st, b = self.get_branch(repo, ref)
        if st != 200 or not isinstance(b, dict):
            return st, None
        commit = b.get("commit") or {}
        sha = commit.get("id") or commit.get("sha")
        if not sha:
            return 0, None
        parts = [c for c in (dir or "").split("/") if c]
        if any(c in (".", "..") for c in parts):
            return 0, None
        for comp in [None] + parts:
            if comp is not None:
                hit = [e for e in entries if e.get("path") == comp
                       and e.get("type") == "tree"]
                if not hit:
                    return 404, None
                sha = hit[0].get("sha")
            st, t = self.get_git_tree(repo, sha)
            if st != 200 or not isinstance(t, dict) or t.get("truncated"):
                return (st if st != 200 else 0), None
            entries = t.get("tree") or []
        return 200, [{"name": e.get("path"), "sha": e.get("sha"),
                      "type": "file" if e.get("type") == "blob" else "dir"}
                     for e in entries]

    # ------------------------------------------------------------ write verbs
    def create_issue_comment(self, repo, n, body):
        """Post a comment on a PR's issue thread.

        The ONLY write verb this module has besides post_status, and it is here
        because plan 0011's loop must leave its iteration ledger and its failure
        report where a successor process can read them without a local file it
        may not have. It cannot approve anything: Forgejo's review verdicts live
        under /pulls/{n}/reviews, which this module deliberately exposes read-only
        (plan 0011 §3 — the wrapper has no approval code path, and a test asserts
        no approval call exists anywhere in the suite)."""
        ow = _split_repo(repo)
        n = _int_or_none(n)
        if ow is None or n is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.create_issue_comment(
            _quote(owner), _quote(name), n, body))

    def edit_pull_body(self, repo, n, body):
        """Replace a pull request's BODY. The third write verb, and the last one
        this module should acquire without a reason as specific as these.

        Here because plan 0004's `Plan: <id>` marker is what makes dedupe work,
        and asking a model for it in the prompt does not reliably produce it.
        Measured twice: pr-proxy #42 and #79 carry the plan as a PATH rather than
        an id, so dedupe never matched them and merged work stayed
        re-dispatchable; speakhush #162 carries no marker at all. The orchestrator
        knows the id, so it can write it — see verify_marked_pr, which stamps only
        a PR it has already proven is its own.

        Body only. Not title, not base, not state, not labels: a verb that can
        retarget or close a pull request is a different capability from one that
        can annotate it, and this module has no business holding the first."""
        ow = _split_repo(repo)
        n = _int_or_none(n)
        if ow is None or n is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.edit_pull_body(
            _quote(owner), _quote(name), n, body))

    def create_pull(self, repo, title, head, base, body):
        """Open a pull request. The fourth write verb (plan 0070), and here
        because a test lane's result has to land somewhere a human can read
        it: `bin/fleet-lane` is the one caller, and it never approves or
        merges what this opens (the fleet's own rule, ADR-0006 §5 — the
        reader is a human until a second one exists).

        `head` is a branch name on THIS repo, never `owner:branch` — the
        lane wrapper always opens from a branch it just pushed to the same
        repository, so the cross-repo fork syntax both platforms otherwise
        accept is not needed here and is not exercised by this module."""
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.create_pull(
            _quote(owner), _quote(name), title, head, base, body))

    def get_commit_status(self, repo, sha):
        """The COMBINED status for a commit: `{state, statuses: [...]}`.

        `/commits/{sha}/status` is the combined endpoint (singular). Its plural
        sibling `/statuses` returns every individual status instead, and reading
        the wrong one turns "is CI green" into a list nobody rolled up — the
        distinction that has bitten this fleet before, which is why the path is
        written out here rather than composed."""
        ow = _split_repo(repo)
        if ow is None or not sha:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.get_commit_status(
            _quote(owner), _quote(name), _quote(str(sha))))

    def get_commit_pull(self, repo, sha):
        """The pull request a commit on the default branch landed through.

        Forgejo answers with ONE pull object; GitHub's sibling endpoint answers
        with a list. Callers that need `merged_by` take either shape."""
        ow = _split_repo(repo)
        if ow is None or not sha:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.get_commit_pull(
            _quote(owner), _quote(name), _quote(str(sha))))

    def post_status(self, repo, sha, state, description, context):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.post_status(
            _quote(owner), _quote(name), _quote(sha), state, description, context))

    def get_user(self):
        """The account the token authenticates as: `(200, {"login": ...})`.
        A caller that posts under a particular identity asks this first
        rather than trusting which token helper it ran."""
        return self._dispatch(self._backend.get_user())

    def list_statuses(self, repo, sha, page=1, limit=50):
        """Every individual status on a commit, newest first, one page. The
        plural `/statuses` (each with its `creator`), not the combined
        `/status` that `get_commit_status` reads: a caller that has to know
        WHO posted a context needs the list."""
        ow = _split_repo(repo)
        if ow is None or not sha:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_statuses(
            _quote(owner), _quote(name), _quote(str(sha)), page, limit))

    def list_pull_files(self, repo, n, page_cap=20, limit=50):
        """Every file a pull request changes, as `(200, [file, ...])`.

        Follows pagination to a CONFIRMED empty page, and returns `(0, None)`
        rather than a truncated list when `page_cap` pages are not enough or
        any page fails: a caller deciding what a PR does not touch must never
        read a cut-off listing as the whole (review 3844). This verb pages
        for itself because the answer is only meaningful complete."""
        ow = _split_repo(repo)
        n = _int_or_none(n)
        if ow is None or n is None:
            return 0, None
        owner, name = ow
        out = []
        for page in range(1, page_cap + 1):
            st, body = self._dispatch(self._backend.list_pull_files(
                _quote(owner), _quote(name), n, page, limit))
            if st != 200 or not isinstance(body, list):
                return (st if st else 0), None
            if not body:
                return 200, out
            out.extend(body)
        return 0, None

    def merge_pull(self, repo, n, head_sha):
        """Merge a pull request, only if its head is still `head_sha`."""
        ow = _split_repo(repo)
        n = _int_or_none(n)
        if ow is None or n is None or not head_sha:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.merge_pull(
            _quote(owner), _quote(name), n, _quote(str(head_sha))))


class Paged:
    """The iterator `paged()` returns. Iterate it for pages (lists of items);
    after it stops, `.outcome` is exactly one of `CAP_HIT`, `EXHAUSTED` or
    `FAILED` — set before the generator returns, so a caller than exhausts
    the iterator can always read it afterward. When `.outcome` is `FAILED`,
    `.failed_status` carries the verb's own `status` for the page that
    failed (an HTTP code like `401`, or `0` for a transport failure that
    never got a status) — `None` for the other two outcomes.

    Collapsing every `FAILED` page into one string discarded the one thing
    that told an expired token (`401`) apart from Forgejo being unreachable
    (`0`) — the pre-module `fleet-reviews` recorded `-> HTTP 401` /
    `-> URLError` for exactly this reason, and a caller reading only
    `.outcome` lost it (review 2146 M1). `.failed_status` is how a caller
    gets it back without inspecting page contents.

    `docs/plan-dispatch.md` is explicit that hitting the cap must read as
    *unreadable*, never as "no more results": a truncated scan authorising a
    duplicate dispatch is worse than one that runs long. `EXHAUSTED` and
    `CAP_HIT` are therefore distinct outcomes, not inferred from page size at
    the call site."""

    def __init__(self, verb, args, kwargs, cap, page_size):
        self._verb = verb
        self._args = args
        self._kwargs = kwargs
        self._cap = cap
        self._page_size = page_size
        self.outcome = None
        self.failed_status = None

    def __iter__(self):
        page = 1
        while True:
            status, body = self._verb(*self._args, page=page,
                                      limit=self._page_size, **self._kwargs)
            if status != 200 or not isinstance(body, list):
                self.outcome = FAILED
                self.failed_status = status
                return
            if not body:
                self.outcome = EXHAUSTED
                return
            yield body
            if len(body) < self._page_size:
                self.outcome = EXHAUSTED
                return
            if self._cap is not None and page >= self._cap:
                self.outcome = CAP_HIT
                return
            page += 1


def paged(verb, *args, cap=None, page_size=50, **kwargs):
    """A generator over pages of `verb(*args, page=N, limit=page_size,
    **kwargs)`. `cap`, if given, is the maximum number of pages fetched;
    `None` means unbounded — pick that only when a truncated read is worse
    than a long one (plan 0022 §2, `fleet-reviews`' backfill).

    Returns a `Paged` (an iterable, not a plain generator function) so its
    `.outcome` survives being read after the loop that consumed it."""
    return Paged(verb, args, kwargs, cap, page_size)
