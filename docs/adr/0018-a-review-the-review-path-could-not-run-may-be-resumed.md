# ADR-0018 — A review the review path could not run may be resumed by mopsus, once the path's own probe passes

- **Status:** Accepted
- **Date:** 2026-09-29
- **Deciders:** the operator (owner/merger)
- **Amended 2026-09-29 by the owner** — §3 names where the head comes from; §1 says which record governs a plan that qualifies under both; Spec-impact and Consequences place the `SPEC.md` edit in a `docs` pull request (ADR-0014 §3)
- **Amended by:** [ADR-0028](0028-a-reopened-usage-limit-drains-the-reviews-it-stopped.md) — §4: a usage-limit drain's retries run beside this slot and are not counted in it
- **Amends:** [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md)
  "What is still not permitted", which now excepts the case this record
  decides. §§1–7 of ADR-0016 are unchanged.
- **Relates:** plan 0095 (the probe, the hold and the work),
  [ADR-0012](0012-a-transient-failure-may-release-its-own-lease.md) §2
  (escalation at the limit), plan 0091 (`orchestrator --resume`).
- **Spec-impact:** `SPEC.md` Principle 4 gains a fourth exception, with a
  Revisions entry, in a `docs` pull request after the plan (ADR-0014 §3).
  *(Amended 2026-09-29: this said the edit lands with plan 0095's
  implementation, citing plan 0094's lease on `SPEC.md`. A plan's path lease
  does not bind a `docs` pull request, and ADR-0014 §3 orders the docs link
  separately.)*

## Context

A review can fail before any reviewer runs, because the host cannot run the
dispatcher's own prerequisites: a pinned program is not executable, or a table
its strict reader needs is missing. Every such failure is identical until the
host is fixed, and none needs judgment, only the fix and a wait. ADR-0016
permits a retry only for a usage-limit stop, so today each one waits for the
owner's `respawn`.

## Decision

**1. The evidence is the probe, never the text.** mopsus acts only on a plan
whose orchestrator failed with `round N: review dispatch failed:` **and** for
whose repo plan 0095's review-path probe failed when mopsus classified it. The
reason string alone never qualifies.

*(Amended 2026-09-29. A plan that also has a `review_attempt` row with outcome
`quota` at the same PR and head is ADR-0016's, not this record's: its retry
waits on ADR-0016 §3's model probe **and**, while the repo's review-path gate
is closed, on this record's §2. Nothing is resumed on a model probe alone while
the path is known to be broken.)*

**2. Not before the probe passes.** A resume waits for the repo's review-path
probe to pass. Only that probe opens the way: not a usage-limit probe, not the
clock, and not the failure's absence from a later cycle.

**3. The head must be unchanged,** exactly as ADR-0016 §2.

*(Amended 2026-09-29. A dispatcher that could not run leaves no
`review_attempt` row, so the baseline is recorded by mopsus: when it classifies
the failure, it reads the marked PR's head from the forge and stores it with
the held entry. That head qualifies only if its commit's committer date is no
later than the failure. Otherwise the head may already be newer than the one
that failed, and nothing is resumed. A resume requires the PR's head to still
equal the stored baseline.)*

**4. One at a time, sharing ADR-0016 §4's slot.** At most one retried review runs
fleet-wide across both kinds.

*(Amended 2026-10-27 by ADR-0028 §3. A drain after a reopened usage limit runs its retries beside this slot, and they are not counted in "at most one". The slot itself, and its sharing with ADR-0016 and ADR-0021, is unchanged.)*

**5. The same reviewer,** exactly as ADR-0016 §5.

**6. How.** The plan case only. mopsus writes the same resume request as ADR-0016
§6, and `fleet-watch` consumes it the same way. mopsus never posts a comment as
the owner or in place of the owner. A PR with no orchestrator has no record of
such a failure, and is not retried.

**7. Bounded and recorded, on ADR-0016 §7's counter.** Each resume is recorded as
`review-requeued` before it starts, and counts toward the same
`review_retry_limit` per repo, PR and head. At the limit, mopsus escalates.

**What is still not permitted.** Changing the host to make the probe pass (a
mode, a schema, a pin, a setting); resuming on the reason text alone; resuming
at a new head; and any retry of the implementer's work.

## Consequences

`SPEC.md` Principle 4 names four exceptions once the `docs` pull request
lands. A broken review path now costs its
repair plus one probe interval, not one `respawn` per stranded plan. A defect
that the probe cannot see is still recovered only by the owner.
