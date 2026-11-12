# ADR-0023 — A Fable review may bill API credits, only inside a window the owner opened and only after the subscription's Fable limit is spent

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** the operator (owner/merger)
- **Amended 2026-10-26 by the owner:** §6 adds a second route, a intakebot job. A
  principal the owner granted bills credits by that grant, with no window and never
  the subscription. The owner's own intakebot jobs follow §1–§3 like a review. §1, §2
  and §5 are scoped to match. Plan 0166 builds it.
- **Relates:** `plans/0146-a-review-may-bill-credits-inside-an-owner-window.md` (the
  window and the ledger); talos plan 0029 (the reviewer's credits mode);
  [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md) (the
  retry this one shortens); `bin/orchestrator`'s `RUNNERS` comment ("never a fallback");
  `plans/0166-a-intakebot-job-may-bill-credits-by-grant.md` (§6); operator/intakebot plans 0006
  and 0007 (the jobs §6 covers).

## Context

Every Claude review today bills the operator's subscription. When the subscription's
Fable limit is spent, the `model:fable` gate closes and reviews wait for the reset
(ADR-0016). The Anthropic organisation now has monthly API credits. Those credits
are shared with speakhush's own agent use, so reviews spend them last and within a cap.

## Decision

**1. Credits are spent only while the owner has opened a credits window.** Only the
owner opens one (the guard is cooperative: it refuses a caller with no terminal, which
is not a security boundary), it lasts at most seven days from when it opened, and it ends early
when the owner closes it. When no window is open, no new credits spend starts, whatever
the gates say. A review already running finishes within its own budget. Only revoking
the key stops it sooner.
*(Amended by the owner, 2026-10-26. Previously this covered every credits spend. It now
covers reviews and the owner's own intakebot jobs. A granted principal's job (§6) needs no
window. The rest is unchanged: only the owner opens a window, and a window lasts at most
seven days.)*

**2. Inside a window, the subscription is still spent first.** A review bills credits
only while the `model:fable` gate is closed. With quota left, an open window costs
nothing.
*(Amended by the owner, 2026-10-26. Previously this applied to every credits spend.
Spending the subscription first is still the rule for reviews and for the owner's own
intakebot jobs. It does not apply to a granted principal's job (§6), which never touches the
subscription.)*

**3. Spend is capped per pull request per calendar month, and in total per calendar month, and the cap is reserved
before the run.** Each credits run is granted, and reserved, the smaller remaining cap
as its hard budget. A grant that is never settled counts at its full amount. A run that would start with no budget left does not start, and the review waits
as it does today.

**4. A credits review says so.** Its provenance names the billing, and every window
change and every spend is an event. A credits review is never reported as a
subscription review.

**5. These are the only automatic routes onto credits:** a review (§1–§4) and a intakebot
job (§6). The `RUNNERS` rule still holds everywhere else: a credential mismatch stops
the run and nothing falls back. A window and a grant are both declared, capped switches
that only the owner sets.
*(Amended by the owner, 2026-10-26. Previously this read "the only automatic route"
and named the review alone. The surviving reason is that every route onto credits is
declared, capped, and set by the owner.)*

**6. A intakebot job is billed by its principal's meter, not by the subscription's
state.** The owner's rule is that the owner's jobs use the subscription and other
users' jobs use API credits.
- A principal whose meter is `credits` bills credits only under a **grant**. Only the
  owner gives a grant (with the same terminal guard as a window), and it names its
  own monthly cap. It lasts until the owner revokes it. It needs no window, and it
  never falls back to the subscription. With no grant, or with its cap spent, the
  job does not start.
- The owner's own intakebot jobs bill as a review does: subscription first, then credits
  only inside a window and only while the model's gate is closed.
- Every intakebot job is capped per job and in total per calendar month. That total is
  a share of §3's monthly total, so intakebot jobs cannot spend the money reviews need.
  The budget is reserved before the run, as in §3.
- A intakebot credits job says so, as in §4, and names its principal.
