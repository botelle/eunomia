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

    def list_pulls(self, owner, name, state, page, limit, sort):
        return ("GET", f"/repos/{owner}/{name}/pulls",
                {"state": state, "page": page, "limit": limit, "sort": sort}, None)

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

    def get_contents(self, owner, name, path, ref):
        return "GET", f"/repos/{owner}/{name}/contents/{path}", {"ref": ref}, None

    def create_issue_comment(self, owner, name, n, body):
        return ("POST", f"/repos/{owner}/{name}/issues/{n}/comments",
                None, {"body": body})

    def edit_pull_body(self, owner, name, n, body):
        return "PATCH", f"/repos/{owner}/{name}/pulls/{n}", None, {"body": body}

    def get_commit_status(self, owner, name, sha):
        return "GET", f"/repos/{owner}/{name}/commits/{sha}/status", None, None

    def post_status(self, owner, name, sha, state, description, context):
        return ("POST", f"/repos/{owner}/{name}/statuses/{sha}",
                None, {"state": state, "context": context, "description": description})

    def create_pull(self, owner, name, title, head, base, body):
        return ("POST", f"/repos/{owner}/{name}/pulls", None,
                {"title": title, "head": head, "base": base, "body": body})


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

    def get_contents(self, owner, name, path, ref):
        return "GET", f"/repos/{owner}/{name}/contents/{path}", {"ref": ref}, None

    def create_issue_comment(self, owner, name, n, body):
        return ("POST", f"/repos/{owner}/{name}/issues/{n}/comments",
                None, {"body": body})

    def edit_pull_body(self, owner, name, n, body):
        return "PATCH", f"/repos/{owner}/{name}/pulls/{n}", None, {"body": body}

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

    def list_pulls(self, repo, state="all", page=1, limit=50, sort=None):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.list_pulls(
            _quote(owner), _quote(name), state, page, limit, sort))

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

    def post_status(self, repo, sha, state, description, context):
        ow = _split_repo(repo)
        if ow is None:
            return 0, None
        owner, name = ow
        return self._dispatch(self._backend.post_status(
            _quote(owner), _quote(name), _quote(sha), state, description, context))


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
