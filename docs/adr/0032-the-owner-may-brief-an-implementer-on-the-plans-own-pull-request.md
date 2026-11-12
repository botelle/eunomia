# ADR-0032 — The owner may brief a plan's implementer on the plan's own pull request, and the brief never outranks the plan

- **Status:** Accepted
- **Date:** 2026-11-05
- **Deciders:** the operator (owner/merger)
- **Relates:** [ADR-0027](0027-an-implementer-may-stop-on-a-question-and-an-answer-resumes-it.md)
  (an owner answer reaches a session without review; this is the same channel
  used before the run instead of after a question),
  [ADR-0019](0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md)
  §2 (the plan pull requests the human merged), plan 0147 (owner guidance on
  the implementation pull request reaches a fix round), plan 0206 (eunomia
  builds this), talos plan 0051 (the reviewers see it).
- **Spec-impact:** `bin/orchestrator`'s `IMPLEMENTER_TEMPLATE` says the
  merged plan is the session's only authorisation; owner hints are now a
  second input that informs that authorisation without adding to it.
  `docs/plan-dispatch.md` gains a section on hints (plan 0206).

## Context

The owner often wants to give an implementer a hint, such as a file to look at
or a pitfall to avoid, after the plan pull request is approved. An approved
branch is frozen, and changing the plan text means a re-review. Today the only
owner text that reaches an implementer is an `Answer:` (ADR-0027), which
needs a question first, and fix-round guidance (plan 0147), which needs the
implementation pull request to exist.

## Decision

**1. A hint is an owner comment on a plan pull request.** A plan pull request
is any merged pull request that changed the plan file on `main`: the one that
filed it and any that amended it. Before the merge it is simply the open plan
pull request. A hint is a comment on one of these whose first line starts
with `Hint:` and whose author is the owner, bound by the numeric Forgejo user
id `FLEET_OPERATOR_UID`, never a login. No other account can post a hint.

**2. Every implementer session gets every hint, verbatim.** That includes a
session started to resume a stopped run, because a resumed run is a new
session that remembers nothing. The hints go after the plan, in their own
delimited block, oldest first. Hints are read until the implementation pull
request opens. After that, guidance goes on the implementation pull request
(plan 0147).

**3. A hint never outranks the plan.** Where a hint conflicts with the plan,
its ADRs or its flow spec, those win. The implementer says so in the
pull request and does not follow the hint. A hint cannot widen the plan's
`paths`, `depends_on`, zone, tier or boundaries. Anything larger is a new plan.

**4. The reviewers see exactly the hints the implementer was given.** Each
hint given to a session is acked on its thread, with a hash of the body that
was given. A review of the implementation pull request shows only acked hints,
as the owner's statements. A hint whose current body no longer matches the
acked hash is shown with that fact stated. A change made on a hint is checked
against the hint and is not reported as unexplained.

**5. What the implementer was told is recorded.** Every hint given to a
session is logged with its comment id and body hash.

## What is still not permitted

A hint is not an approval, a merge, a `respawn` or a dispatch. It changes no
review bar and no seat. It does not make a plan pull request mergeable sooner,
and no account but the owner's can write one.
