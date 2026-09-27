# ADR-0012 — A dispatch that failed before any work began may be released by mopsus, a bounded number of times

- **Status:** Accepted
- **Date:** 2026-09-24
- **Deciders:** the operator (owner/merger)
- **Amended 2026-09-25 by the owner**: §1's lease condition names the record,
  not orphanhood, and adds "no marked PR" (review of #558, eunomia#559)
- **Relates:** `SPEC.md` Principle 4 (this is its second named exception),
  `docs/plan-dispatch.md` §"Why a failing wrapper keeps its lease" (the loop this
  bounds), `plans/0046-mopsus-escalates-with-a-handle.md` (named `mechanical`
  and left it unbuilt), `plans/0064-a-failure-arrives-with-its-own-shortlist.md`
  (the classes), `plans/0078-mopsus-requeues-a-transient-failure.md` (the work).

## Context

A failed orchestrator keeps its lease on purpose. The watcher skips a plan only
while a dispatch lease is live, so releasing on failure re-dispatches every
cycle, which was measured as three leases, three spawns and three pages in three
cycles. Recovery has therefore been a human act: a `respawn` comment on a marked
PR, or `fleet-release --force` when there is no PR.

For a failure that happened before any work began (the forge unreachable, the
model backend erroring in the first seconds), the human act is always the same,
so a person adds no judgment, only delay.

## Decision

**1. `bin/mopsus` in `mechanical` mode may release a failed dispatch lease**
through `bin/fleet-release --force`, under its own session identity, and only
when all of these hold:

- the failure's class is on mopsus's **retryable** list. A class qualifies only
  if its evidence shows that no session produced work (no branch pushed, no PR
  opened). Nothing that ran to a verdict qualifies.
- the lease record still names the holder that failed, in state `active`: not
  released, not taken over, not reassigned to a successor;
- the plan has no marked PR in any state, and the check could be made. An
  unreadable forge counts as a PR;
- the number of earlier automated releases for that repo and plan is below
  `retry_limit`, a `bin/fleet-config` key.

*(Amended by the owner, 2026-09-25. The lease condition used to read "is not
orphaned or taken over". A failed orchestrator stops heartbeating, so its
lease always goes orphaned within its TTL, and that wording could never have
held. What it meant to exclude, a lease someone else now holds, is kept. The
marked-PR condition is new: a respawned successor that fails at the forge
would otherwise be released onto a plan that dedupe will never dispatch
again, and nobody would be paged.)*

**2. The loop is bounded by `retry_limit`, not by a person.** At the limit,
mopsus escalates exactly as `escalate` mode does, and the brief names the
attempts. `retry_limit = 0` turns automated releases off.

**3. Every automated release is recorded** as its own event naming the class and
the attempt number. A release that no event explains is a defect.

**4. A release is not a dispatch.** mopsus never spawns. The watcher
re-dispatches under the dispatch cap like any other plan.

**What is still not permitted.** Takeover of a *live* lease; releasing any lease
whose failure could have left work behind; any retry by the orchestrator itself;
and anything mopsus's `triage` mode would decide. Each needs its own record.

## Consequences

Principle 4 gains a second exception, named in `SPEC.md` beside the pin
exception. `retry_limit` is spend: each release can buy one more session, so the
key is validated and bounded like `dispatch_cap`.
