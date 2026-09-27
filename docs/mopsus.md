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
`TM63PG5C3F.org.eunomia.minos`. iOS opens the app when it is installed and
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
evidence, the answer is `unclassified`, not a bigger reach.

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

## What is deliberately not here

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
