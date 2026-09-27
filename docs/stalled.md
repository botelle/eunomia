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

## The four states

For every open pull request in every repository `config/repos.conf` lists:

| state | test |
|---|---|
| `unreviewed` | CI green at head, and no review at that head SHA |
| `stale-review` | a review exists, but the newest one is older than the head — a round whose fixes were never re-reviewed |
| `stale-base` | CI red at head, the merge-base has fallen behind the base branch's current tip, and that tip's own CI is green |
| `red` | CI red at head against a current base — genuinely failing |

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

## Paging

One page per `(repo, PR, head SHA)` — a new push is a new head and pages
again; a cycle over an unchanged head is silent, the same discipline
`fleet-orphans` and `mopsus` both hold for the reason `check_pins` first
learned: a channel that repeats on a condition that persists is a channel
that gets muted.

The page names the command that would act:

- `unreviewed` / `stale-review` — the review dispatcher, exactly as
  `bin/orchestrator` would invoke it (`FLEET_REVIEW_CMD`, or its own default)
  against that repo and PR.
- `stale-base` — a note to rebase by hand; there is no command, because this
  tool never rebases.
- `red` — a note to investigate; CI is failing against a base that has not
  moved, so there is nothing else to name.

Delivery reuses `bin/orchestrator`'s own `notify()` — the same
angelia-then-ntfy channel, and the same durable `~/dev/.fleet/pages.jsonl`
every other page on this fleet lands in.

## Reading the forge

Through `bin/fleetforge.py` only — `list_pulls`, `list_reviews`, and
`get_commit_status` are all this tool needs, and the `merge_base` field
Forgejo's pull-request object carries directly answers "has the base moved"
without a commit walk. `forge_timeout()` defaults to 60s: below that, this
fleet's own pull-request listings fail deterministically once a repository
has any real history (docs/forge.md) — a second, cheaper-looking default
here would silently reopen that hole.

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
