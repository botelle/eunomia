# mopsus

A failed dispatch already carries everything a brief needs — see the
`dispatch` view in `docs/fleet-db.md`. When a plan fails, `fleet-watch`'s
orchestrator emits `plan-failed`, pages, deliberately does not release its
lease, and stops: surface once, never respawn. Nothing in that path *decides*
anything. `mopsus` closes the gap between "a page fired" and "I know what to
do about it" — for a cost of one command, not an investigation.

Named for the Argo's seer, who read the signs and told the crew what they
meant, and — the owner's version — because it mops up after the processes
that leaked.

## What it does, and does not, do

Under `escalate`, `mopsus` never acts. No `respawn` comment, no push, no lease
release, no retry. Under `mechanical` (the default since plan 0078) it does one
thing more, described in "Requeue" below. It reads `fleet.db`, the lease record and a run log, composes a brief,
and pages a **handle** — a short id, safe for a phone notification, that
names a stored brief. Recovery is still the human `respawn` comment
`docs/plan-dispatch.md` already documents; mopsus just makes the decision
cheap by putting the plan, the failure and the run log one command away.

```
mopsus                 sweep fleet.db for newly-failed leases, page a handle
                       for each one not already paged
mopsus <handle>        open a session already holding that failure's brief
mopsus list            list every handle recorded, repo/plan id read from
                       that handle's own brief
mopsus <handle> --format json
                       the failure as data: class, ordered actions, PR, links
```

The sweep is meant to run on a timer (`launchd/org.eunomia.mopsus.plist`,
every 5 minutes) — its own unit, never inside `fleet-watch`. The watcher is
the fleet's dispatch thread; composing a brief means reading a log file and a
database, and effort like that does not belong on the thread whose whole
value is that it checks and moves on.

## Modes (`config/mopsus.conf`)

One repo per line: `<owner/repo|default> = <mode>`.

| mode | behaviour | this plan |
|---|---|---|
| `escalate` | compose a brief, page a handle, act never | **built** |
| `mechanical` | classify by failure reason, requeue known-transient classes, escalate the rest | **built** (plan 0078) |
| `triage` | a model reads the failure and chooses | named, not built |
| `off` | nothing | built (trivially) |

`config/mopsus.conf` now says `default = mechanical` (the owner's decision,
2026-09-24); a conf with no `default` line still resolves to `escalate`.
`triage` is not built. Setting a repo to it gets you
a refusal — `mopsus: <repo> <plan> failed under mode 'triage' — not built
yet, refusing rather than treating it as 'escalate'` — never a silent
fallback to `escalate`.

## Requeue (`mechanical`, ADR-0012)

For each newly failed lease, `mechanical` **requeues** — emits `plan-requeued`
(`plan`, `lease`, `class`, `attempt`, `limit`) as `EUNOMIA_SESSION=mopsus`,
then runs `bin/fleet-release <lease> --force` under the same identity — when
all hold:

- the class is in `RETRYABLE_CLASSES` in `bin/mopsus` (today `transport` — the
  forge was never reached — and `backend-before-work` — the model backend failed
  before any commit existed; either way no work began);
- the plan carries no marked PR, in any state, and the check could be made
  (ADR-0012 §1, amended). `row["pr"]` from `fleet.db` is checked first — no
  forge read needed when the dispatch view already names one; otherwise
  mopsus asks fleet-watch's own dedupe scan, `marked_pr` in `bin/fleet-watch`,
  reused rather than restated so the two can never disagree about what
  counts. An unreadable forge, or the scan raising, counts as a marked PR —
  never as "no PR";
- the lease record still shows the holder that failed, with state `active`;
- the ledger holds fewer than `retry_limit` earlier `plan-requeued` events for
  that repo and plan.

The event is emitted **before** the release, not after: if `plan-requeued`
cannot be recorded, mopsus does not release the lease at all, since a release
the ledger never counted would let the same plan requeue past `retry_limit`
unnoticed. If the emit succeeds and the release then fails, the attempt still
counts — `retry_limit` is a bound to protect, not a count to keep exact, and
over-counting is the direction that stays safe.

A requeue sends **no page**; the event is the record. Otherwise the failure is
escalated exactly as under `escalate`, with one extra line after `reason:`
naming why: `requeued:   <n> of <limit> — limit reached` when the limit was
the only reason; `requeue:    skipped — marked PR #<n> exists` or `requeue:
skipped — could not read the forge for a marked PR` for the marked-PR
condition; `requeue:    skipped — plan-requeued not recorded: <stderr>` when
the emit itself failed; or, if the emit succeeded but `fleet-release` exited
non-zero, the existing `release:    fleet-release failed — <stderr>` line.
The release is never retried.

`retry_limit` is a `fleet-config` key (`fleet-config get|set retry_limit`), an
integer 0–5, default 2; `0` turns requeue off. Attempts are counted from the
ledger's `plan-requeued` events, never from mopsus's state file. mopsus never
spawns: the watcher re-dispatches the released plan under `dispatch_cap`.

## The brief

Assembled entirely from facts the fleet already recorded. The `class` section
below is the one place mopsus interprets `dispatch.failure_reason`, and it
interprets *only* that string (see [The class](#the-class-and-its-shortlist)):

```
Dispatch failure: <repo> <plan_id>
lease:      <lease>
branch:     <branch, learned from the lease record>
reason:     <dispatch.failure_reason, verbatim>
rc:         <dispatch.rc>
timed_out:  <yes|no|->
seconds:    <dispatch.seconds>
pr:         <dispatch.pr, or "->
log:        <path to the full run log>
transcript: <path to the transcript, or "-">

--- tail of run log (15 lines) ---
<the log's own last 15 lines>
```

A brief composed from a failure that carries a class then ends with a section
after the log tail (see below). A row with no class — an old brief, a fixture —
renders byte-identically to the text above, and `mopsus <handle>` opens whatever
text is stored, unchanged.

The transcript's **path** appears; its **contents** never do. Transcripts are
written 0600 and deliberately outside the fleet tree (SPEC Principle 6) —
copying one into a brief under `<fleet-dir>/mopsus/` would move it inside
that boundary and undo a decision made on purpose. The run log is already
redacted (`bin/orchestrator`'s `RunLog` passes every line through
`redact_transcript` before it is written) and may be excerpted directly.

## The class, and its shortlist

A generic *retry / session / abandon / ignore* menu is wrong more often than
right. Classified by hand from the first twenty-one briefs (2026-09-19), the
two largest classes want **opposite** answers: where the session ran and the
wrapper refused what it left, the work is probably sitting in a pull request
and `retry` rebuilds it; where the forge was never reached, `retry` is the
whole fix. So every brief carries a **class**, and the class carries an ordered
**shortlist** — the decision is "which of these two", not "what are my
options".

| class | matches (`failure_reason`) | shortlist, in order |
|---|---|---|
| `work-exists-unverified` | `no verified marked PR: …` (including *could not read … pull requests*, which is the verification read failing after a session that exited 0), `fix session left the head … nothing was pushed` | `check-branch`, `retry` |
| `transport` | `ssh: connect to host`, `Could not resolve host`, `Connection refused/timed out/reset` — the forge was never reached | `retry`, `check-forge` |
| `backend-before-work` | `session ended on a backend error before any work: API <status> …`, unless the status is 429 or the message names a spend or usage limit | `retry` |
| `quota` | the same form with status 429, or a message naming a spend or usage limit | `wait-for-reset`, `retry` |
| `backend-after-work` | `session ended on a backend error after work began: API <status> …` (also what a failed branch check yields — "could not tell" counts as work) | `check-branch`, `retry` |
| `timeout` | `timed out after N min` | `retry-longer-bound`, `split-plan` |
| `plan-text-refused` | `plan text carries credential shapes` | `edit-plan` |
| `branch-collision` | `could not create branch … already exists` | `clear-stale-branch`, `retry` |
| `host-missing-tool` | `needs '<tool>', which is not on PATH` | `fix-host` |
| `raced-ci` | `CI is still running` | `wait-for-ci`, `retry` |
| `unclassified` | nothing above | `read-log`, `retry` |

**`open-pr` is first wherever the lease left a PR** (`dispatch.pr`), whatever
the class. It carries the PR number and two links:
`https://minospr.app/pr/<repo>/<n>` and the forge's `…/<repo>/pulls/<n>`.

**The Minos link is a universal link, not a scheme**, and it is live — the
app-site-association at `minospr.app` claims `/pr/*` for
`TEAMID0000.org.eunomia.minos`. iOS opens the app when it is installed and
follows the redirect to the web when it is not, so **nothing here implements a
fallback**. The forge link is not that fallback; it is the direct pull request,
for a reader who wants the page rather than the app.

Each action names what it costs. The lists are **data** in `CLASS_ACTIONS`,
never code paths: mopsus still acts on nothing. **No action changes a plan's
status.** Setting a plan `abandoned` is a reviewed merge
(`docs/plan-triage.md`); a shortlist may point at the PR where that is decided
and may not offer the flip. `tests/test_mopsus_classify.py` scans every
generated list for it.

**Classification reads `failure_reason` only** — never the run log, never the
transcript (0600, deliberately outside the fleet tree). If a rule needs richer
evidence, the answer is `unclassified`, not a bigger reach. The one exception is
`review-quota` (see "Retrying a review" below): when a `failure_reason` matches
no `CLASS_RULES` entry and starts `round N: review dispatch failed:`, a second
stage checks the held review-retry queue for the plan's marked PR — never the
run log or the transcript either, only `review_attempt` rows and a forge read
— and reclassifies to `review-quota` when one is eligible. `classify()` itself
stays exactly as pure as this section always said: it still reads
`failure_reason` alone, and the second stage is a separate function `sweep`
calls after it.

**A reason matching no rule is `unclassified`, and the brief prints it raw.**
Rules come from twenty-odd failures; the next batch will contain something new,
and a confident wrong shortlist at 3am is worse than an honest "no rule fits".
Real example kept deliberately unclassified: `clone failed: … Permission denied
(publickey)` — the forge answered and said no, so it is not `transport`, and a
retry fails identically.

```
--- class: work-exists-unverified ---
1. open-pr — review or merge #81 — the work is already there; a retry would rebuild it
   minos: https://minospr.app/pr/operator/loyalty/81
   forge: http://forge.example:3000/operator/loyalty/pulls/81
2. check-branch — one look at the branch and its pull requests on the forge — …
3. retry — a whole new session that rebuilds work possibly already pushed — …
```

`mopsus <handle> --format json` prints the same thing as data (`handle`,
`repo`, `plan_id`, `lease`, `reason`, `class`, `pr`, `actions[]` with `links`
on `open-pr`) for a client to render instead of parsing prose. It looks the
failure up by handle in `fleet.db`, so it also answers for handles issued before
classes existed, and it opens no session.

## The page

One line of cause, then the handle — the two-line shape a phone notification
survives:

```
operator/ares 0001-ranged-standoff-core: implementer timed out after 90 min…
mopsus h7
```

`PAGE_MAX_CHARS` (180, in `bin/mopsus`) is a judgment call about a lock-screen
preview's budget, made once and asserted against in `tests/test_mopsus.py`
rather than eyeballed per push. The **reason** is what shrinks to fit; the
handle line is always added whole, because a page that needs scrolling to
reach the handle has lost the handle.

Delivery reuses `bin/orchestrator`'s own `notify()` / `record_page()` — the
same angelia-then-ntfy channel, and the same durable
`~/dev/.fleet/pages.jsonl` every other page on this fleet lands in. A lease is
marked paged the moment a brief is written and a page is *attempted*, whether
or not delivery actually reached a phone: idempotence does not depend on a
channel being up, or a re-page storm on an outage is exactly the "48 rows a
day" failure `fleet-orphans` already learned to avoid.

## The first run: one summary, not one page per backlog item

Idempotence (`mopsus.state.json`) only knows about a failure once mopsus has
seen it — nothing marks where "now" begins. The real first run, 2026-09-14,
swept `fleet.db`'s entire history and sent **18 pages in one burst**, one per
pre-existing failure.

The fix (plan 0050, D5): the trigger is `mopsus.state.json` being **absent**
before the sweep runs — never a row count, which would also collapse two
genuine failures landing in one ordinary cycle into a single page. On that
one first-ever sweep, mopsus still composes a brief and records a handle for
**every** newly-seen failure — nothing is skipped — but it pages **one**
summary naming the count instead of one page each:

```
mopsus: first run — 18 pre-existing failures found
mopsus list
```

Every later sweep is the ordinary path: each new failure pages on its own,
exactly as described above. The first-run case fires at most once per fleet
directory, for the life of that `mopsus.state.json`.

## Opening a handle

`mopsus <handle>` reads the stored brief and execs into
`$MOPSUS_CLAUDE_CMD` (default `claude`) with the brief text as its argument —
starting an interactive session with the brief already in view. It decides
nothing about the brief's content; that is the whole of its job.

`mopsus list` is the first-run page's pointer made real: it prints every
handle `mopsus.state.json` has recorded, with the repo and plan id read from
that handle's own brief (never re-fetched from `fleet.db`, which may have
moved on by the time someone runs `list`) — so a page that only named a count
still leaves every one of those 18 failures one command away. A handle whose
brief has gone missing is listed as such, never silently skipped.

## A usage limit (plan 0094, `docs/quota-gate.md`)

`quota` is not in `RETRYABLE_CLASSES`: a limit fails identically until it
resets. It is requeued **only through a gate condition** (D5), never
unconditionally, and the gate is `bin/fleet-quota`'s.

- **A stop closes the gate (D2).** For a `quota` failure mopsus asks which
  account the lease's own last-recorded runner spends (`_account_for_lease`,
  reading `implementer-start`'s `runner` field off `dispatch_phase` — plan
  0184 D1): a vendor account closes the WHOLE `vendor:<account>` gate instead
  of guessing Claude; everything before this plan named no runner at all, so
  it still falls back unchanged to `weekly` (the reason names a weekly limit)
  or `session`. Either way the close is `--evidence <lease id>`, *before*
  composing the brief, and `resets_at` is the reason's `resets <time> (<zone>)`
  at its first occurrence after the failure's own timestamp, else unknown.
  A `review_attempt` quota row closes its model's `model:<bucket>`, or (plan
  0184) the WHOLE `vendor:<account>` gate for a model a registered `local_cli`
  runner declares under a vendor family (`_scope_for_model`,
  `review_stop_sweep`) — never `session` or `weekly` either way.
- **Held, not forgotten (D5).** In `mechanical` mode the lease is recorded as
  `{"handle": …, "held": <scope>}`, paged once, its brief carrying `held:
  <scope> closed until <time>`, and re-read on every sweep although it is
  already in `state["leases"]`. On the first sweep that finds its gate open,
  it is requeued as above (`plan-requeued`, then `fleet-release --force`,
  bounded by `retry_limit`, no marked PR) and becomes `{"requeued": true}`. At
  the limit, or when a condition fails, it becomes an ordinary paged entry with
  the reason on a fresh brief. A gate that cannot be judged (no row) keeps the
  failure held.
- **Probing (D4).** Every closed gate that is due is probed through
  `run_probe`, detached under the review-retry lock: `model:<bucket>` with the
  stopped attempt's model, `session`/`weekly` with `QUOTA_PROBE_MODEL`
  (`claude-sonnet-5`). A `vendor:<account>` gate is never probed, plan 0184 or
  not (`_gate_probe_model` returns `None` for it, docs/quota-gate.md D1:
  "mopsus probes no vendor gate") — `run_probe` reaches a vendor account only
  through `~/agent/dispatch-review.py --probe-limit`, and that path fetches a
  Claude credential and runs the `claude` CLI regardless of what `--model`
  names, so a codex/gemini/muse model string handed to it would run under the
  REVIEWER's own Claude account, never the operator's vendor one (plan 0184
  Boundaries: never reopen one account from another account's probe or
  snapshot). A `vendor:<account>` gate opens by the meter alone
  (`fleet-quota observe`), exactly as before this plan. Due at `resets_at` + 2
  minutes, then every `review_probe_minutes`; with no `resets_at`, every
  `review_probe_minutes`. An answered probe opens the gate `by probe`, and
  only that gate — a review probe that answers also opens its own model's
  gate, never another account's. An error opens nothing.

## A broken review path (plan 0095, ADR-0018)

A review can fail before any reviewer runs: the dispatcher's own pinned
prerequisites do not run on the host. Such a failure leaves no `review_attempt`
row, so the held queue above never sees it.

- **Classified on the probe (D3).** A failure whose reason matches
  `round N: review dispatch failed:`, that `review-quota` has not claimed, gets
  `fleet-quota probe-review-path <repo>` run for its repo. Exit 75 classes the row
  `review-path`, records `{"held": "review-path:<repo>", "lease": <id>, …}` with
  the PR and head it saw, and escalates once with a `held:` line on the brief.
  The head qualifies only if its commit's committer date is no later than the
  failure (ADR-0018 §3); otherwise, or when it cannot be read, the entry is an
  ordinary paged entry whose brief says `head moved before classification`
  (or `head could not be read before classification`). A plan that also has a
  `quota` `review_attempt` row at the PR is ADR-0016's and is not claimed here.
  The gate write is the probe's own: mopsus writes no second `close`. Exit 0 or 1
  leaves the row exactly as before (`unclassified`). The text alone never
  classifies a row, and a row already recorded is not probed again.
- **Resumed on the same probe (D4).** 0094's probe loop skips `review-path:*`. On
  the first sweep that finds the gate open, in any repo mode but `off`, mopsus
  checks in order: the PR is open at the recorded head and the dispatch lease is
  orphaned; the single review-retry slot is free; `review_retry_limit` is not
  reached for the repo, PR and head. It then emits `review-requeued` (with
  `cause: review-path`) and writes the `resume_request`, which `fleet-watch`
  consumes with `resume=True`. The entry becomes `{"resumed": true}`. A forge
  or token-helper failure on that sweep (no answer, not a 404) changes nothing:
  the entry stays held and is read again on the next. If a check fails the entry becomes an ordinary paged entry with the reason on a fresh
  brief; at the limit the brief reads `requeued: N of M — limit reached`. The
  count is the same `review-requeued` count a quota retry of that PR and head
  uses. mopsus never posts `respawn` and never changes the host. ADR-0016's
  own retry (`review-probe`, the retry sweep and `_attempt_retry`) skips a row
  whose repo's `review-path` gate is closed, and takes it up once the gate
  opens.

## Retrying a review (plan 0090, ADR-0016)

A review that reaches the model's usage limit fails identically until the
limit resets. `mopsus` waits for that reset and retries — once, at the same
head, a bounded number of times — for a PR a session opened with no
orchestrator, and for a plan whose orchestrator's review loop stopped there.
See [ADR-0016](adr/0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md)
for the decision; this section is the mechanics.

**The held queue (`held_review_queue`, D1).** Every talos `review_attempt`
row (plan 0012) with `outcome='quota'` (any ordinal, ADR-0013 §4),
`outcome='no-verdict'` (ADR-0021, below) or `outcome='budget'`, for which:

- the stop is not settled by its own reviewer. The rule is the same for every
  outcome (ADR-0028 §1): a later `review_attempt` at the same repo, PR, head and
  `ordinal` (read off `rowid`, since the table is insert-only), or a live
  (`dismissed = 0`, `stale = 0`) `review` from that ordinal's account (ordinal 1
  or NULL: `reviewer`; 2: `second_reviewer`). The other reviewer's review at the
  head settles nothing;
- the PR is open, not a draft, and its forge head still equals the recorded
  `head_sha`;
- the forge shows no live review at that head from the row's own reviewer
  (`reviews_at`, which drops dismissed and stale reviews).

A forge read that fails drops the row from THIS cycle — it never admits the
row as eligible. The queue comes back oldest first, a PR carrying a
`Plan: ` marker ahead of a bare session PR.

**The reset probe (`run_probe`, D2, ADR-0016 §3).** At most once per model
per `review_probe_minutes`, mopsus runs `~/agent/dispatch-review.py
--probe-limit --model <model>` — the same dispatcher a real review uses, so
mopsus never holds a Claude credential of its own. Exit 0 records `reset`,
exit 75 records `held`, anything else (a timeout, a spawn failure) records
`error` — into a `review_probe` row (`model`, `at`, `outcome`). An `error` is
never read as a reset; ADR-0016 §3 is explicit that only an ANSWERED probe
counts.

**One retry, recorded first (`_attempt_retry`, D3).** Only a probe that comes
back `reset` triggers a retry, and only on the oldest queued row. Before
acting, mopsus emits `review-requeued` (`kind`, `repo`, `pr`, `head`, `plan`,
`lease`, `attempt`, `limit`) — if that fails, nothing runs, the same "don't
act before the record exists" rule `plan-requeued` follows above.

- **`kind = session`** (no `Plan: ` marker, or a marker with no matching dead
  lease behind it): mopsus runs `dispatch-review.py <repo> <pr> --post`.
- **`kind = plan`** (a marker AND a dead dispatch lease still naming that
  plan in fleet-watch's own lease store): mopsus writes one `resume_request`
  row (`plan`, `lease`, `pr`, `head`, `requested_at`, `consumed_at=NULL`).
  `fleet-watch`'s `orphan_sweep` is the one consumer (see below) — mopsus
  spawns nothing itself for this kind.

Both the probe and the retry run DETACHED, under one fleet-wide lock file
(`locks/review-retry.lock`) — never inside the sweep itself (ADR-0016 §4: at
most one retried review runs fleet-wide, and reviews share one subscription
with every interactive session). The lock's fd is inherited across `exec`
into a `mopsus _review-lock-run` child rather than re-acquired, so there is
no window between "confirmed free" and "the child holds it" for a second
caller (the cron sweep and `mopsus review-probe`, D7, both reach this same
path) to slip through.

**A review that ended without a verdict (plan 0127, ADR-0021).** A
`review_attempt` row with `outcome='no-verdict'` is retried once, by the same
path as above (head check, same reviewer, resume request or `--post`,
`review-requeued` first) with three differences:

- *No probe, no wait.* Nothing ran out, so `_cmd_review_lock_run` goes straight
  to `_attempt_retry`, and the sweep does not consult `review_probe_minutes`.
- *A limit of one.* The retry is counted over `review-requeued` events carrying
  `reason: "no-verdict"` for the repo, PR and head, so a usage-limit retry does not
  spend it. The limit is `min(review_retry_limit, 1)`: none when the key is `0`.
  A second `no-verdict` row at that head pages (PR, head, reviewer and both
  session ids) once, through the same `review_paged` idempotence. Until that
  second row lands the retry counts as in flight and nothing is paged, but only
  for `NO_VERDICT_INFLIGHT_MINUTES` (90) after its `review-requeued` event; a retry
  that recorded no attempt by then is a failed retry and pages. A spent, paged or
  in-flight row never holds the rows behind it.
- *Superseded only by the same reviewer.* A `no-verdict` row is settled by a
  later `review_attempt` with the same `ordinal`, or a live (`dismissed = 0`,
  `stale = 0`) `review` row from that ordinal's account (ordinal 1 or NULL: the
  `reviewer` account; 2: `second_reviewer`). The `model` plays no part, so both
  ordinals may run one model (ADR-0024). The other reviewer's review at that head is expected and settles
  nothing. Since plan 0168 this is the rule for `quota` and `budget` rows too
  (ADR-0028 §1); the old "any review at the head settles a `quota` row" rule
  is gone.

Both kinds take the same `locks/review-retry.lock`, one at a time, oldest row
first. Only `quota` rows close a model's gate (`review_stop_sweep`) or widen a
failure to `review-quota`; the orchestrator's `review dispatch failed` text is
never read.

**`fleet-watch` consumes a resume request (D4).** `orphan_sweep` treats an
unconsumed `resume_request` as a second, separately-recorded trigger for the
SAME dispatch it already runs on an owner `respawn` comment — not a new
authority, and not a change to who the human path answers to. It applies
only when the lease record still names the failed holder the request was
written against, and `requested_at` is newer than that lease's own dispatch.
Resume mode is unconditionally `True` for this trigger (the PR this cycle
already found open IS the branch on origin — nothing left to confirm), and
`consumed_at` is set only once the spawn actually succeeds.

**`review-quota`: a second classification stage, not a first-stage rule
(D5).** "Classification reads `failure_reason` only" (above) still holds for
`classify()` itself. Separately, `sweep` runs
`_apply_review_quota_reclass` over whatever `classify()` already produced: an
`unclassified` dispatch failure whose reason is the ORCHESTRATOR's own
`round N: review dispatch failed: …` (its review loop, not `classify`'s
rules) becomes `review-quota` when `held_review_queue` independently shows
this repo/PR as eligible. `review-quota` carries its own shortlist
(`wait-for-reset`, then `retry`) but is deliberately absent from
`RETRYABLE_CLASSES` — the actual retry is this section's `review_retry_sweep`,
keyed on `review_attempt`, not the dispatch-lease `try_requeue` above; this
reclassification only keeps a human reading the brief from racing that retry
by hand.

**Bounded, counted, paged (D6).** `review_retry_limit` (`fleet-config
get|set review_retry_limit`, 0–5, default 2; `0` disables the whole section)
and `review_probe_minutes` (15–1440, default 60) are `bin/fleet-config` keys,
resolved the same three-tier way `retry_limit` is. Attempts are counted from
`review-requeued` events, per (repo, PR, head) — mopsus's state file is never
the count, same as `plan-requeued`. At the limit, mopsus pages a handle whose
brief lists that head's `review_attempt` rows and `review-requeued` events
and names the PR directly — a session PR has no lease or dispatch row to
compose a brief from the way `compose_brief` does above.

**A reopened gate drains the reviews it stopped (plan 0169, ADR-0028 §2-§3).**
When `fleet-quota` opens a `model:<bucket>` gate and neither `session` nor
`weekly` is closed (a scope with no row has never closed), `review_drain_sweep`
retries every held `quota` stop of that bucket that still qualifies, once per
(repo, PR, head), with at most `review_drain_concurrency` running at once
(`fleet-config`, 1-4, default 2; `FLEET_REVIEW_DRAIN_CONCURRENCY`). The
transition is the reset, so no probe is run. `no-verdict` and `budget` rows are
not drained, and neither is a row that stopped after the transition or one whose
head an earlier drain already retried: it stopped at the limit again, and only an
answered probe on the one-row sweep resets it.

The drain is a detached `mopsus _review-drain-run` under its own
`review-drain.lock`, never `review-retry.lock`, so probes, `no-verdict` retries
and other buckets carry on. The bucket is reserved — its entry in
`mopsus.state.json` (`review_drains[<transition id>]`) set to `started` — BEFORE
the classification scan below runs, not after: that scan costs one forge read
per held row, real wall-clock time during which a one-row sweep running
concurrently must already see the bucket as draining, or it could dispatch the
very head the drain is about to. While the entry is `started`, `review_retry_sweep`
and `review-probe` leave that bucket's `quota` rows alone. Every retry runs through
`_attempt_retry`, so `review-requeued` is recorded first; a drain is always billed
to the subscription. Each result is written to the entry as it happens.

A reopening whose only skips are report-only (closed, merged, draft, head
moved, reviewed) starts no drain and pages nobody: those rows are not
actionable and will never stop recurring on their own, so paging about them
on every reopening would be noise that only grows. The transition still gets
an entry, marked `not-actionable` — the candidate set a transition can ever
drain (its scope, stopped before its own `at`) cannot change afterwards, so
without that entry every cycle would repeat the same full scan for as long as
the gate stays open. An already-started entry still records report-only
skips, since by then it has something else to report.

Each head is classified once, first match wins, when the drain BEGINS. With
bounded concurrency a row near the back of the dispatch list can wait real
time for a slot, so immediately before it uses one the drain re-verifies the
same classification — a stale `dispatch` verdict for a head the one-row sweep
has since retried, or whose PR has since moved on, is caught there rather than
reviewed a second time:

| outcome | when |
|---|---|
| `skip: closed` / `merged` / `draft` / `head moved` / `reviewed` | the held queue's own checks |
| `skip: in base` | the PR's `merge_base` equals its head |
| `skip: in flight` | an unconsumed `resume_request` younger than `RESUME_PENDING_STUCK_MINUTES` (240), or a `review-requeued` younger than 60 minutes with no attempt or own-reviewer review since |
| `skip: stuck` | a `resume_request` unconsumed for longer than `RESUME_PENDING_STUCK_MINUTES` — normally orphan_sweep consumes one within a cycle of its lease dying; this long unconsumed means a bug elsewhere (that lease never died, or orphan_sweep itself), surfaced instead of sitting invisibly `in flight` forever |
| `skip: red` | a context other than `eunomia/deps` is `failure`/`error` in the combined status, or the MERGER's own latest `eunomia/deps` status (read directly, never from the combined endpoint) is a failure for a reason `pr_hold` does not call a hold |
| `skip: held` | `fleet-deps`'s `pr_hold`: an implementation PR waiting on an unmet dependency. A plan PR is never held |
| `skip: spent` | `review_retry_limit` is already reached for the head |
| `skip: unreadable` | any read failed; never dispatched on a guess |
| `dispatch` | everything else |

A skip writes no `review-requeued` and spends no attempt. The one-row sweep and
`review-probe` apply the same `in base`, `in flight`, `red`, `held` and
`unreadable` skips — the in-flight check runs unconditionally on both paths, so
a head a drain just requeued is left alone by the one-row sweep and the probe
too, whatever kind of stop they would otherwise retry there: one retry round
settles every stop at a head (ADR-0028 §3), so a second one run concurrently
would review it twice. A `stuck` resume_request pages once from either path
(`mopsus: resume_request stuck <repo> #<pr>`), deduped the same way
`review_retry_limit`'s own page is. The `red`/`held` reading of `eunomia/deps`
only ever looks at the account `pr_hold` itself reads — the merger's own
latest status, read directly through `list_statuses`, never through the
combined endpoint. The combined endpoint shows only ONE entry per context, its
overall latest from whichever account posted most recently, so it can hide a
different write-access account's unrelated failure behind a clean merger
status (ADR-0019 §5) exactly as easily as it can hide the merger's OWN failure
behind a different account's later, unrelated success — either way the
combined entry's creator, not just its value, would have to be checked, and
only a read scoped to the merger's own postings can do that. PRs that change
only `plans/` go first.

Before each new retry the drain checks three things and starts nothing more if
any holds, marking the rest `not started`: `gate closed (<scope>)` (the bucket's
gate, `session` or `weekly`), `retry hit the limit` (one retry exited 75, the code
`dispatch-review.py` gives a round with a usage-limit stop), `new quota stop`.
Retries already running finish. The drain never opens or closes a gate. A head
this drain's OWN earlier results already dispatched (any `started`-but-not-yet-
`paged` replan of the same transition, after a dead child or a failed notify)
is excluded the same way a different transition's drained head is — "dispatched"
means an exit code or a written `resume_request`, never a path held, a
not-yet-reached dispatcher or an at-limit answer.

When it ends the drain sends one page, `mopsus: <n> reviews retried after
<scope> reopened`, where `n` counts only the results that reached the
dispatcher; results that never got that far are listed separately as
"Attempted, not dispatched". The page lists each dispatched `repo#pr` with its
result, the skips by reason, and the rows not started; a head a restart later
dispatches is dropped from the skip list, so a PR is never shown as both
skipped and dispatched. The entry becomes `paged` only if the notify
succeeded. An entry left at `started` with the drain lock free (a dead child, a
failed notify, a gate that closed) is finished by the next cycle from the
recorded results — starting a new child first if candidates remain, with no
new child only once none do. An entry whose forge reads keep failing is given
up on, and paged from whatever was recorded, after
`DRAIN_STUCK_MINUTES` (180) straight from the FIRST failing read (not from
when the entry was created — a read that starts failing long after the entry
started gets its own full 180 minutes, not whatever was left of the entry's
age); a read that succeeds again resets that clock. The same bound also
covers a persistent CHILD-side failure rather than a read failure — this
process's own reads can succeed (`plan["complete"]` true, candidates to
dispatch) while every spawned child still dies before recording even one
result, e.g. a broken fallback for the child's only token source. Past
`DRAIN_STUCK_MINUTES` since the entry was created with nothing ever recorded,
the sweep stops respawning and pages from whatever is there (nothing). One
whose `quota_transition` row is
itself gone (`fleet.db` lost and rebuilt mid-drain) is paged immediately,
since there is nothing left to read. Either way the bucket's `quota` rows are
released back to the one-row sweep and `review-probe` rather than held
indefinitely. `mopsus review-drain --dry-run` prints the classification and
order for the newest open transition and changes nothing.

**`mopsus review-probe` (D7).** `started` (it claimed the lock and kicked off
one probe, detached, ignoring `review_probe_minutes`), `busy` (the lock is
already held), or `nothing-held` (the queue is empty, or already at the
limit). It never waits for the probe or the retry that a reset may trigger —
the result is read back from `review_probe` on a later call. It scans at most
`PROBE_SCAN_LIMIT` (20) candidates of the held queue per call, so a long run
of spent, red or held rows at the front cannot turn its one forge read per
candidate into an unbounded one. Lynceus plan 0023 is the caller.

- No retry logic, no dedupe beyond "one brief per failed lease". The class
  table above is a *shortlist*, not a router: building `mechanical` still needs
  a record of what the owner actually chose for each escalated failure first
  (Handoff) — the first time the chosen action is not on the list is the
  finding that matters. `triage` stays unbuilt; with the class as data, a model
  reading a failure would choose from a shortlist rather than an open question,
  and that is the plan after this one.
- Not recorded yet, and worth recording (plan 0064 Handoff): which action the
  owner actually takes per class, and how often `unclassified` fires. Common
  means the rules are too narrow; never means they are probably too broad and
  something is being classified confidently wrong.
- No opinion on whether the run-log tail is the right excerpt for every
  failure class. It usually is; the credential-shape refusal happens before
  anything runs, so a fixed tail may be the wrong window for that class in
  particular (Handoff) — worth recording once there is enough of that class
  to judge from.

## The stuck-plan sweep (plan 0211)

A `ready` plan whose newest marked PR is merged or closed, with no fresh
owner `redispatch`, is not a *dispatch failure* — nothing crashed, and
`fleet-watch` never spawns on it (that would rebuild work already on main).
It is reported every cycle instead, through the same `plan-blocked` shape a
dependency or overlap block uses (`docs/plan-dependencies.md`), and written
to `<fleet-dir>/blocked.json`. `stuck_sweep()`, called from `main()` right
after `review_retry_sweep`, turns that into a page with a handle — the one
thing `fleet-watch` deliberately does not do for these forms, so a stuck
plan pages once with three concrete actions rather than a bare "Plan
blocked" with none.

**Input.** `blocked.json`'s `blocked` entries whose `form` is `built` or
`rejected` (`built-not-done`, `partial` or `closed-unmerged` — never
`orphan`, which is a re-plan, not a page: see `docs/plan-dependencies.md`).
The file is read, never written, by this sweep. It is treated as missing
when it is unparseable, not `v: 1`, or when `written_at` is more than two
hours old — a stale file can still list a plan the owner has since
redispatched, and cycles have been measured at ~1,230s under load (plan
0209), so two hours is several cycles, long enough that a merely slow cycle
never reads as stale. A repo whose `config/mopsus.conf` mode is `off` is
skipped, the same as every other sweep in this file.

**Idempotence.** Its own state namespace, `stuck_paged`, keyed on
`"<repo>|<plan>|<pr>"` — deliberately not the `leases` namespace above: a
stuck plan has no lease, no dispatch row and no log. A key already present
is never paged again, so a cycle that still lists the same stuck plan sends
nothing further; a NEW marked PR on the same plan is a new `pr`, hence a new
key, and pages again.

**The first run.** If `mopsus.state.json` has never carried a `stuck_paged`
key before, this sweep's first page is ONE summary — "first stuck-plan
sweep — N stuck plans found", then `mopsus list` — never one page per plan,
the same reasoning plan 0050's D5 gives for the dispatch-failure sweep's own
first run. Every plan found is still briefed and given a handle; the summary
is only how it is paged.

**The brief and its three actions.** Header, PR, form, reason and detail,
then (for `partial`) the `Remainder:` text — it usually answers the question
on its own — then a numbered shortlist, kept as data in `STUCK_ACTIONS`
rather than folded into `CLASS_ACTIONS` (`tests/test_mopsus_classify.py`
pins that table's keys to `CLASS_RULES`'s, and a stuck plan reaches neither):

1. **`redispatch`** — the owner comments the line `redispatch` on the PR
   (`docs/redispatch.md`); cost: a whole new implementation session.
2. **`mark-done`** — `mopsus <handle>`; the session that opens MUST read the
   plan in full on `main`, including its Handoff section, and the PR, before
   it may propose a plan PR setting `status: done`. If either says the plan
   stays `ready`, it proposes nothing. Cost: one reviewed plan PR.
3. **`abandon`** — the same session proposes `status: abandoned` instead;
   cost: one reviewed plan PR, and every plan depending on this one stays
   blocked until its edge is edited.

None of the three runs by itself (`docs/plan-triage.md`): `redispatch` is the
owner's own forge comment, and the other two are reviewed merges a session
only *proposes*. `mopsus <handle> --format json` answers a stuck handle the
same shape `failure_view` gives a dispatch failure — `class: "stuck-plan"`,
`lease: null`, `reason`, `pr`, and `actions` (each with its `cost`, and
`redispatch`'s with `pr` and its Minos/forge `links`) — so lynceus's existing
reader needs no second code path. `mopsus list` also lists these handles, as
`<handle>\t<repo>\t<plan>\tstuck:#<pr>`, read from the state entry rather
than a brief.
