# ADR-0030 — A review the orchestrator held for a closed quota gate resumes itself when that gate opens

- **Status:** Accepted
- **Date:** 2026-11-03
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md) — §1 and §6: the orchestrator, as well as mopsus, may record the stop and write the resume request, for a review it held or that a reviewer's usage limit stopped. §5, §7 and its "What is still not permitted" otherwise stand
- **Relates:** plan 0195 (the work), [ADR-0028](0028-a-reopened-usage-limit-drains-the-reviews-it-stopped.md) (what a reopened gate means), plan 0090 (`resume_request`), eunomia#1269 (the review whose HIGH this answers)

## Context

Since eunomia#1269 the orchestrator holds a review before dispatch when its
reviewer's quota gate is closed, and stops. No recovery path covers that stop:
ADR-0016 §1 needs an attempt recorded with outcome `quota`, and the held review
made none. The same gap covers a dispatch that one reviewer's usage limit failed
after the other reviewer posted. Both waited for the owner's `respawn`.

## Decision

**1. The orchestrator records the stop itself.** When it holds a review for a
closed reviewer gate, or a review dispatch fails on a usage-limit response
from a reviewer, the orchestrator:

- names that reviewer's quota scope;
- closes the scope, with the stop as evidence, if it is not already closed;
- writes one `resume_request` for its own plan, lease, PR and head, with
  reason `review-held:<scope>`;
- stops, holding the lease.

It does not wait for mopsus. The orchestrator is the mechanical layer, and it
cannot approve, merge or post as the owner, so a request it writes only decides
when the same review runs again. It never retries in its own process: the
resume is a `fleet-watch` dispatch under the cap. So ADR-0012's "any retry by
the orchestrator itself" still stands.

**2. Only that scope's reopening resumes it.** `fleet-watch` acts on a
`review-held:<scope>` request as ADR-0016 §6 describes, and defers it while
`<scope>` is closed. A reset hint, a weekly schedule or an elapsed backoff only
says when to look again. The resume needs the gate's own reopening (ADR-0028
§2).

**3. Bounded and recorded as ADR-0016 §7.** These resumes count against
`review_retry_limit` for that repo, PR and head (the counter ADR-0016, 0018
and 0021 share, where `0` turns them off). Each is recorded as
`review-requeued` before it starts. At the limit, the stop is an ordinary
failure and is paged.

**4. The same reviewer** (ADR-0016 §5). A resume reruns the review the held
attempt was for, and never switches model because the first is exhausted.

**5. One resume per stop.** A stop is keyed by repo, PR and head, whoever
writes it. While an unconsumed `resume_request` exists for that key, neither
the orchestrator nor mopsus writes a second, and mopsus skips a recorded
`quota` attempt at that head. These resumes take ADR-0016 §4's shared slot,
and once a gate has reopened, ADR-0028's drain concurrency. The dispatch cap
alone is not enough, because a retry reruns the whole round.

**6. Rechecked when it is consumed.** A request can wait for a weekly reset.
So before dispatching, `fleet-watch` rechecks the request's stored head
against ADR-0016 §2 and ADR-0028's skips, and against §3's limit. Those
conditions are a moved head, a closed, merged or draft PR, the stopped
reviewer's verdict already at that head, a head in the base, red CI, or
`eunomia/deps` failure. A request that fails the check is consumed with its
reason and spends no attempt.

## Consequences

ADR-0016's "What is still not permitted" no longer covers this case. A review
held for a closed gate is a stop that waits for a reset, which is the kind
ADR-0016 permits. Plan 0195 builds it.
