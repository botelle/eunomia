# ADR-0016 — A review stopped by a usage limit may be retried by mopsus, at the same head, a bounded number of times

- **Status:** Accepted
- **Date:** 2026-09-27
- **Deciders:** the operator (owner/merger)
- **Amended by:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md) §4 — §1 no longer narrows to the verdict reviewer
- **Amended by:** [ADR-0021](0021-a-review-that-ended-without-a-verdict-is-retried-once.md) — "What is still not permitted" now also excepts a stop recorded as `no-verdict`, retried once and without §3's probe
- **Amended by:** [ADR-0018](0018-a-review-the-review-path-could-not-run-may-be-resumed.md) — "What is still not permitted" now excepts a review the review path could not run, on that path's own probe. §4's one-at-a-time slot and §7's `review_retry_limit` counter are now shared with that case; their wording is unchanged
- **Amended by:** [ADR-0027](0027-an-implementer-may-stop-on-a-question-and-an-answer-resumes-it.md) — "What is still not permitted" now excepts a resume of a plan stopped on a question, once an answer from the owner or intakebot exists, bounded by ADR-0027 §5
- **Amended by:** [ADR-0028](0028-a-reopened-usage-limit-drains-the-reviews-it-stopped.md) — §2: only the stopped reviewer's own review settles a stop, and a head already in the base, red CI, or an implementation PR whose `eunomia/deps` is `failure` is skipped; §3: a reopened `model:` gate is a reset while neither `session` nor `weekly` is closed; §4: a reopened gate drains up to `review_drain_concurrency` at a time, beside the shared slot
- **Amended by:** [ADR-0030](0030-a-review-held-by-a-closed-gate-resumes-itself.md) — §1 and §6: the orchestrator may also record the stop and write the resume request, for a review it held for a closed reviewer gate or that a reviewer's usage limit stopped, bounded by §7
- **Relates:** [ADR-0012](0012-a-transient-failure-may-release-its-own-lease.md)
  (whose "what is still not permitted" says a new case needs its own record; this
  is that record), [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  (which reviewer runs), plan 0091 (the resume dispatch the plan case needs),
  plan 0090 (the work), talos plan 0012 (the attempt record this reads).
- **Spec-impact:** `SPEC.md` (Principle 4 gains a third exception, with a
  Revisions entry; `review-requeued` joins the event list with the change that
  emits it, as `tests/test_event_type_sync.py` requires), and
  `docs/plan-dispatch.md` §"Leases, death, and respawn" (recovery is no longer
  only a human act). Both are updated by a following `docs` pull request.

## Context

A review that reaches the model's usage limit fails the same way every time until
the limit resets, and nothing retries it. A PR a session opened stays unreviewed
until someone dispatches it again. A plan whose orchestrator failed in its review
loop waits for the owner's `respawn`, which today cannot resume a plan whose PR
exists. Neither case needs judgment, only a wait.

## Decision

**1. The failure must be recorded as a usage limit.** mopsus acts only on a review
attempt that `dispatch-review.py` recorded with outcome `quota`, for the verdict
reviewer (ordinal 1). An attempt with no record, with any other outcome, or from
an annotating reviewer is not retried.

*(Amended 2026-09-28 by ADR-0013 §4. A reviewer at any ordinal now withholds the
approval if it does not produce a review, so a usage-limit stop at any ordinal
qualifies. The retry reruns the whole round at that head. "The verdict reviewer
(ordinal 1)" and "from an annotating reviewer" no longer narrow this.)*

**2. The head must be unchanged.** A retry reviews the head the stopped attempt was
meant to review. If the PR's head moved, or the PR is closed, merged, a draft, or
already carries a verdict at that head, there is nothing to retry.

*(Amended 2026-10-27 by ADR-0028 §1 and §4. "A verdict at that head" means the stopped reviewer's own: a review by the other reviewer settles nothing. A head already contained in the base branch, red CI at the head, or an implementation PR whose merger account's latest `eunomia/deps` is `failure` also leaves nothing to retry, and such a skip spends no attempt. The head rule itself is unchanged.)*

**3. Not before the limit resets.** mopsus establishes a reset with a one-line
request to the stopped attempt's model. It runs at most one such probe per model
per `review_probe_minutes`, a `bin/fleet-config` key bounded to 15–1440. Only a
probe that the model answers counts as a reset. A probe that errors in any other
way is not evidence of a reset. A retry is never the probe.

*(Amended 2026-10-27 by ADR-0028 §2. A `quota_transition` that reopens the stopped model's `model:<bucket>` gate after the stop is also a reset, while neither the `session` nor the `weekly` gate is closed, and needs no further probe. The probe still decides otherwise, an unanswered probe is still not evidence, and a stop whose drained retry stopped again waits for an answered probe.)*

**4. One at a time, and the first new stop ends the sweep.** At most one retried
review runs fleet-wide. A retry that stops at the limit again holds every other
retry until the next reset.

*(Amended 2026-10-27 by ADR-0028 §3. Once the gate has reopened, up to `review_drain_concurrency` (1–4) retries run at once, plan-document PRs first, at most one per head. They are additional to this one slot, which ADR-0018 and ADR-0021 still share. What survives: a new stop at the limit, or a gate closing again, starts nothing more.)*

**5. The same reviewer.** A retry resolves the reviewer exactly as the stopped
attempt did. Switching to another model because this one is exhausted is a
different decision, and this ADR does not make it.

**6. How a retry happens.**
- **A PR with no orchestrator.** mopsus runs `dispatch-review.py --post`.
- **A plan whose orchestrator failed in its review loop.** mopsus writes a resume
  request to `fleet.db` naming the plan, the failed lease and the head. It also
  records `review-requeued`. `fleet-watch` acts on the request, not on the event:
  it consumes the request once, only while the lease record still names the
  failed holder, and dispatches `orchestrator --resume` under the dispatch cap.
  A request older than that lease's dispatch is ignored.
- **In both cases,** mopsus never posts a comment as the owner or in place of
  the owner.

**7. Bounded and recorded.** At most `review_retry_limit` retries per repo, PR and
head. The key is bounded to 0–5, and `0` turns retries off. Each retry is recorded
before it starts, as `review-requeued`, naming the PR, the head and the attempt
number. At the limit, mopsus escalates as ADR-0012 §2 does.

**What is still not permitted.** Retrying a review that stopped for any other
reason; retrying at a new head; retrying an annotation; any retry of the
implementer's work; and acting on a log event where state can be read instead
(Principle 3). Each needs its own record.

*(Amended 2026-09-29 by ADR-0018. "Retrying a review that stopped for any other
reason" no longer covers a review the review path could not run: ADR-0018
permits resuming one, on plan 0095's review-path probe, sharing this record's
one-at-a-time slot (§4) and its `review_retry_limit` counter (§7). Everything
else in this paragraph stands, and so does the reason for it: each new case
needs its own record, which ADR-0018 is.)*

*(Amended 2026-10-11 by ADR-0021. "Retrying a review that stopped for any other
reason" no longer covers a stop recorded as `no-verdict`: ADR-0021 permits one
retry of it at the same head, with no probe, sharing §4's slot. The rule that
each new case needs its own record stands, and ADR-0021 is that record.)*

*(Amended 2026-10-26 by ADR-0027. "Any retry of the implementer's work" no longer
covers a plan stopped on a `plan-question`: once an `Answer:` from the owner's or
intakebot's uid exists, that plan may be resumed, at most `question_limit` times on
intakebot's answers (ADR-0027 §5). Everything else in this paragraph stands.)*

*(Amended 2026-11-03 by ADR-0030. "Retrying a review that stopped for any other
reason" no longer covers a review the orchestrator held because its reviewer's
quota gate was closed, or that a reviewer's usage limit stopped. The orchestrator
records that stop and writes the resume request itself (§1 and §6 above no longer
require mopsus or a recorded `quota` attempt for it), the request waits for that
gate's own reopening, and it shares §7's `review_retry_limit`. Everything else in
this paragraph stands.)*

## Consequences

`SPEC.md` Principle 4 gains a third exception. The plan case cannot run until
`fleet-watch` can dispatch `--resume`, which is plan 0091. `review_retry_limit`
and the probe are spend, because each buys a model request, so both are bounded
keys.
