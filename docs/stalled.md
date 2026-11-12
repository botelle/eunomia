# fleet-stalled

A pull request the fleet dispatched gets reviewed automatically —
`bin/orchestrator` calls the review dispatcher itself once CI resolves. A
pull request a human opened by hand gets nothing: no review dispatch is
watching for it, and nothing notices a branch that has gone stale against a
`main` that kept moving. `fleet-stalled` names both classes of "waiting on
nobody" once a day, and stops there — it never dispatches a review, never
rebases, never comments.

Two real cases motivated it, both found by the owner rather than by the
fleet (measured 2026-09-16 on `operator/eunomia`):

| PR | condition |
|---|---|
| **#414** | round-1 review returned `COMMENT`, every finding was fixed and pushed, and no second review was ever dispatched. One stale `COMMENT`, no approval. |
| **#415** | one line of `docs/diagrams/README.md`, red on one CI lane and green on the other — 28 commits behind a `main` that is green on both. |

## The five pull-request states, and one plan state

For every open pull request in every repository `config/repos.conf` lists:

| state | test |
|---|---|
| `unreviewed` | CI green at head, and no review at that head SHA |
| `stale-review` | a review exists, but the newest one is older than the head — a round whose fixes were never re-reviewed |
| `stale-base` | CI red at head, the merge-base has fallen behind the base branch's current tip, and that tip's own CI is green |
| `red` | CI red at head against a current base — genuinely failing |
| `held` | an implementation PR whose only failing context is `eunomia/deps`, because its plan has an unmet `depends_on` |

### `held`, and precedence (plan 0170)

`classify_pr` decides in this order; the first match wins:

1. `red` / `stale-base` / the existing logic, when a context **other than**
   `eunomia/deps` is `failure` or `error`. Contexts are read from the combined
   endpoint's de-duplicated `statuses`, so a lane that failed and was re-run
   green is not red.
2. `held`, when `pr_hold` (`bin/fleet-deps`) says so: the latest `eunomia/deps`
   status from the agent-merger account is `failure` and its description is
   the unmet-dependency form (`plan <id> waits on <dep>: …`). An unreadable
   status list is a trouble, like any other unreadable read.
3. Otherwise the usual classification with `eunomia/deps` taken out. A
   `eunomia/deps` `failure` that is not an unmet dependency (bad `Plan:` id,
   plan not on main or not ready, a lane's meeting failure) is a fault the
   owner must fix and stays `red`. With `eunomia/deps` `success` or `pending`
   and no other context, the PR is judged on review alone, so an unreviewed
   one still pages.

`held` is printed in the report but **never paged**, does not use up the
`(repo, PR, head)` page key, and does not by itself make the exit code 1:
nothing is waiting on a person. When the hold clears on the same head the PR
is classified normally and pages once.

A **plan PR is never `held`**: its `depends_on` gates dispatch, not review, so
`fleet-deps` posts `success` on it, and a plan PR waiting for a review is a
real `unreviewed` stall. A prose `HOLD` in a PR title is not read; the fleet
has no reliable way to parse one (plan 0152 was abandoned).

A pull request whose newest review IS at the head SHA, or whose CI has not
yet resolved (pending, or no statuses posted yet), is not reported at all —
there is nothing stalled about it.

`stale-base` is never inferred from a commit count. A branch can be fifty
commits behind and failing on its own merits, and reading that as "stale"
sends someone to rebase a change that is genuinely broken. The test is
measured, not assumed: red at head, **and** the base has actually moved
(`merge_base != base.sha`), **and** the base's own current tip is green.
Where the base's status cannot be read, the state is `red` — the safe
direction, because guessing `stale-base` would send someone to rebase a
branch that might not need it.

A review at an older SHA does not count as a review. `#414` had one — round
1's `COMMENT`, still sitting there after the fixes it asked for were pushed —
and reading it as "reviewed" is exactly the failure this tool exists to
catch, the same shape as the ignition gate's rule that an approval belongs to
the tree it was given for.

## What it does not do

`config/stalled.conf` sets a per-repo mode: `off`, `report` (the default —
name a stalled pull request and page once, act never), or `dispatch` —
named, and refused by name (`not built yet`) rather than silently treated as
`report`. Building `dispatch` needs a record of what the owner actually
chose for each reported PR first (Handoff, below); this tool exists to start
that record, not to guess ahead of it.

It never rebases, pushes, or closes a branch. Another session may hold a
worktree on that branch, and rebasing under a live session is the collision
worktrees exist to prevent (ADR-0005) — `stale-base` is a report, and the fix
is a person running the rebase where they can see what else is going on.

A pull request whose head branch is covered by a live (assigned or active,
non-orphaned) branch lease is skipped outright: the orchestrator is still
stewarding it, and reporting it here would just teach the operator to ignore
the report.

## `failed-plan`: a plan, not a pull request (plan 0194)

The five states above are each about ONE open pull request. `failed-plan` is
about a PLAN, and exists because a dispatch can fail before a pull request is
ever opened — nothing a pull-request scan can see. Measured 2026-11-03 on
`operator/eunomia`: seven `status: ready` plans whose last dispatch had already
failed (eunomia 0169/0179/0184, hosttools 0014/0018, lynceus 0046, talos
0040) were found only because the owner asked, not because anything paged.

A plan is `failed-plan` when ALL of these hold, read fresh every run (never
cached across runs — a merge, an abandonment, or a new dispatch always clears
it on the very next cycle):

1. its front matter on **main** (read through `bin/fleet-watch`'s own
   `plan_files`, never a local checkout — a shared checkout's branch is
   whatever the last session left) says `status: ready`;
2. the newest row for `(repo, plan_id)` in `fleet.db`'s `dispatch` view
   (eunomia plan 0036, by `current_phase_ts` — one row per lease, so a
   retried plan has several and the freshest wins) is `terminal` with an
   `outcome` other than `merged`. `outcome = 'closed'` counts: a closed pull
   request that never re-armed the plan settles nothing, and the plan still
   reads `ready` (hosttools 0014's exact shape);
3. no dispatch lease for that plan is **alive** — `bin/fleet-watch`'s
   `dispatch_leases()` for the identity, `fleetlib.is_orphaned` for the
   verdict, **never** the lease's `state` field. The 2026-11-03 plan-0179
   deadlock was exactly a dead run's `active` lease read as running for 14
   hours; reading the state field here would reintroduce that bug one layer
   down. (`operator/lynceus`'s own reads of this same `dispatch` view —
   `api/lynceus/sources.py` `leases()` and `api/lynceus/dispatches.py`, as of
   commit `0bc5384` — do not apply this verdict either: `leases()` reports
   "overdue" from a fixed age on `created`, a display heuristic, and
   `dispatches.py` never reads lease state at all.)
4. and either no marked pull request is open (none exists, or the one that
   does is closed), or one is open and no live steward owns it — read through
   `bin/fleet-watch`'s own `marked_pr`, and "stewarded" through the same
   `stewarded_branches()` the five pull-request states already skip on.

The next action depends on which of (4)'s shapes applies:

| marked PR | action |
|---|---|
| none ever opened | `fleet-requeue <repo> <plan id>` |
| open, no live steward | comment `respawn` on it |
| closed (or merged without clearing `ready`) | a reviewed PR deciding the plan's status (`abandoned`, or a re-file) — `fleet-requeue` refuses this shape by name |

This tool never runs any of the three itself — detect and page, never dispatch
or comment, same as every other state here.

## Paging

One page per `(repo, PR, head SHA)` — a new push is a new head and pages
again; a cycle over an unchanged head is silent, the same discipline
`fleet-orphans` and `mopsus` both hold for the reason `check_pins` first
learned: a channel that repeats on a condition that persists is a channel
that gets muted.

`failed-plan` pages on a **different** key and a different cadence, because
nothing about it is bound to a head SHA: `(repo, plan id, newest dispatch
lease)`, at most once per calendar day (UTC), for as long as the plan stays
`failed-plan` at all. Fifty failed plans paging hourly is how a channel gets
muted just as surely as a repeating head would; daily is the compromise that
keeps it visible without burying everything else this tool sends.

The page names the command that would act:

- `unreviewed` / `stale-review` — the review dispatcher, exactly as
  `bin/orchestrator` would invoke it (`FLEET_REVIEW_CMD`, or its own default)
  against that repo and PR.
- `stale-base` — a note to rebase by hand; there is no command, because this
  tool never rebases.
- `red` — a note to investigate; CI is failing against a base that has not
  moved, so there is nothing else to name.
- `failed-plan` — `fleet-requeue`, a `respawn` comment, or a status decision;
  see the table above.

Delivery reuses `bin/orchestrator`'s own `notify()` — the same
angelia-then-ntfy channel, and the same durable `~/dev/.fleet/pages.jsonl`
every other page on this fleet lands in. **A page is not "sent" until it is
delivered.** Every stored page entry carries the `notify()` return value as
`delivered`; dedupe now checks that flag before treating an entry as done, so
an entry left `delivered: false` (angelia down, eunomia#1253 before it had
keys installed at all) is retried on the next run instead of being silently
dropped forever — this applies to every state above, not only `failed-plan`.

## Reading the forge

Through `bin/fleetforge.py` only — `list_pulls`, `list_reviews`, and
`get_commit_status` are all this tool needs, and the `merge_base` field
Forgejo's pull-request object carries directly answers "has the base moved"
without a commit walk. `forge_timeout()` defaults to 60s: below that, this
fleet's own pull-request listings fail deterministically once a repository
has any real history (docs/forge.md) — a second, cheaper-looking default
here would silently reopen that hole.

`failed-plan` reads two more sources, both reused rather than reimplemented
so each has one definition fleet-wide:

- **`bin/fleet-watch`**, for `plan_files` (plans on main), `marked_pr` (the
  plan's work PR, if any) and `dispatch_leases` (which plans have a live
  implementation lease) — the exact functions the watcher itself dispatches
  against, loaded the same way `bin/fleet-requeue` already does.
- **`fleet.db`**, read-only, for the `dispatch` view's `(repo, plan_id)` →
  newest outcome (`FLEET_DB`, default `$EUNOMIA_FLEET_DIR/fleet.db`). A host
  whose pin has not advanced to plan 0036 yet has no `dispatch` view at all —
  a **normal** state, not a scan trouble, so `failed-plan` simply finds
  nothing there rather than paging forever about a source nobody installed
  (the same "absent, not zero" read `operator/lynceus`'s own
  `api/lynceus/dispatches.py` gives this identical view).

Nothing here dispatches, requeues, releases a lease, or comments —
`failed-plan` reads the same sources `bin/fleet-watch` writes, but only reads
them, same as every other state this tool reports.

## Handoff

The first run's count of stalled pull requests, and how many are
`stale-review` rather than `unreviewed`, decides whether `dispatch` is worth
building: the two have different fixes — one is a forgotten command, the
other is a forgotten *second* command — and which dominates the count says
which fix to automate first, if either.

Whether `stale-base` ever fires on a branch that is genuinely broken (not
just behind) is the other thing to watch. The two-signal test — red at head,
base moved, base's own tip green — is the guard against that, and it is the
claim most likely to be wrong in a way fixtures alone cannot show.

How many of the 2026-11-03 orphans (the seven no-PR failures, and the six
stewardless-HIGH pull requests) were still unsettled by the time this shipped
is the first thing to read off a live `--dry-run`, and which of `failed-plan`'s
three actions dominates says the same thing D1's five pull-request states
already said once: which forgotten step is worth automating next.
