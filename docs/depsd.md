# fleet-depsd

`fleet-depsd serve` (plan 0181) is a second resident process, alongside
`fleet-watch`, that re-checks every held plan when a watched repository's
`main` moves or the owner attests an `external:<slug>`. It owns two derived
tables in `fleet.db` — `dependency_hold` and `dependency_scan` — and wakes
`fleet-watch` when a plan's last hold clears, so it dispatches within the
watcher's one-second sleep tick rather than waiting for the next full cycle
(up to the watcher's own poll interval).

It does not resolve dependencies itself: every check goes through
`bin/fleet-watch`'s `_plan_dependency_blocks` (plan 0180 D3), the one
definition of "met" the dispatch loop also uses. It never dispatches, never
spawns an orchestrator, and never writes to a repository.

## What triggers a recompute

`recompute()` is one full pass: for every `dispatch` repository in
`config/repos.conf`, read `plans/` on `main`, and for every `ready` plan
resolve its `depends_on` through `_plan_dependency_blocks`. It then replaces
that repository's rows in `dependency_hold` in one transaction, and records
the pass in `dependency_scan`.

A recompute runs on:
- a verified `push` delivery to a watched repository's `main` (below);
- a successful attestation write (the POST/DELETE API, below);
- a poll backstop, every `FLEET_DEPSD_POLL` seconds (default 600, accepted
  60..3600) — this alone is enough to clear every hold eventually, with no
  webhook installed at all;
- start-up.

A trigger only sets a dirty flag; one worker loop runs `recompute()` while
the flag is set, so a burst of triggers costs one pass.

When a repository's `plans/` cannot be read, its previous `dependency_hold`
rows are kept — never deleted on a read failure — and its `dependency_scan`
row is marked unreadable.

`fleet-depsd serve --once [--dry-run]` runs a single pass with no bind and no
service lease, then exits. `--dry-run` prints what it would record and
writes nothing — not the tables, not a wake file.

## The webhook: `POST /webhook/forgejo`

The signature (`X-Forgejo-Signature`, an HMAC-SHA256 hex digest of the raw
body) is verified before anything is parsed. A missing or bad signature, or
no secret loaded, is answered without reading the payload. At most 1 MiB of
body is read; a larger declared or actual body is refused.

A verified delivery that is not a `push` to `refs/heads/main` of a
`dispatch` repository is acknowledged and changes nothing. Otherwise the
dirty flag is set — **the payload is a trigger, never a fact**: `recompute()`
re-reads `main` from the forge itself, never from the webhook body (SPEC
Principle 3).

| condition | status |
|---|---|
| body over 1 MiB (declared or actual) | 413 |
| no secret loaded | 503 |
| missing or wrong signature | 401 |
| not a push to a watched `main`, and not a recognised `pull_request` action for an enrolled repo | 204 |
| push accepted; dirty flag set | 202 |
| `pull_request` accepted; a `plan_review_job` row is persisted | 202 |
| `pull_request` action matched an enrolled repo, but the pull request is currently draft, closed, or merged, or its change is not plan-only | 204 |
| `pull_request` action matched, but the pull request, its changed-file list, or the queue database could not be read | 503 |

Only the delivery id, the repository and the outcome code are logged — never
a header value or the body.

The secret is read from the stdout of the command named by
`FLEET_DEPSD_WEBHOOK_SECRET_CMD` (default a helper from the hosttools
sibling plan) and held in memory only. Fetched at start, then retried no more
often than once per `FLEET_DEPSD_POLL` (the AppRole helper logs in and
revokes its own login token on vaulthost each time it runs — trying it every
second forever, for as long as the value stays unavailable, would hit that
AppRole once a second). If it is unavailable at start, one page goes out and
the webhook route answers `503` until a later poll fetches it; the poll
backstop keeps clearing holds regardless. The attest token
(`FLEET_DEPSD_ATTEST_TOKEN_CMD`) is fetched and retried the same way, but
pages nothing of its own — a missing attest token only blocks a write
nobody but lynceus and the owner make, never a recompute.

## What each page means

This service pages through the same `watch.notify` the resident watcher
uses, so a page from either lands in the same place. One page is
`fleet-depsd`'s own:

| title | means | what to do |
|---|---|---|
| `fleet-depsd: no webhook secret` | `FLEET_DEPSD_WEBHOOK_SECRET_CMD` produced nothing at start (or has not since the last time it succeeded) — the webhook route answers `503` until it does | Check the helper runs cleanly by hand (it is the hosttools sibling plan's script; see the call-site comment above it in `bin/fleet-depsd` for its name and commit). Nothing else is broken: the poll backstop still clears every hold on its own cadence, just not within a second of a push. |

Every other failure mode this service can reach — a held service lease
refusing a second instance, a lockfile held by another process on this host,
a recompute that raised and was logged — is a refusal to START or a logged
`WARN`, not a page: each is either visible the moment an operator looks
(`fleet-depsd` simply does not run) or self-heals on the next pass (a
recompute that raised this cycle tries again next poll). Only the condition
above is silent otherwise, which is why it is the one that pages.

## PR lifecycle deliveries: queueing a plan review (plan 0189)

`POST /webhook/forgejo` also answers a `pull_request` event, beside the
`push` handling above, through the SAME signature check. A delivery whose
`action` is one of `opened`, `reopened`, `synchronize`, `synchronized`, or
`ready_for_review`, for a repository listed in `config/repos.conf` (any
entry, not only a `dispatch` one — the same registry `bin/fleet-plan-review`'s
own `_enrolled()` reads), reads that pull request's CURRENT head from the
forge — **never trusted from the delivery's own `head` field**. If that
read shows the pull request currently draft, closed, or merged, or its
changed-file list is not plan-only (`changed_paths_for_position`/
`reviewer_position`, the same classification `_reconcile_plan_reviews`
below and `bin/fleet-plan-review` itself both use), nothing is written and
the delivery answers `204`. The plan-only check exists because this
delivery fires for EVERY pull request in an enrolled repository, code
changes included — without it, every lifecycle event on a code PR would
queue a job that only spends one of the worker's bounded per-pass claims on
`_dispatch_one` terminal-fencing it `skipped` for "not a plan filing" the
moment it is claimed.

Writing a row that `_dispatch_one` would only terminal-fence `skipped` a
moment later would otherwise plant a dead end at that exact head, since
plan 0188's `_enqueue()` is idempotent on `(repo, pr, head_sha)` — a LATER
delivery for the identical head (the same PR taken out of draft, or
reopened, with no new commit) would then find that same `skipped` row
again. This handler does not rely on never writing one in the first place
to stay out of that dead end: when `_enqueue()` returns an EXISTING row
(one this handler, `_reconcile_plan_reviews` below, or `bin/fleet-plan-
review`'s own worker re-checking eligibility at claim time, already left
`skipped`) and the PR is now confirmed eligible, `bin/fleet-depsd`'s own
`_enqueue_or_revive` revives that row to `queued` itself — see its
docstring — rather than leaving it unchanged. Reviving is always correct at
that point, never merely convenient: draft/open/closed, enrollment, and the
changed-file position are exactly the attributes that can flip at a FIXED
head with no new commit, and every one of them has just been re-checked,
fresh, immediately before — a row found `skipped` here was always skipped
for one of those, never for a change whose position cannot change without a
new commit. So both the handler's own write-time check above and the
worker's narrower, independent claim-time race (the PR going ineligible
AFTER a `queued` row exists but BEFORE `bin/fleet-plan-review`'s worker
claims it) land on the same outcome: a later delivery at that identical
head finds the row revived, never a permanent no-op. Otherwise the job is
persisted through `bin/fleet-plan-review`'s existing queue (plan 0188's
`_enqueue()`) before answering `202`. Any other action, an unenrolled
repository, or a pull request the forge cannot currently read answers `204`
(ignored) or `503` (the pull request, its changed-file list, or the queue
database could not be read) — never `202` for a delivery nothing was
written for. Whether the deployed Forgejo
itself redelivers a non-2xx response has not been confirmed (see
"Enrolling `pull_request` deliveries" below); the actual recovery for a
`503` or a dropped delivery alike is the reconciliation sweep below, which
finds the same pull request again on its own cadence regardless of
whether anything redelivers.

Because the head is always read fresh, a replayed or out-of-order delivery
for one pull request collapses to exactly one row for whatever the forge
says the head actually is when each is handled — the delivery's own
ordering never matters. A genuine head move between two deliveries is not
collapsed: it is the ordinary case plan 0188's `superseded` state exists
for.

**Nothing here dispatches a review.** Persisting the job only sets
`review_dirty`, which wakes the resident service's own worker loop, below
— no model call, and no forge call beyond the one head read, happens on the
HTTP handler's own thread.

### Enrolling `pull_request` deliveries — what the owner must confirm first

The `action` allowlist above is **provisional**. No delivery from the
deployed Forgejo instance has been captured to confirm it: doing so means
subscribing a hook to `pull_request` events, which is outside what an
implementer session may do (repository webhook configuration is a
protected action). Forgejo is a Gitea fork, and Gitea's own webhook struct
names a push onto an open pull request's branch `synchronized`
(`api.HookIssueSynchronized`), not GitHub's `synchronize` — both spellings
are accepted so neither lineage's naming silently drops the trigger, but
this has not been checked against `forge.example` itself.

Before subscribing a production hook to `pull_request`:

1. Add the event to one hook (a scratch repository is safest) and open,
   reopen, push to, or take a draft pull request out of draft there.
2. Read that hook's own delivery history (Forgejo's hook management UI, or
   `GET /repos/{owner}/{repo}/hooks/{id}/history` with a token that has
   admin rights on the repository — `fetch-forgejo-token.sh`'s implbot PAT
   does not) and record the exact `action` string(s) seen.
3. If any differ from `_PR_LIFECYCLE_ACTIONS` in `bin/fleet-depsd`, open a
   follow-up PR narrowing or widening that set before relying on it in
   production — the reconciliation sweep below is the backstop that keeps
   recovering plan-filing reviews in the meantime, action names aside.
4. Only then add `pull_request` to the repositories' webhook `events` list
   (the existing Forgejo hook management workflow this service's `push`
   subscription already uses — no new Hosttools capability is needed: the
   route, the secret, and the signature check are all already in place for
   `push`).

Until step 4, this path runs on the reconciliation sweep alone.

## Reconciling, and the bounded worker wake (plan 0189)

`serve` (resident) runs a second loop, on its own thread — deliberately
separate from the one that triggers `recompute()` on a push, so a slow or
waiting review can never delay dependency processing. Each pass:

1. **Reconciles** a bounded slice of the enrolled registry: up to three
   repositories (a resumable cursor, `plan_review_reconcile_cursor`, so a
   large registry is covered over several passes rather than one burst).
   Within each chosen repository, EVERY open pull request is visited, not
   only the first ten: the forge's own listing is paged ten at a time, up to
   ten pages (so up to 100 open pull requests per repository per pass) —
   enough to cover any realistic backlog in one pass, and a `WARN` if even
   that is not enough, rather than a silent ceiling that quietly never
   revisits a plan PR sorted past the first page. Each pull request's
   changed-file list is classified the same way `bin/fleet-plan-review`
   itself does (`reviewer_position`); only a plan-only one is enqueued.
   `_enqueue`'s own idempotence on `(repo, pr, head_sha)` means a pull
   request whose current head already has any row — done, held, queued,
   whatever — costs one no-op write, which is what "exclude completed
   current heads" comes down to. A row sitting `skipped` at that exact head
   is revived to `queued` instead of left alone — the same
   `_enqueue_or_revive` helper the `pull_request` webhook handler uses (see
   "PR lifecycle deliveries" above) — so a plan PR the worker once correctly
   skipped as a draft or closed, and has since become eligible again with no
   webhook ever confirming it, is still recovered by the sweep alone.
2. **Drains a bounded worker pass**: `bin/fleet-plan-review`'s own
   `_claim_jobs()`/`_dispatch_one()`, claiming at most three ready jobs, one
   at a time, each immediately before dispatching it. This is the same
   claim and dispatch path `fleet-plan-review worker` itself runs — never a
   restated one — including Mopsus's retry-slot coordination and Talos's
   standing-review check described in `docs/plan-review-dispatch.md`.
   Resolves `bin/fleet-plan-review`'s own `_forge()` BEFORE claiming
   anything, the same ordering `cmd_worker` itself uses and for the same
   reason: a token-helper failure there is a `sys.exit()`, raised as
   `SystemExit`, which no `except Exception` below it catches — resolving
   it first means that failure costs this pass zero claimed jobs and one
   `WARN`, not a daemon thread that dies mid-claim and never runs again
   while the service keeps reporting healthy.

   Two things only THIS wake does, neither inside `_dispatch_one` itself:
   it takes `bin/orchestrator`'s `review_quota_hold` right after claiming,
   before any forge read, holding (no attempt spent) while the owner's
   session/weekly/model gates are closed — the gate the orchestrator's own
   first-attempt review loop already takes before ITS OWN dispatch, which
   this one needs for the same reason that loop does: nobody is watching
   an unattended wake the way a hand-run `fleet-plan-review worker` is. And
   it never reclaims the one job still ready after holding it, within the
   SAME pass: with nothing else in the queue, claiming immediately hands
   the identical row straight back, and reclaiming it up to the pass's own
   limit would spend every one of `MAX_DISPATCH_ATTEMPTS` back to back in
   the same few seconds, for a failure a later pass, minutes apart, might
   not have seen at all.

The pass runs when `review_dirty` is set (a persisted `pull_request`
delivery, or start-up) or `FLEET_DEPSD_POLL` has elapsed since the last
one — the same cadence and the same "a trigger only sets a flag" shape
`recompute()` already uses, so this alone recovers a missed delivery
eventually, with no webhook installed at all.

A dispatch inside this pass can run for several minutes
(`bin/fleet-plan-review`'s own `DISPATCH_TIMEOUT`, 2400s). Because this runs
on its own thread, that time never delays the recompute loop's own push
processing or lease heartbeat.

**What this does not newly solve.** `bin/fleet-plan-review`'s own module
docstring ("Automatic dispatch") proved its coordination contracts safe for
an on-demand, human- or script-decided run, and named three callers that
remain uncoordinated with it: a hand-run `dispatch-review.py`, the
orchestrator's own first-attempt review loop for a PR it owns, and a
resumed orchestrator session reached through Mopsus's `kind="plan"` retry.
Running the SAME worker on a schedule here does not close those — it only
means the window in which they can race is now open whenever this service
is, not only when someone remembers to run `fleet-plan-review worker` by
hand. Concretely: this service's own worker pass and one of those three
other callers can each independently decide a plan PR needs a review and
both dispatch one for the identical head, for as long as this service
keeps running rather than only for the few minutes a hand-run `worker`
takes — doubling the review spend on that head, not merely risking a race.
This is an accepted cost of running the worker unattended, not something
`bin/fleet-depsd` (this plan's own lane) can close: the claim-fencing, the
retry-slot, and the pre/post-dispatch reconciliation `_dispatch_one`
already does, in `bin/fleet-plan-review`, are the only protection against
them, same as before plan 0189 — a lease or other coordination across
those three callers and this one would mean touching `bin/fleet-plan-
review` and `bin/orchestrator`, both outside `paths:` here.

## The read and attest API

Every response is JSON.

- `GET /api/v1/holds` → `{"computed_at", "unreadable": [repo, ...], "holds":
  [{"repo", "plan", "dep", "form", "reason", "detail", "since"}, ...]}`.
  `computed_at` is the OLDEST `dependency_scan.scanned_at` across every
  repository, so a stale view reads as stale. Unauthenticated — every field
  is already readable in the plan files themselves.
- `GET /api/v1/attestations` → `{"attestations": [{"slug", "note",
  "attested_at", "attested_by"}, ...]}`. Unauthenticated, for the same reason.
- `POST /api/v1/attestations` `{"slug", "note"}` → `201` with the row.
- `DELETE /api/v1/attestations/<slug>` → `200`, or `404` if the slug was not
  attested (writes nothing).
- `GET /healthz` → `{"lease", "webhook_secret": bool, "attest_token": bool,
  "last_scan"}` — booleans only, never a secret value.
- Anything else: `404`. A recognised path with the wrong method: `405`.

### How a write authenticates

The POST and DELETE routes are the only code that ever writes
`dependency_attestation`/`dependency_attestation_change` — a test in
`tests/test_fleet_depsd_serve.py` AST-walks `bin/fleet-depsd` for any other
call site, and scans the rest of `bin/` for a raw `INSERT`/`UPDATE`/`DELETE`/
`REPLACE` against either table.

A write is protected by **two** checks, in this order:

1. **The connection's peer address must be this host** — `127.0.0.1`, `::1`,
   or the configured bind address. A real remote peer cannot complete a TCP
   handshake while claiming one of this host's own source addresses, so this
   check is exactly "this host", and it is checked *before* the
   `Authorization` header or the body is ever read. Any other peer gets
   `403`, logged with the peer address only — no header, no body, no token.
   This is why the write stays safe on a plain-HTTP LAN bind: a bearer token
   sent from off-host would cross the wire in cleartext, and this check means
   a leaked token is useless from anywhere but the one host that can also
   just read the database.
2. **The caller holds the attest token.** `Authorization: Bearer <token>`,
   compared with `hmac.compare_digest` against the token read from the
   stdout of `FLEET_DEPSD_ATTEST_TOKEN_CMD` (default a helper from the
   hosttools sibling plan), held in memory only. No token loaded: `503`. A
   missing or wrong token: `401`.

The lynceus sibling plan holds this same token (fetched server-side, after
its own owner authentication) and calls this route from the same host; the
phone this plan's handoff describes never holds it.

After the peer and token checks: the slug must match `EXTERNAL_SLUG_RE`
(`422` if not), and for a POST it must be cited by at least one row in
`dependency_hold` (`422` "no held plan cites external:<slug>" if not — this
catches a typo, and there is no way to attest ahead of a plan existing). A
successful write sets the dirty flag, so the clearing hold's wake is written
on the very next pass.

## The wake spool

`fleet-depsd` writes `~/dev/.fleet/wake/<sha1(repo|plan)>.json` — `{"repo",
"plan", "reason", "at"}` — for a `(repo, plan)` that had at least one hold
before a pass and has none after, and is still `ready`. The write is a temp
file in the same directory, `os.replace`d into place, so a reader never sees
a half file; the same `(repo, plan)` always names the same file, so a burst
of clears coalesces.

The write happens **before** the pass's transaction that deletes the hold
rows commits. If the process dies in between, the holds are still in the
table, the next pass sees the same transition and rewrites the same wake —
harmless, since a wake is only a hint — and the other ordering would lose the
wake for good once the deletion had committed.

`bin/fleet-watch` consumes the spool: `_take_wakes()` lists it, considers
only names matching `^[0-9a-f]{40}\.json$` (never a `.tmp-*` file the
producer may still be writing), and unlinks each match before acting on it
(consume-once). `_service_wakes()` then runs the exact gates the dispatch
loop applies — the quota hold, the cap, the protection verdict, main's own
health, dependencies, overlap, dedupe, the review path and the human-merge
gate — through `_dispatch_one`, the same per-plan function the cycle's own
loop calls. **A wake is a hint, never an instruction**: anything that can
write to the fleet state directory could write one, so the worst a forged
wake can do is cost one read.

This runs in the resident watcher's one-second sleep tick, whenever the spool
holds at least one file matching the wake-name pattern — a stray non-wake
file (an orphaned `.tmp-*`, a `.DS_Store`) is never enough to trigger the
preflight below. A failed preflight (the broker down, no token, the
orchestrator not ready) leaves the wake files in place and backs off for 60s,
so a sustained outage is not re-probed every second. It also runs inside the
watcher's own cycle, after every top-level sweep and before every repository
in its dispatch loop — a cycle has run 647–1061s on opshost against the default
120s `FLEET_WATCH_INTERVAL`, which leaves no sleep tick for a wake to wait
on. `fleet-watch`'s own cycle summary line carries each top-level phase's
seconds (`phases: question=…s orphan=…s lane=…s meeting=…s
main-health=…s dispatch=…s`), so the worst-case wake latency — the longest
phase between two checkpoints — can be read straight off the log.

## Install order

1. Advance the eunomia pin so the new code, `dependency_hold` and
   `dependency_scan` are in the checkout `fleet-db-migrate` and this service
   run from.
2. Run `fleet-db-migrate`.
3. Install `org.eunomia.fleet-depsd` (`launchd/org.eunomia.fleet-depsd.plist`).
4. Check `/healthz`, and that `/api/v1/holds` lists today's holds.
5. Once the hosttools sibling plan that files the push-webhook installer
   script is `done`, run it as that plan documents, and cite its plan id and
   commit here. Until then, this service runs on the poll backstop alone —
   every hold still clears, just on `FLEET_DEPSD_POLL`'s cadence rather than
   within a second of the merge.

## What this service is not

It is not a second place dependency resolution is decided — see
`_plan_dependency_blocks` in `docs/plan-dependencies.md`. It is not a queue
that exists only in a running process (SPEC Principle 2): every hold and
scan result lives in `fleet.db`, and killing this process at any instant
loses nothing the next pass would not recompute. It is not authenticated by
TLS: the same-host peer check is what makes a plain-HTTP LAN bind safe for a
write route, and adding TLS here, or moving a caller off this host, changes
that contract and needs its own plan.
