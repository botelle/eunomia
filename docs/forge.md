# fleetforge — the one door to the forge

*Plan `plans/0022-forge-module.md`.* Every forge call the repository makes
goes through `bin/fleetforge.py`: one named verb per API shape, so adding a
second forge is a second backend class and not a sweep across six files.

## The verb inventory

`Forge(base_url, token, kind="forgejo", timeout=20)` — two backends,
`forgejo` and `github` (plan 0052). `base_url` is the bare origin (e.g.
`http://forge.example:3000`, or `https://api.github.com`); which prefix, if
any, gets appended is the backend's own business — Forgejo's is `/api/v1`,
GitHub's is nothing, GitHub's REST API lives at the bare host with no version
segment in the path. `timeout` is seconds on the underlying `urlopen()` call;
the default (20) matches `fleet-candidate`'s pre-migration value, and
`fleet-reviews` passes `timeout=30` explicitly to keep its own pre-migration
value rather than silently shortening it (review 2146 LOW).

An unrecognised `kind` raises `ValueError` at construction, naming the bad
value and the kinds that exist (`forgejo`, `github`) — not defaulted to
Forgejo, because a typo that silently picked a backend would 404 on every
call and look like an outage rather than a config mistake.

Every verb keeps the exact same signature and `(status, body)` contract on
both backends — `bin/fleet-watch` and every other caller never learn which
forge they are talking to, and neither backend ever changes what a caller
must pass or how a caller must read what comes back:

| Verb | Forgejo | GitHub |
| --- | --- | --- |
| `get_pull(repo, n)` | `GET /repos/{owner}/{repo}/pulls/{n}` | `GET /repos/{owner}/{repo}/pulls/{n}` |
| `list_pulls(repo, state="all", page=1, limit=50, sort=None)` | `GET /repos/{owner}/{repo}/pulls?state=&page=&limit=[&sort=]` | `GET /repos/{owner}/{repo}/pulls?state=&page=&per_page=[&sort=&direction=]` |
| `list_reviews(repo, n, page=1, limit=50)` | `GET /repos/{owner}/{repo}/pulls/{n}/reviews?page=&limit=` | `GET /repos/{owner}/{repo}/pulls/{n}/reviews?page=&per_page=` |
| `list_issue_comments(repo, n, since=None, page=1, limit=50)` | `GET /repos/{owner}/{repo}/issues/{n}/comments?page=&limit=[&since=]` | `GET /repos/{owner}/{repo}/issues/{n}/comments?page=&per_page=[&since=]` |
| `get_branch(repo, name)` | `GET /repos/{owner}/{repo}/branches/{name}` | `GET /repos/{owner}/{repo}/branches/{name}` |
| `get_branch_protections(repo)` | `GET /repos/{owner}/{repo}/branch_protections` | **unsupported** — always `(0, None)`, no request made |
| `get_contents(repo, path, ref=None)` | `GET /repos/{owner}/{repo}/contents/{path}[?ref=]` | `GET /repos/{owner}/{repo}/contents/{path}[?ref=]` |
| `list_contents(repo, dir, ref=None)` | `GET /repos/{owner}/{repo}/contents/{dir}[?ref=]` | `GET /repos/{owner}/{repo}/contents/{dir}[?ref=]` |
| `post_status(repo, sha, state, description, context)` | `POST /repos/{owner}/{repo}/statuses/{sha}` | `POST /repos/{owner}/{repo}/statuses/{sha}` (Statuses API, not Check Runs) |
| `create_issue_comment(repo, n, body)` | `POST /repos/{owner}/{repo}/issues/{n}/comments` | `POST /repos/{owner}/{repo}/issues/{n}/comments` |
| `edit_pull_body(repo, n, body)` | `PATCH /repos/{owner}/{repo}/pulls/{n}` | `PATCH /repos/{owner}/{repo}/pulls/{n}` |
| `create_pull(repo, title, head, base, body)` | `POST /repos/{owner}/{repo}/pulls` | `POST /repos/{owner}/{repo}/pulls` |
| `get_commit_status(repo, sha)` | `GET /repos/{owner}/{repo}/commits/{sha}/status` | `GET /repos/{owner}/{repo}/commits/{sha}/status` |
| `get_repo(repo)` | `GET /repos/{owner}/{repo}` | `GET /repos/{owner}/{repo}` |
| `list_branches(repo, page=1, limit=50)` | `GET /repos/{owner}/{repo}/branches?page=&limit=` | `GET /repos/{owner}/{repo}/branches?page=&per_page=` |
| `list_commits(repo, sha=None, path=None, page=1, limit=50)` | `GET /repos/{owner}/{repo}/commits?sha=&path=&limit=&page=` | `GET /repos/{owner}/{repo}/commits?sha=&path=&per_page=&page=` |
| `search_repos(page=1, limit=50)` | `GET /repos/search?page=&limit=` — rows under `data` | `GET /search/repositories?q=&page=&per_page=` — rows under `items` |

`get_repo`, `list_branches`, `list_commits` and `search_repos` (plan 0057) were
added to serve `bin/fleet-orphans`, the last tool in `bin/` that still opened
its own forge connection — see "Migrated consumers" below.

No raw `request()` is exposed. Every call site on `main` at this plan's
landing maps to one of the verbs above; an escape hatch invites the next
open-coded path.

### The divergences (plan 0052 D2, extended by plan 0057)

Nine of the fifteen rows above are byte-identical past the host and prefix —
GitHub's REST API was modelled closely enough on the same resource shapes
that `get_pull`, `get_branch`, `get_contents`, `list_contents`,
`create_issue_comment`, `edit_pull_body`, `get_commit_status` and `get_repo`
need no translation at all beyond the prefix `_ForgejoBackend`/`_GithubBackend`
already carry. The divergences that do exist:

- **`list_pulls`/`list_reviews`/`list_issue_comments`/`list_branches`/
  `list_commits` — the page-size parameter.** Forgejo calls it `limit`;
  GitHub calls it `per_page`. The translation lives entirely inside
  `_GithubBackend`, so `paged()`'s short-page-ends-the-scan rule (a page
  shorter than the requested size means exhausted — see below) keeps working
  unmodified: it never needed to know the parameter's name, only that asking
  for N items and getting fewer than N back means there is nothing left.
  GitHub's own convention for *discovering* more pages is a `Link` response
  header, not a page count in the body — but since `paged()` never reads a
  total or a next-page URL, only the length of the page it got, explicit
  `page=`/`per_page=` query parameters are sufficient and no `Link` header
  parsing was needed. **Pagination stayed entirely inside the backend.**
  (`bin/fleet-orphans` does not use `paged()` — see the pagination note below
  `Paged`/`paged()` further down — but its own local loop calls
  `list_branches`/`list_commits` the same way, so the same translation
  applies.)
- **`list_pulls`'s `sort="oldest"`.** The one value `bin/fleet-watch` sends
  (fleet-watch:734-740, for dedupe). GitHub has no `oldest` keyword; the same
  ordering is `sort=created&direction=asc`, and `_GithubBackend` makes that
  substitution. Any other `sort` value passes through unmodified, since no
  caller sends one.
- **`get_branch_protections` is unsupported on GitHub — the one verb this
  plan could not make work the same way.** Forgejo answers with every
  branch's protection rule in one list; GitHub has no repo-wide equivalent,
  only a per-branch read (`GET /branches/{branch}/protection`), and that
  read's body shares no field names with Forgejo's — no `required_approvals`,
  `enable_push_whitelist`, `push_whitelist_usernames`,
  `unprotected_file_patterns`, `apply_to_admins`. `bin/fleet-watch`'s
  `main_is_protected` reads exactly those Forgejo-shaped fields to decide
  whether main can be written by anything but a reviewed merge (see
  `main_is_protected` in `bin/fleet-watch`); translating GitHub's differently-shaped
  answer into them would mean inventing values GitHub never sent, which is
  the "approximating" plan 0052's boundary forbids outright. The `github`
  backend therefore returns `(0, None)` **without making a request at all** —
  the same "could not tell" every other unreadable-protections case already
  produces, which `main_is_protected` already treats as a refusal, not a
  pass. This is a real capability gap, not an oversight: a repo on the
  `github` backend cannot today be vouched for as protected by this path. See
  the handoff to whichever plan next builds a GitHub-shaped protection read.
- **`post_status` chose the Statuses API over Check Runs.** GitHub has two
  ways to report a commit's build state. Check Runs is aimed at GitHub
  Apps — it wants an installation token with `checks:write`, not the plain
  PAT this module holds, and its response carries fields
  (`conclusion`, structured `output`) this module has no verb shaped to
  return. The Statuses API takes the same three fields Forgejo's endpoint
  does (`state`, `context`, `description`) and authenticates with the token
  already in hand, so it is the one `_GithubBackend.post_status` calls — no
  behavioural difference for any caller.
- **`get_contents`/`list_contents`: both base64-encode a file's content, but
  GitHub additionally refuses to inline it past roughly 1MB** (encoding comes
  back as `"none"` instead, and the caller would need the separate Git Blobs
  API to fetch it — not implemented here). Forgejo imposes no such limit on
  this fleet's self-hosted instance. Neither backend's `get_contents`
  attempts to paper over this: the module hands back whatever the forge
  said, same as it always has for a non-JSON or unexpected body.
- **`search_repos` wraps its rows under a different key on each backend, and
  is not normalised (plan 0057).** Forgejo's `/repos/search` with no query
  lists every repository the token can see, under `data`. GitHub has no
  matching "list everything visible, no filter" endpoint — its nearest
  equivalent is the Search API (`GET /search/repositories`, which does not
  even share the `/repos/` path prefix every other verb does), and it wraps
  rows under `items` instead. GitHub's search additionally requires a
  non-empty `q` in production; `_GithubBackend.search_repos` sends `q=""`
  rather than inventing a query no caller here has ever needed, which is a
  documented gap the same way `get_branch_protections` is one — a caller on
  the `github` backend wanting this verb for real would need to extend it
  with a query, not assume the Forgejo shape. Normalising the two response
  shapes into one key was considered and rejected: it would hide a real
  difference from whichever caller reads the body, relocating the bug to
  wherever that caller assumes the wrong key rather than removing it.

Four of these WRITE, and the list is meant to stay short enough to read:
`post_status`, `create_issue_comment`, `edit_pull_body`, and `create_pull`. The
third takes a BODY and nothing else — not title, base, state or labels —
because a verb that can retarget or close a pull request is a different
capability from one that can annotate it, and the module has no business
holding the first. It exists because the `Plan: <id>` marker is what makes
dedupe work and asking a model for it in the prompt does not reliably produce
it: pr-proxy #42 and #79 wrote it as a path rather than an id, and speakhush
#162 omitted it entirely. `verify_marked_pr` stamps it, and only onto a pull
request it has already proven is its own.

`create_pull` is plan 0070's: `bin/fleet-lane` is its one caller, opening the
`Tests-for: <plan>` pull request a test lane's result lands as. It never sends
a `draft` field — Forgejo's create-pull API has none, so the caller marks a
draft the way both platforms honour, a `WIP: ` title prefix — and it never
approves or merges anything; that capability does not exist anywhere in this
module (plan 0011 §3, still true).

## The contract

- **Returns `(status, body)`. Nothing raises.** `URLError`, `OSError`,
  `http.client.HTTPException`, `ValueError` (including `UnicodeEncodeError`
  and the header-injection guard's "Invalid header value", which carries the
  token verbatim in its text) — all of it becomes `(0, None)`, with the
  exception's own text discarded, never logged or re-raised. An HTTP error
  status becomes `(code, None)`.
- **`0` means "could not tell", never "no".** A caller that collapses `0`,
  `401`, `403` and a `5xx` into "not found" fails closed in a way that looks
  like a policy decision (`fleet-repo` exits 2, NOT DETERMINABLE, on exactly
  these). The module hands back the code; the caller decides.
- **The token lives only in the `Authorization` header.** Not in a URL, not
  in an exception message, not in `repr()`/`vars()` of a `Forge` (held in a
  `__slots__` field with no backing `__dict__`, and a hand-written `__repr__`
  that never mentions it). The module reads no environment variable and runs
  no token command — which token a caller holds (admin vs implbot) is the
  caller's business.
- **Every interpolated path segment is percent-encoded**, including the
  `owner`/`repo` split. `urlopen` raises `UnicodeEncodeError` on a raw
  non-ASCII segment; encoding lets a non-ASCII branch name or contents path
  succeed instead of crashing the caller.
- **`repo` is always `owner/repo`.** The module never supplies an owner, and
  every verb validates shape with the existing `fleetlib.validate_repo` — not
  a second, subtly different regex (a second one is how `_ID_RE` once allowed
  `/`, review round 1 of PR #9). `validate_repo` calls `sys.exit`; since a
  `SystemExit` reaching a verb's caller would be exactly the crash-loop class
  this module exists to forbid (one malformed repo string must not end a run
  that is partway through several others), each verb catches it under
  `try/except SystemExit` and returns `(0, None)`, the same outcome any other
  malformed request gets — the exit itself never happens inside a verb. This
  is the reading review 2119 on PR #116 settled: the amended plan text still
  says "at the CLI edge or in the `Forge` constructor", but the constructor
  takes no `repo`, so validation cannot live there; each consumer keeps its
  own edge behaviour on top of the module's guarantee (`fleet-reviews`: one
  `_FAILURES` entry and continue; `fleet-candidate`: exit 2 with its own
  message). `fleetlib.validate_repo`'s regex is lowercase-only with a 64-char
  component cap, stricter than `fleet-candidate`'s pre-migration inline check
  (`a.repo.count("/") != 1 or not all(a.repo.split("/"))`, which accepted
  uppercase and longer components) — a small behaviour change, not "unchanged"
  (review 2146 LOW).
- **A redirect forwards the `Authorization` header to another host.** This is
  `urllib`'s default redirect handler, not something this module adds, and
  predates the module (the open-coded `_api`/`_get` predecessors had the same
  exposure) — not a regression, but worth stating rather than leaving implicit
  now that the behaviour is centralized in one place (review 2146 LOW).

## `paged()` and the cap-hit rule

`fleetforge.paged(verb, *args, cap=None, page_size=50, **kwargs)` returns an
iterable of pages (lists of items). After it is exhausted, `.outcome` is
exactly one of:

- `EXHAUSTED` — a page came back shorter than `page_size`; there is nothing
  more to read.
- `CAP_HIT` — `cap` pages were fetched and the last one was full; there may
  be more beyond it.
- `FAILED` — a page came back as anything other than `(200, list)`.

When `.outcome` is `FAILED`, `.failed_status` carries the verb's own `status`
for the page that failed — an HTTP code such as `401` or `403`, or `0` for a
transport failure that never got a status. `None` for the other two outcomes.
Collapsing every `FAILED` page into the bare string `"failed"` discarded
exactly the thing that told an expired token (`401`) apart from Forgejo
being down (`0`) — the pre-module `fleet-reviews` recorded `-> HTTP 401` /
`-> URLError` for this reason, and `_paged_all` now reports `-> HTTP
{failed_status}` when `.failed_status` is truthy, falling back to `.outcome`
only for the opaque `status == 0` case (review 2146 M1).

`docs/plan-dispatch.md` is explicit that hitting a cap must read as
*unreadable*, never as "no more results": a truncated scan authorising a
duplicate dispatch is worse than one that runs long. `CAP_HIT` and
`EXHAUSTED` are therefore always distinguishable by the caller through
`.outcome`, without inspecting page contents or comparing page length at the
call site.

`fleet-reviews`' backfill pages with `cap=None` (unbounded) deliberately: a
backfill that stops early and reports "complete" is worse than one that runs
long. A future caller that does set a cap must treat `CAP_HIT` the same as
`FAILED` — as an incomplete read, not a short one.

### `bin/fleet-orphans` does not use `paged()` (plan 0057 D3)

`paged()`'s `EXHAUSTED` rule is **a page shorter than `page_size` ends the
scan** — plan 0022's own shortcut, chosen because every migrated caller so far
could tolerate it. `bin/fleet-orphans` cannot: plan 0020 §3 requires paging to
a **confirmed empty page**, never stopping on a short one, because a short
page is also what a truncated response looks like, and the 2026-09-07 failure
this tool exists to catch was exactly that — an open PR read as "no PR" because
one (short) page was taken for the whole set. Trading that property for
`paged()`'s shared helper would have quietly loosened the one guarantee this
tool's absence result depends on: "no orphans" has to mean absence, not "no
orphans on the pages I happened to read."

Rather than add a strict mode to `Paged` — which every other caller of
`paged()` would then have to consciously not opt into, since `EXHAUSTED`
already means something weaker for them — `bin/fleet-orphans` kept its own
local pagination loop (`_paginate`, `_commit_pages`) and simply pointed it at
the new `Forge` verbs (`list_branches`, `list_commits`, `list_pulls`,
`search_repos`) instead of a raw `_get`. The loop shape is unchanged from
plan 0020; only the transport underneath it moved. This is the cheaper and
more legible option: one file's pagination boundary stays visible in that
file, instead of a second, stricter mode threaded through `Paged` that only
one caller would ever set.

## Migrated consumers

`bin/fleet-reviews` and `bin/fleet-candidate` migrated fully in this plan.
Both keep their existing exit codes and output shape; both moved their
Forgejo-facing tests onto a stub `http.server` in a background thread. Two
small behaviour changes came with the move, called out here rather than
claimed away as "unchanged" (review 2146 LOW):

- `fleet-reviews` reads `FLEET_FORGEJO_URL` (the bare origin every other
  fleet tool reads) and falls back to `FORGEJO_API` (shaped `<origin>/api/v1`)
  with the `/api/v1` suffix stripped before use, warning once on stderr when
  only the old knob is set. `FLEET_FORGE_OWNER` (default `operator`) qualifies
  a bare repo name at the CLI edge — in the request paths, the stored `repo`
  column, and the `--report --repo` filter alike, so the three cannot desync.
- `fleet-candidate` reads `FLEET_FORGEJO_URL` unchanged (its existing knob and
  default) and now validates its `repo` argument with
  `fleetlib.validate_repo`, catching the `SystemExit` it raises so the CLI
  keeps its own `warn()` + exit-2 convention rather than adopting
  `validate_repo`'s generic exit. **This is stricter than the inline check it
  replaced** (`a.repo.count("/") != 1 or not all(a.repo.split("/"))`):
  `validate_repo`'s regex is lowercase-only with a 64-character component
  cap, so a repo name the old check accepted — uppercase, or a longer
  component — is now refused at the CLI edge instead of reaching the network.
- `fleet-reviews` keeps its pre-migration 30s `urlopen()` timeout by passing
  `timeout=30` to `Forge()` explicitly; `fleet-candidate` keeps its
  pre-migration 20s, which is also the module's default, so it passes
  nothing.

`bin/fleet-orphans` migrated in plan 0057, the same shape `bin/fleet-watch`
used in plan 0051: `fleetforge` is loaded by `SourceFileLoader`, not
`import` (consistent with how this file already loads `fleetlib` and
`fleet-repo`'s module), and its tests replaced the `om._api` monkeypatch with
a `Forge`-shaped double (`_ForgeDouble` in `tests/test_fleet_orphans.py`,
mirroring `tests/test_fleet_watch._ForgeDouble`) wired through a `_forge`
builder rather than a raw dispatcher. It keeps its pre-migration 20s
timeout, which is also `Forge`'s default, so it passes nothing explicit —
same as `fleet-candidate`.

## Migration ledger

Every remaining line in `bin/` (outside `fleetforge.py`) that open-codes a
forge-shaped call — grep `_api(`, `_get(`, `urlopen(`, `http\.client`.

**The `file:line` column below is a snapshot, for humans reading this doc —
it is NOT what the test enforces, and it WILL drift.** Review 2146 H1 found
exactly that: a landed +15-line shift on `main` (the bare import moved
45→60, `_api` 340→355, the ntfy call 816→831, the comments call 1093→1108)
broke both ledger tests. `tests/test_fleetforge.py` now pins each call site
by **file + the matched line's own text**, not by line number, so it
survives exactly this kind of drift; only a real change — a call site added,
removed, or migrated — moves the pin. Re-run `git log -L` or grep the
pattern above against current `main` if a cited line number here looks
wrong; the test, not this table, is authoritative.

| File:line | What | Owning lane |
| --- | --- | --- |
| `bin/orchestrator:453` | **angelia**, not the forge — the fleet's push service, reached with its own per-app service token | never migrates; `fleetforge` speaks only Forgejo |
| `bin/fleet-watch:77` | `import http.client` (backs the two entries below) | never migrates — see below |
| `bin/fleet-watch:286` | `_UnixHTTPConnection(http.client.HTTPConnection)` — the LOCAL forgejo-broker transport, not a direct Forgejo call | never migrates — see below |
| `bin/fleet-watch:287` | docstring on the class above | never migrates — see below |
| `bin/fleet-watch:301` | `_broker_get()` — the broker's own `(status, body)` verb, a different door than this module | never migrates — see below |
| `bin/fleet-watch:315` | `_broker_get()`'s exception guard | never migrates — see below |
| `bin/fleet-watch:333` | `_broker_get("/health")` call site | never migrates — see below |
| `bin/fleet-watch:364` | `_broker_get(f"/forgejo/protection/{owner_repo}")` call site | never migrates — see below |
| `bin/fleet-watch:1246` | `urlopen()` — an **ntfy** notification POST, not a forge call | non-forge; stays open-coded regardless |
| `bin/fleet-leak-watch:127` | `urlopen()` — an **ntfy** notification POST, not a forge call | non-forge; stays open-coded |
| `bin/orchestrator:169` | `urlopen()` — an **ntfy** notification POST, not a forge call | non-forge; stays open-coded |
| `bin/fleet-orphans:586` | `urlopen()` — an **ntfy** notification POST, not a forge call | non-forge; stays open-coded |

`bin/fleet-watch` is migrated: plan 0051 replaced its own `_api()` and the six
call sites through it (`get_branch_protections`, `list_contents`,
`get_contents` twice over, `list_pulls(sort="oldest")`,
`list_issue_comments(since=...)`) with a `fleetforge.Forge` loaded the same
way `fleetlib` already was — by `SourceFileLoader`, never `import`, because
this file is also loaded from two directories that are not on `sys.path`
(`~/.local/share/pins/eunomia/bin` and keyvault's forgejo-broker deployment on
vaulthost). What is left in the ledger above is not migration debt: the `ntfy`
notification call site (`fleet-watch:1246`, alongside
`fleet-leak-watch:127` and `orchestrator:169`) posts to a notification
endpoint, never to Forgejo, and `fleet-watch`'s local `forgejo-broker`
transport (`_UnixHTTPConnection`, `_broker_get`, lines
77/286/287/301/315/333/364) is a different door — a unix-socket daemon that
itself talks to Forgejo out of process. Neither ever routes through
`fleetforge`.

`bin/fleet-orphans` is migrated (plan 0057). Its own `_api()`/`_get()` are
gone; its four previously-unreachable reads (`/repos/search`, a bare repo
GET, a paginated branches list, a sha/path-filtered commits list) are now
`search_repos`, `get_repo`, `list_branches` and `list_commits` on `Forge`,
alongside `list_pulls`/`get_contents`, which already existed but were left
unmigrated because the tool's own confirmed-empty-page pagination boundary
(plan 0020 §3) is stricter than `paged()`'s short-page-ends shortcut. That
boundary is unchanged: `bin/fleet-orphans` still pages with its own local
loop (`_paginate`, `_commit_pages`), it just calls the six `Forge` verbs
underneath it instead of a raw `_get` (see "`bin/fleet-orphans` does not use
`paged()`" above). What is left in the ledger above for this file is the
`ntfy` notification call site, the same non-forge exception every other tool
here gets.

`tests/test_fleetforge.py` greps this same pattern set on every run and
fails if the live hit set and this table ever diverge in either direction: a
new open-coded call appearing anywhere in `bin/`, or a call site that is gone
from `bin/` but still ledgered. Two separate tests enforce this at two
granularities: `test_the_hand_enumerated_call_site_set_matches_bin_today`
pins each site by **file + the matched line's own text** (immune to the
line-number drift described above — see review 2146 H1/M2); the ledger test
below pins this table by **file + how many times the pattern matches that
file**, which is coarser but is what a `file:line` table that is explicitly
not required to stay numerically accurate can promise.

## Handoff (plan 0022 §5)

- The two migrated consumers needed exactly the verbs the inventory
  predicted (`get_pull`, `get_branch`, `post_status` for `fleet-candidate`;
  `list_pulls`, `list_reviews` for `fleet-reviews`); nothing extra came up
  during migration.
- Failure mode the stub server cannot produce: a Forgejo that answers `200`
  with an HTML sign-in page instead of JSON. The module already handles this
  correctly — `get_pull` et al. return `(200, None)` on a body that is not
  valid JSON — but a caller that only checks `status == 200` and assumes
  `body` is truthy JSON (rather than checking `body is not None` or letting
  a subsequent `.get()` raise `AttributeError` on a string) would read that
  as healthy. `tests/test_fleetforge.py`'s non-JSON-body case pins the
  module's own half of this; nothing here can pin every caller's half.
- `request()` — the raw escape hatch — was never added, so there is nothing
  for the next lane to remove.

## Request cost, and the timeout that was under it

`fleetforge.Forge` defaults to a 20-second timeout. That is **below what this
fleet's own pull-request listing costs**, and the gap discarded two complete
dispatches before anyone measured it.

Measured 2026-09-09 against `operator/eunomia` (247 pull requests):

| request | time |
|---|---|
| `pulls?state=all&limit=50` (one page) | **22.6 s** |
| `pulls?state=all&limit=10` (one page) | 4.7 s |

Forgejo does roughly **0.45 s of work per pull request** on this endpoint, so
the cost scales with the repo's history rather than with the page size. curl and
urllib agreed on the total (23.7 s and 22.7 s), which is what ruled out a client
bug.

The consequence is worth stating plainly, because it was misdiagnosed twice as a
transient blip: the read failed **every** time, deterministically, by sitting a
couple of seconds over the client's patience. The retry added in #246 could not
rescue it — all five attempts hit the same wall — and a run that had produced a
complete, correct pull request was thrown away at the last step.

`bin/orchestrator` now builds its `Forge` with `forge_timeout()` (60 s, ~3x the
measured cost, overridable with `FLEET_FORGE_TIMEOUT`).

**The same 20 s was hardcoded a second time, in `bin/fleet-watch`'s own
`_api()`, and fixing the orchestrator did not reach it.** The two modules each
open their own door to the forge — the ledger below has said so since it was
written — so the watcher went on failing on exactly the read the orchestrator
had just been taught to survive. The first armed scan refused to dispatch:

```
fleet-watch: WARN operator/eunomia: dedupe unreadable for 0029-service-lease — not spawning
fleet-watch: 0 dispatched (cap 1)
```

The dedupe read is a pull listing, so it cost 22.6 s and the client gave up at
20. Refusing there is right — an unreadable dedupe is indistinguishable from
"no marked PR exists", and guessing would re-dispatch merged work — but nothing
was wrong except the patience. `bin/fleet-watch` now has its own
`forge_timeout()` reading the same `FLEET_FORGE_TIMEOUT`.

One consequence that is not local: since infra ADR-0003 §2, `main_is_protected`
runs in the **broker's** process on vaulthost, loaded from a deployed copy of this
file. The watcher's plist cannot change the timeout the broker reads
protections with — the broker has to be redeployed for that.

**This raises the ceiling; it does not lower the cost.** A full scan is still
~22 s per page, so verification on a large repo can take minutes. The cheaper
fix is not to scan at all: `verify_marked_pr` is looking for a pull request that
was created seconds earlier, so a listing sorted newest-first would almost
always answer on page one. `paged` has no early exit today, which is why that is
a separate change rather than a line in this one.
