# A plan-filing PR's review survives the filing session

A plan is filed as a pull request carrying nothing but prose — the plan
document itself, maybe an ADR, maybe a line in another plan's `depends_on`.
Before plan 0188, that review happened only two ways: the filing session
stayed alive long enough for CI to go green and ran `dispatch-review.py`
itself, or a human remembered to run it by hand once CI settled. Neither
survives the session ending, which it always does — CI takes minutes and a
session does not wait around for it.

`bin/fleet-plan-review` is the durable queue and its bounded worker:

```
fleet-plan-review enqueue <owner/repo> <pr> [--db PATH]
fleet-plan-review status [--repo R] [--pr N] [--json] [--db PATH]
fleet-plan-review worker [--limit N] [--claim-ttl SECS] [--db PATH]
```

`enqueue` is idempotent on `(repo, pr, head_sha)` — a `UNIQUE` constraint on
the table itself, not only an application-level check, so filing the same PR
twice (the filing session, then a later sweep) writes one row. `worker` runs
ONE bounded pass over whatever is ready; it is not a daemon, and nothing in
this plan starts it on a timer — see "Why no launchd unit" below.

A sweep claims least-recently-touched jobs first (`updated_at`, not
`created_at`). Every hold bumps `updated_at`, so a job stuck held sweep
after sweep — CI permanently red, a quota drain that never resolves —
sinks behind every job this worker has not looked at since, instead of
keeping the front of the line forever just because it was filed first.
`created_at` orders a tie between two untouched jobs, so a worker running
against an otherwise-idle queue still claims oldest-first.

## The queue

Two tables, in the exact shape plan 0180's `dependency_attestation` /
`dependency_attestation_change` established: a table of current state with
non-null REAL Unix `created_at`/`updated_at`, and an append-only history
table carrying an `actor` on every row. Both are migrated by
`bin/fleet-db-migrate` like every other owner's.

`plan_review_job` — one row per `(repo, pr, head_sha)`:

| column | meaning |
|---|---|
| `state` | `queued` \| `held` \| `claimed` \| `done` \| `superseded` \| `skipped` \| `failed` |
| `reason` | why it is in that state, in the exact words the next sweep or a human reads |
| `attempts` | how many times a dispatch was actually spent on this row |
| `claimed_by`, `claimed_at` | which worker holds it now, and since when |

`plan_review_job_change` — one row per transition, `action` + `detail` +
`actor` + both timestamps. Every write to `plan_review_job` goes through one
function, `_transition()`, which stamps both tables together — there is no
second writer to drift from it.

### States

- **queued** — filed, never yet claimed.
- **held** — claimed at least once, found not ready: CI still running, CI
  red (rechecked, not abandoned — the usual fix is a second push's status on
  the SAME head, and a job is only ever keyed by head when a push actually
  moves it; `eunomia/deps` is dropped from this check before it runs — see
  "The dependency exception" below), the changed-file list unreadable or
  incomplete, the forge unreadable, mopsus's review-retry slot busy, or a
  stop mopsus's own retry policy already owns (see "Quota and other stops"
  below). Re-read on the next sweep; nothing here acts on how long it has
  been held (SPEC Principle 4 — a held entry ages in view, never
  auto-abandoned).
- **claimed** — a worker holds it now. Abandoned past `--claim-ttl` (default
  3000s: `orchestrator.dispatch_review()`'s own 2400s subprocess timeout plus
  600s of headroom for the forge reads `_dispatch_one` makes before it ever
  reaches that call — never the bare 2400s, which would read a worker still
  legitimately inside its own dispatch as abandoned), it is reclaimed by
  whichever worker's sweep notices next — always by re-reading the forge
  from scratch, never by trusting what the dead worker last saw. Every write
  this worker makes to a claimed job is itself conditional on still being its
  holder (`_fenced`), so even a claim reclaimed by a misconfigured, shorter
  `--claim-ttl` cannot have its outcome overwritten by the worker that lost
  the race — only left for the new holder to resolve.
- **done** — EVERY currently configured plan-reviewer seat's OWN review
  stands at this head: ordinal 1 (the `reviewer` role's account) always, plus
  ordinal 2 (`second_reviewer`, `rev2bot`) whenever `bin/fleet-models`'
  `repo_model` has it enabled for this repo's `plan_reviewer` position — which
  is every production repo today, through its `default` row. Checked through
  the SAME `reviews_at()` the orchestrator and mopsus already use, narrowed
  per ordinal to THAT ordinal's own account: a `rev2bot`-only review does not
  mark ordinal 1 done, and an ordinal-1-only review does not mark a
  two-seat repo done (ADR-0021 §4, ADR-0028 §1 — only an ordinal's own review
  settles that ordinal; the other reviewer's never does). Seats are resolved
  from this worker's own `con` (the same `repo_model` table `fleet-models
  reviewers` reads, no subprocess); a database with no `repo_model` table at
  all (every test fixture here) or no row for this repo falls back to a
  single required seat, ordinal 1. D5: **a HIGH is terminal for this head** —
  a seat's own review that withholds (posted as COMMENT, never APPROVED)
  settles that seat exactly like an approval does, since a push to a NEW head
  is the only thing that could reopen it, never a redispatch at this one. The
  `reason` records each required ordinal's own outcome (`approved`, or
  `not approved (terminal...)`), never collapsing both into a bare "posted".
- **superseded** — the PR's head moved since this row was filed. Terminal
  for this row, but only once the replacement row for the new head is
  already committed: the two writes are ordered so that a crash between
  them leaves the replacement in place rather than losing the row that
  would have picked the review back up. A head that the forge reports as
  unreadable (status 200, but no `head.sha` on the pull request body at all)
  is held, not superseded — the row would otherwise go terminal with no
  replacement committed (the guard that files one only runs when a new sha
  actually exists), silently dropping the review.
- **skipped** — terminal, and never retried by THIS worker's own `_claim_jobs`:
  closed, merged, a draft, the repo is not enrolled (`config/repos.conf`,
  through `bin/fleet-repo`'s own `read_conf()` — the one registry, never a
  second list), or the change is not plan-only (talos's own
  `reviewer_position` would hand it to `code_reviewer`, and a reviewer this
  queue exists for is the wrong one for that change). A row at exactly this
  state and head is the one case `bin/fleet-depsd` reaches back INTO this
  queue to revive rather than leave alone — both its `pull_request` webhook
  handler and its reconciliation sweep share the one `_enqueue_or_revive`
  helper for this (see `docs/depsd.md`, "PR lifecycle deliveries" and
  "Reconciling, and the bounded worker wake"): draft/open/closed and
  enrollment are attributes that can flip at the same head with no new
  commit, and `_enqueue()`'s idempotence on `(repo, pr, head_sha)` would
  otherwise make a later, now-eligible delivery or sweep pass for that
  identical head find this same row again, forever. A row skipped for "not
  plan-only" is never revived this way — the change at a fixed head cannot
  become plan-only without a new commit, which this worker's own supersede
  handling above answers with a fresh row, not a revival of this one.
- **failed** — a genuine dispatch failure, after `MAX_DISPATCH_ATTEMPTS`
  (3) of this worker's own attempts, with the dispatcher's own detail text
  as the reason.

## The filing classifier

Eligibility for THIS queue is exactly talos's own plan-vs-code split, so a
job accepted here is a job `dispatch-review.py` would also have resolved to
`plan_reviewer` had it run by hand. `bin/fleet-plan-review` ports three
pieces of talos `dispatch-review.py` (operator/talos, read from `~/agent` at
commit `2db65f5`, 2026-11-04):

- `_is_prose_path` / `reviewer_position` — the allowlist: `plan_reviewer`
  only when every changed path is prose under `plans/` or `docs/`, else
  `code_reviewer`. Ported verbatim, with the citation beside it in the code.
- the rename-pairing rule from `changed_files_for_position` — a renamed
  entry contributes BOTH its new filename and its `previous_filename`, so a
  file moved into or out of `plans/`/`docs/` is judged by what it used to be
  too.

`tests/test_plan_review_queue.py` proves the port's behaviour on the four
shapes a changed-file list can take: **plan-only** (resolves
`plan_reviewer`), **mixed** (resolves `code_reviewer`, skipped by this
queue), **renamed** (both directions — into prose and out of it — judged by
the OLD path), and **unreadable** (the forge's file list could not be read at
all, which holds rather than ever resolving `plan_reviewer` on a partial
read).

A minimal CI verdict (`ci_verdict`) is ported the same way from talos's own
`ci_verdict`, simplified: it drops talos's `REQUIRED_CI` context check and
returns a bare state — `green`/`pending`/`red`/`absent`/`unreadable` — rather
than talos's two-value `(ok, message)`, because this queue needs to tell
pending from red from absent apart where that collapses into one `False`.
Dropping `REQUIRED_CI` loses nothing a live dispatch would have kept: read
against `~/agent`, commit `2db65f5`, that check only becomes a refusal under
`--require-ci`, which `orchestrator.dispatch_review()`'s own argv never
passes — without it, a missing required context is a WARN that still spends
the round, exactly what omitting the check here produces too.

### The dependency exception

`eunomia/deps` (plan 0107, posted by `bin/fleet-deps`) is filtered out of a
head's CI statuses before `ci_verdict` ever looks at them — never read as a
vote either way, not even to decide "absent". It names whether a plan
filing's OWN dependency edges (the plan it names, or that plan's
`depends_on`) are met on `main`, which gates whether `fleet-merge` will MERGE
the filing, never whether it gets reviewed: ADR-0028 §4, *"A plan PR is never
held, because its edges gate dispatch, not review (plan 0159)"*, and
`docs/deps-status.md`'s own "review dispatcher" section, *"a PR held on a
dependency is still reviewed."* A plan filing whose dependency is not yet on
`main` is the NORMAL case for a plan that depends on one still in review
itself — holding on that `failure` would block the review this queue exists
to run, on a fact review can't change.

## Dispatching, and reconciling before it

On every claim, `_dispatch_one` re-reads the PR, the changed-file list, CI,
and — before ever spending anything — which of this repo's currently
configured plan-reviewer seats still lack their OWN review at the head
(`orchestrator.reviews_at`, the exact function mopsus's own
`held_review_queue` uses, narrowed per ordinal by `_pending_ordinals` to that
ordinal's own account: ordinal 1 the `reviewer` role's, ordinal 2
`second_reviewer`'s — `rev2bot`, which `reviews_at`'s own docstring says posts
independently of ordinal 1 on every repo that has one). That narrowing
matters both ways: reading "any review at all" as done would mark a job
finished when only `rev2bot`, or an owner's comment, had posted and ordinal 1's
review had never run; reading only ordinal 1 as done would mark a two-seat
repo finished with ordinal 2's review still missing. Required seats come
from `_required_ordinals`, `bin/fleet-models`' own `active_ordinals()` read
off this worker's `con` — no subprocess, so no risk of resolving a different
`fleet.db` than this worker's `--db`. With nothing left pending, the job is
`done`, no spend — whether this is the first time it was ever claimed or a
reclaimed, abandoned one recovering from a crash between `post-review.py`
publishing a review and a worker recording it.

That same reconciliation runs a SECOND time, immediately after mopsus's
review-retry slot is actually taken and immediately before the dispatch call
below — not merely before and after the whole claim, which is too early and
too late to close the gap a caller who ALSO takes that slot (mopsus's own
`kind="session"` retry) could otherwise slip through between this worker's
first read and its own turn at the lock. And it runs a THIRD time immediately
AFTER a dispatch that exits 0: an exit code is never read as a verdict (only
a review at the head is), which is the exact failure measured on `epeius#1`,
2026-09-07 — three rounds reported as APPROVE from terminal output while the
PR carried zero reviews, because `--post` had been left off a different run.

The actual spend goes through `orchestrator.dispatch_review()` — the SAME
function, SAME identity, SAME `--no-followups` and `FLEET_REVIEW_CONTRACT`
the orchestrator's own review loop uses — never a second, hand-rolled
subprocess call that could drift from it. That call runs every configured
seat in one round, and talos's own per-ordinal idempotence
(`standing_ordinals()`, `dispatch-review.py:4391-4443`, used at `main()`'s
`:5121-5162`) skips whichever ordinal already has a standing review, so a
round that completes a PARTIAL prior one (ordinal 1 posted, ordinal 2 did
not) spends on the missing seat only, never re-posts the standing one. That
property is cited against the real source above; the only place it is
EXERCISED is `tests/test_plan_review_queue.py`'s fake dispatcher
(`FAKE_DISPATCHER`'s `post_accounts` mode), which hand-codes the same skip —
proof the port agrees with the citation, not that the real dispatcher was
run and observed to agree with itself.

### Quota and other stops

Talos's `review_attempt` table already distinguishes the outcomes mopsus's
own retry policy owns — `quota` (ADR-0016), `no-verdict` (ADR-0021), `budget`
(plan 0029) — from everything else, per ordinal (an `ordinal` column on each
row). Before spending, and again after a failed spend, this worker checks
each PENDING ordinal's own most recent `review_attempt` row at the exact
head — never the single newest row across every ordinal, which can belong to
the other seat and say nothing about this one's stop. If any pending
ordinal's outcome is one mopsus owns, the job is held with a reason naming
that ordinal and is **never redispatched automatically by this worker again
at that head** — redispatching while mopsus is already retrying the same
seat's stop would pay twice for one. A stop in none of those three outcomes
is an "unsupported stop": reported with the dispatcher's own actionable
detail text, retried up to `MAX_DISPATCH_ATTEMPTS` (3) times by this worker,
then `failed`.

## Reading the queue by state group

A human or a script asking "what is this queue doing right now" reads
`fleet-plan-review status`'s `state` column against these four groups —
the same shapes `docs/depsd.md`'s reconciliation section and
`tests/test_plan_review_webhook.py` use:

| group | states | meaning |
|---|---|---|
| **waiting** | `queued` | filed, not yet claimed by any worker pass |
| **reviewing** | `claimed` | a worker holds it now (a dispatch, if one is running, can take several minutes — `DISPATCH_TIMEOUT`, 2400s) |
| **blocked** | `held` | claimed at least once, found not ready yet (CI, the changed-file list, a busy retry slot, or a stop Mopsus's own retry policy owns) — re-read on the next sweep, with `reason` naming the exact check |
| **stopped** | `done`, `superseded`, `skipped`, `failed` | terminal for this row: reviewed, replaced by a fresher head's row, not this queue's business, or out of attempts |

## Automatic triggers (plan 0189)

Beyond a hand run of `enqueue`/`worker`, two automatic paths now feed and
drain this queue, both from `bin/fleet-depsd serve` (the resident service
plan 0181 already runs) — see `docs/depsd.md` for the wire-level detail:

- **A `pull_request` webhook delivery** persists a job for the pull
  request's current head (reading it fresh from the forge, never trusted
  from the delivery itself) through this file's own `_enqueue()` — the
  SAME function a hand-run `enqueue` calls.
- **A bounded worker pass**, on `fleet-depsd`'s own schedule, calls this
  file's own `_claim_jobs()`/`_dispatch_one()` — the SAME functions
  `fleet-plan-review worker` itself calls — plus a reconciliation sweep
  that recovers a pull request a delivery was never received for.

Neither adds a second writer, a second claim path, or a second dispatch
call: `bin/fleet-depsd` imports this file and calls straight into its
existing entry points, so the coordination analysis below (Mopsus's slot,
the standing-review re-reads, the claim fencing) covers the automatic path
exactly as it covers a hand run. What it does NOT newly cover is "Why no
launchd unit"'s own answer, next.

One gate IS new, and lives in `bin/fleet-depsd`, not here: this file's own
`_dispatch_one` never calls `bin/orchestrator`'s `review_quota_hold` — it is
the gate the orchestrator's OWN first-attempt review loop takes before its
own call to `dispatch_review()`, and that loop is always either a human's
hand-run session or a dispatched one, i.e. attended. `fleet-depsd`'s worker
wake fires on a timer and on a webhook with nobody watching, so it takes
that same gate itself, immediately after claiming a job and before
`_dispatch_one` ever reaches the forge, holding (never failing) the job
with no attempt spent when the owner's session/weekly/model gates are
closed. A hand-run `fleet-plan-review worker` still does not check it,
exactly as before — the gate exists for the unattended caller plan 0189
introduces, not for this file's own entry point.

`fleet-depsd`'s worker wake also claims strictly one job at a time and
never reclaims the SAME job twice within one pass: `_claim_jobs` orders by
`updated_at`, so a job this pass just held sinks behind every job it has
not touched yet — unless it is the only ready job there is, in which case
the very next claim in the same pass would hand it straight back with no
real time having passed. Reclaiming it immediately like that would spend
every one of `MAX_DISPATCH_ATTEMPTS` in the same few seconds for a
transient failure a later, separately-timed pass might have survived —
this wake stops after one attempt per job per pass instead, leaving
anything still ready for the next poll or wake.

## Why no launchd unit

Plan 0188 D4 required proving coordination with mopsus's retry lock and
talos's standing review checks, and said plainly: if the existing contracts
cannot prevent two live launches on the same `(repo, pr, head)`, name the
exact missing one and stop before wiring automatic dispatch. That check was
made, not assumed, and the answer is: **the contract is missing**, so this
ships as an on-demand, bounded command with no scheduler.

What DOES coordinate, checked against the actual source:

- **Mopsus's one-at-a-time slot** (`bin/mopsus`'s `_review_slot`, ADR-0016
  §4) — a fleet-wide, non-blocking `fcntl` lock at
  `lib.fleet_dir()/"locks"/"review-retry.lock"`. This worker takes the exact
  same lock, by loading `bin/mopsus` and calling its `_review_slot()`
  directly, before every dispatch, and re-checks `reviews_at()` again once
  inside it (above). So this worker and mopsus's own `kind="session"` retry
  (`_dispatch_session_retry`, called from inside that SAME slot) can never
  both spend at once.
- `dispatch-review.py`'s OWN pre-post check — `standing_ordinals()`, which
  skips an ordinal already approved and exits before posting anything if
  every configured ordinal already stands (above). Real, but a single
  unlocked read: two processes started close enough together can each read
  "nothing stands" before either has posted.
- **Reconciliation before and after spending** (above) — this worker never
  pays to review a head that already carries a live review, whichever
  process posted it.

What does NOT coordinate — three callers, none of which take mopsus's slot,
any other lock, or wait for this worker's own re-reads:

- a **hand-run** `dispatch-review.py --post` on the same PR;
- the **orchestrator's own first-attempt review loop** (`dispatch_review()`,
  called with no lock at all, for a PR it owns); and
- a **resumed orchestrator session reached through mopsus's `kind="plan"`
  retry.** `bin/mopsus`'s `_resume_review_path` takes the slot only to decide
  whether to retry and to write a `resume_request` row
  (`_write_resume_request`) — unlike the `kind="session"` case, it never
  dispatches inside that slot. The actual spend happens LATER, in a
  brand-new `orchestrator --resume` process `fleet-watch`'s `orphan_sweep`
  starts after consuming that row, and that process reaches the SAME
  unlocked first-attempt review loop named above — just one step removed
  from mopsus's own lock. This is not a hypothetical case for this worker's
  OWN jobs: a plan-filing PR is routinely itself the deliverable of a
  dispatched implementer session (this plan's own filing was), so it can
  carry the orchestrator lease `_resume_review_path` requires.

For a genuine plan-filing PR with no orchestrator lease at all, only the
hand-run case applies, and that is the exact convention
`docs/plans-need-parsing/0026-reviewer-trigger.md` already names rather than
enforces in code ("a session about to dispatch by hand checks the queue
first"). So running this worker by hand, when a human or a script decides
to, is safe today. Running it unattended on a timer is not proven safe
against any of the three races above, which is why **this file** ships no
launchd plist of its own, and why `worker` itself still takes no `--limit`
default large enough to assume unattended use.

**Plan 0189 runs it unattended anyway, from `bin/fleet-depsd serve`** — not
by closing the gap above, but by accepting it at the one scale the plan's
own goal requires: a plan-filing PR's review must survive its filing
session ending, which routinely happens while CI is still pending, i.e.
before any orchestrator lease on that PR could still be live.

**The orchestrator-lease race is NOT bounded by the filing session's own
lifetime, and is not narrow.** `orchestrator.review_loop` (`bin/orchestrator`)
calls `await_ci` and BLOCKS on it, holding its lease the entire wait — it
is a synchronous wait inside the still-live session, not a poll the session
returns between. `ci_wait_bound`'s own ceiling, `CI_WAIT_CEILING_SECS`, is
six hours, and a plan filing can go through several review rounds, each
with its own CI wait. So the orchestrator session is, if anything, MOST
likely to still hold its lease at the exact moment CI turns green — that is
precisely the condition `await_ci` is waiting to observe before the SAME
session calls `dispatch_review()` itself, with no lock at all. That is also
the exact moment `fleet-depsd`'s own reconciliation sweep or worker pass
would notice the same CI-green transition and attempt to dispatch. The
window this race is open for is not "until the session exits" — it is the
orchestrator's entire `review_loop` for that PR, which can span hours. The
hand-run race remains exactly as open as it always was — a session about to
dispatch by hand must still check `fleet-plan-review status` first — and is
now live continuously rather than only when someone runs `worker` by hand.
`fleet-depsd`'s own claim is bounded (at most three jobs per pass,
`docs/depsd.md`) specifically to keep any one pass's exposure small, not to
close it, and that bound does nothing to narrow the orchestrator-lease
window described above, which is a property of the orchestrator's own loop,
not of how many jobs `fleet-depsd` claims.

**For whoever actually closes the gap:** either the convention above needs
to become a code contract (`dispatch-review.py` checking `reviews_at()`
itself under a lock keyed by `(repo, pr, head)` rather than Mopsus's single
global slot, and a resumed orchestrator session doing the same before it
ever reaches its own review loop), or the race needs to be shown not to
matter in practice. Until then, this is a documented, accepted residual
risk, not a solved one — this paragraph is where a future change updates
the record.

## Reading the queue

```
fleet-plan-review status --repo operator/loyalty
operator/loyalty#81     9efb1f41   held        attempts=0  CI is still running on 9efb1f41
```

`--json` gives the same rows as data. A `held` reason names the exact check
that is not satisfied yet ("CI is still running on …", "CI is red on …: …",
"ordinal 1: outcome=quota — mopsus's own retry policy owns this stop, not
this worker"), so the queue answers "why hasn't this been reviewed" the same
way `fleet-reviewq list` (the sibling, unbuilt, general-purpose queue in plan
0026) would.
