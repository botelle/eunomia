# ADR-0021 — A review that ended without a verdict may be retried by mopsus, once, at the same head

- **Status:** Accepted
- **Date:** 2026-10-11
- **Deciders:** the operator (owner/merger)
- **Amended 2026-10-11 by the owner** — §2 says what its count is, §5 says which clause of ADR-0016 §2 §4 replaces, and Spec-impact names a docs pull request (reviews 4297, 4298). A fourth note, under §4, was withdrawn the same day (reviews 4335, 4336).
- **Amends:** [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md)
  "What is still not permitted": "retrying a review that stopped for any other
  reason" no longer covers a stop recorded as `no-verdict`. ADR-0016's §4,
  §5 and §6 apply to this case unchanged, and its §2 applies except for one
  clause (§5 here). Its §3 (the reset probe) does not
  apply, and its §7 limit is narrowed to one.
- **Amended by:** [ADR-0024](0024-no-review-requires-a-named-model.md), 2026-10-21 — §4:
  "the same reviewer" is the same ordinal and account, never the same model.
- **Relates:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  §4 (any reviewer that produces no review withholds the approval),
  [ADR-0018](0018-a-review-the-review-path-could-not-run-may-be-resumed.md)
  (the other non-quota case), talos plan 0012 (the attempt record this reads).
- **Spec-impact:** `SPEC.md` Principle 4's exceptions gain this case, with a
  Revisions entry, and `docs/mopsus.md` describes it.

  *(Amended 2026-10-11. This read "both are updated by the plan that builds
  it". A `SPEC.md` change is a `docs` pull request under ADR-0014, as ADR-0016
  and ADR-0018 route theirs.)*

## Context

A reviewer sometimes ends without a `VERDICT` line. `dispatch-review.py` records
the attempt as `no-verdict`, refuses to guess, and exits non-zero. Since ADR-0013
§4, that withholds the approval even when the other reviewer approved. The
orchestrator records `review dispatch failed` and stops. ADR-0016 retries only a
`quota` stop, and ADR-0018 only a review path whose probe failed, so nothing
retries this one. The plan waits for the owner to type `respawn`.

On 2026-10-09 it happened twice in an hour (deployplane#10, eunomia#822), on two
different models. Both PRs sat for two days, until the owner respawned them.

## Decision

**1. The stop must be recorded as `no-verdict`.** mopsus acts only on a
`review_attempt` row with that outcome, at any ordinal. A reason string in a log
never qualifies.

**2. Once.** At most one retry per repo, PR and head for this case, whatever
`review_retry_limit` says, and none when that key is `0`. A second `no-verdict`
at the same head escalates as ADR-0012 §2 does. A reviewer that fails twice on
the same commit is failing for a reason a third run will not change.

*(Amended 2026-10-11. The count is this case's own: retries recorded as
`review-requeued` with reason `no-verdict` at that repo, PR and head. A
usage-limit retry at the same head does not spend it, and it does not spend
`review_retry_limit`. What is shared with ADR-0016 is the one-at-a-time slot.)*

**3. No probe, and no wait.** Nothing has run out, so ADR-0016 §3 does not apply.
The retry takes the next free slot under ADR-0016 §4.

**4. A review from another reviewer does not settle it.** The stopped attempt is
superseded only by a later attempt or review from the same reviewer at that
head. The other reviewer's review at the same head is expected, and is not
evidence that this one ran.

*(An amendment of 2026-10-11 stood here and was withdrawn the same day. It
extended this section to a usage-limit stop, which is ADR-0016's case and was not
amended there, and it said two reviewers may share one model, which ADR-0013 §4
forbids (reviews 4335, 4336). This section applies to a `no-verdict` stop only,
as first written. How a usage-limit stop at a second reviewer is settled is not
decided by this record.)*

*(Amended by [ADR-0024](0024-no-review-requires-a-named-model.md), 2026-10-21.
"The same reviewer" is the same ordinal and posting account, never the same
model. ADR-0013 §4 no longer forbids two reviewers on one model, so a match by
model would let the other reviewer's review settle this one's stop, which this
section forbids.)*

**5. Everything else is ADR-0016's.** The head must be unchanged (§2, except its
clause "or already carries a verdict at that head", which §4 here replaces:
with two reviewers the head always carries the other one's review), the same
reviewer is resolved (§5), the retry reruns the whole round, and it happens as
§6 describes. Each retry is recorded as `review-requeued` before it starts.

## Consequences

- `bin/mopsus` reads `no-verdict` rows as well as `quota` rows, with its own
  limit of one and without the model probe.
- The retry spends one more review round. That is the price of not waiting for
  a person, and the limit of one bounds it.
- Why a reviewer ended without a verdict is still a question for its
  transcript. This record makes the fleet recover; it does not explain the stop.
