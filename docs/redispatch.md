# `redispatch` — sending a closed or merged marked PR back

Dedupe reads a marked PR in **any** state as "this plan is done"
([plan-dispatch.md](plan-dispatch.md) §Dedupe). That is right when the work
landed, and wrong when the session opened a PR for part of the work and left the
plan `ready` on purpose. `redispatch` is the owner's one-comment way to say
*again*.

## Which gesture

| the plan's marked PR | what you want | do this |
|---|---|---|
| **open**, orchestrator dead | continue on that branch | `respawn` |
| **closed or merged**, plan still `ready` | dispatch again, on a fresh branch | `redispatch` |
| any | stop | set the plan's `status: abandoned` |
| any | different work | re-file under a new id |

`respawn` continues an open PR. `redispatch` never acts on one: an open marked
PR is `respawn`'s case, and two gestures with overlapping meaning would let a
stale comment of one kind fire the other.

## The gesture

A **top-level** comment on the plan's **newest** closed or merged marked PR whose
whole line reads `redispatch` (backticks optional; `please redispatch` is
refused). It must be authored by the numeric uid in `FLEET_OPERATOR_UID`; with
that unset the gesture is disabled and the page says so.

"Newest" is the non-open marked PR with the highest number (creation order), not
the latest `closed_at`. The page and the comment check read the same PR.

## The time bound: one comment, at most one attempt

The comment counts only if it is newer than **both**:

- the PR's close time (`merged_at` for a merged PR, `closed_at` otherwise, and
  either one if the other is null), and
- the `created` of **every** lease the plan has ever had, in any state,
  including one that was assigned and then released because the start failed.

So any attempt that got a lease consumes the comment, and a successor that
launches but never activates cannot be re-spawned by the same comment: no
automatic loop. If `fleet-claim --assign` itself fails there is no lease, the
comment is not consumed, and the existing one-shot `fail-assign` page covers it.
A start that fails after a lease was assigned pages once (`fail-redispatch`) and
says the comment is used up; fix the cause and post `redispatch` again.

A missing or unparsable timestamp (the PR's close time, the comment's
`created_at`, a lease's `created`) fails closed: no redispatch, and a warn names
the field.

## Where it is honoured

- The dispatch loop: a `ready` plan with only non-open marked PRs still counts
  as done unless a fresh `redispatch` sits on the newest.
- The orphan sweep: an orphaned lease of such a plan gets a successor, assigned
  **before** the dead lease is retired.

Both apply the same three gates `respawn` does, and the dispatch loop's own:
the protection verdict is re-read, the plan is re-read from main and must be
`ready`, and its dependencies are re-checked; the cap is respected. Neither path
reads comments for a live lease.

The page for an orphaned lease whose marked PR is closed reads
`PR #N is closed — post redispatch as a top-level comment to dispatch again, or
change the plan's status`. It is sent once per lease, under its own marker.

## A fresh branch

The orchestrator refuses to create a branch that already exists on origin, and a
merged or closed PR usually leaves `feat/<slug>` behind. A redispatch's lease
names `feat/<slug>-r<N>`: the first `N` from 2 with no ref on origin and no live
lease naming it. If origin cannot be read, there is no redispatch that cycle.
A first dispatch keeps `feat/<slug>`.

## Example: infra 0014

`operator/infra` 0014 was dispatched 2026-09-24T03:50Z. The session opened
infra#174, marked `Plan: 0014-…`, titled "(plan 0014, partial)", and left the
status `ready` because the rest needed owner `sudo`. #174 merged; the lease sat
`active` with an `.orphaned` marker and nothing could dispatch the plan again.
Now: once the `sudo` step is done, comment `redispatch` on #174 and the watcher
starts a successor on `feat/<slug>-r2` and retires the dead lease.

## Not automatic

Nothing infers a redispatch from a failure class, an exit code or a
partial-looking title. That is [ADR-0012](adr/0012-a-transient-failure-may-release-its-own-lease.md)'s
domain, and it excludes anything that produced work.
