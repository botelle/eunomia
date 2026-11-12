# ADR-0028 — A reopened usage limit drains the reviews it stopped, a few at a time, and only the stopped reviewer's own review settles one

- **Status:** Accepted
- **Date:** 2026-10-27
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md)
  §2 (which verdict settles a stop), §3 (what counts as a reset) and §4 (how
  many retries run at once). §1, §5, §6 and §7 are unchanged.
  [ADR-0018](0018-a-review-the-review-path-could-not-run-may-be-resumed.md) §4
  (what "at most one … across both kinds" counts while a drain runs).
- **Relates:** [ADR-0021](0021-a-review-that-ended-without-a-verdict-is-retried-once.md)
  §2 and §4, and [ADR-0024](0024-no-review-requires-a-named-model.md) (the same
  reviewer is the same ordinal and account; this applies that rule to every
  held stop), [ADR-0019](0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md) (`eunomia/deps`),
  plan 0094 (the `model:<bucket>`, `session` and `weekly` gates), plans 0168,
  0169 and 0170 (the work).
- **Spec-impact:** `SPEC.md` Principle 4's third exception says "one at a time".
  It becomes "at most `review_drain_concurrency` at a time once the limit has
  reopened", in a following `docs` pull request (ADR-0014).

## Decision

**1. Only the stopped reviewer settles a held stop.** A held row at ordinal N,
whatever its outcome (`quota`, `no-verdict` or `budget`), is settled by a later
`review_attempt` at the same head and ordinal, or by a live review at that head
from ordinal N's account. (`review_attempt` records no account.) A review by the other reviewer at the same head
settles nothing. This is ADR-0021 §4's rule, applied to every held outcome.

**2. A reopened gate is a reset only while the account-wide gates are open.** A
`quota_transition` that opens the stopped model's `model:<bucket>` gate,
recorded after the stop, counts as ADR-0016 §3's reset for that stop, whoever
opened it (`meter`, `probe` or the owner), provided neither the `session` nor
the `weekly` gate is closed (a gate with no row has never closed). No further probe is needed. ADR-0016 §3's probe still
decides a reset otherwise. A stop whose drained retry stopped at the limit again
is not drained again: only an answered probe resets it.

**3. A reopened gate drains, a few at a time.** Once §2's reset holds, mopsus
retries the held `quota` stops of that model that still qualify, with at most
`review_drain_concurrency` running at once. This is a `bin/fleet-config` key,
bounded 1–4. Plan-document PRs go first. A drain starts at most one retry per
repo, PR and head, and that retry settles every stop at that head. A retry that
stops at the limit again, or any of the three gates closing, starts nothing
more. Retries already running finish.

The drain's slots are additional to ADR-0016 §4's one slot, which ADR-0018 §4
and ADR-0021 §2 share. That slot still admits one retry of any other kind while
a drain runs, and the drain never takes it.

**4. A retry is skipped, not spent, when it has nothing to review.** On top of
ADR-0016 §2, a stop is skipped if its head is already contained in the base
branch, its CI is red at the head, or it is an implementation PR whose merger
account's latest `eunomia/deps` at the head is `failure`. A plan PR is never
held, because its edges gate dispatch, not review (plan 0159). A skip records no
`review-requeued` and does not count against `review_retry_limit`.

**5. One page per reopening.** Each drain sends the owner one summary, keyed to
the `quota_transition` that started it. It lists what was dispatched, what was
skipped and why, and what was not started and why.

## Why

§1: ADR-0016 §2 was written when a PR had one reviewer. With two, "a verdict at
that head" is always true after the round, because the second reviewer posts
even when the first stops. Read that way, the held queue is empty.

§2: A probe exists to tell a reset from a hope. A `model:` gate reopened by the
meter says only that one bucket is below its limit, and a review can stop on an
account-wide limit that bucket does not show. The meter does see `session` and
`weekly`, so with none of the three closed a probe adds only a request and an hour. The
last sentence bounds what an unseen limit can cost: one attempt per stop.

§3: ADR-0016 §4's "one at a time" was set for a limit that stops a handful of
reviews. When a whole week's reviews stop, one an hour does not clear the
queue before the limit is spent again. The bound stays because each retry is
spend. A retry reruns the whole round, so two retries of one head would review
it twice. The shared slot stays separate so that a drain lasting hours does not
hold up the cases that share it.

§4: A retry that cannot lead to a merge spends the limit for nothing. A skip
costs nothing, so it does not use up an attempt.
