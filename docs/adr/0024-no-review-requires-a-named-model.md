# ADR-0024 — A reviewer is a role, not a model; which model fills it is configuration

- **Status:** Accepted
- **Date:** 2026-10-21
- **Deciders:** the operator (owner/merger)
- **Supersedes:** the owner's ruling of 2026-08-26, "Fable on every PR, hard
  stop", wherever it still binds or is restated.
- **Amended by:** [ADR-0026](0026-any-runner-may-hold-any-position.md) §7 (2026-11-05) — §3: a repository whose reviewer seats are all the owner's needs no agent review. *(Withdrawn 2026-11-05.)*
- **Amends:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  §4 (both "different models" passages); [ADR-0019](0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md)
  §3 and §4; [ADR-0021](0021-a-review-that-ended-without-a-verdict-is-retried-once.md)
  §4 ("the same reviewer" is the same ordinal); [ADR-0003](0003-adr-track.md)
  §5 and [ADR-0004](0004-ignition-gate.md) steps 7a, 7b and 10 (named models in
  actor rows); [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md)
  (the new reviewer positions' `'default'` row); [ADR-0011](0011-a-runner-has-a-provenance-class.md)
  header.
- **Relates:** [ADR-0023](0023-a-fable-review-may-bill-api-credits-inside-an-owner-window.md),
  which permits Fable on API credits and requires nothing; `bin/fleet-models`;
  supersedes the unmerged first draft of this ADR, eunomia#1007.
- **Amended by:** [ADR-0026](0026-any-runner-may-hold-any-position.md), 2026-10-22 — §2: a reviewer need not be hosted.

## Context

The 2026-08-26 ruling put Fable on every review, and ADR-0013 §4 and ADR-0019
§3 then required the two reviewers to run different models. Both were right
when made: the review was the only check on a change besides CI, so a second
model was the only source of a second view. The fleet has since grown other
independent checks: test lanes met against the implementation (ADR-0006), a
mediator that triages the meeting (ADR-0004), and two reviewer accounts in
separate sessions. Model diversity can come from those, where configured.

The rules tied reviewer roles to models: `revbot` read as "the Fable
reviewer" and `rev2bot` as "the Opus reviewer". Fable's weekly limit is shared with
the owner's own work, so every review waited on it, and the review queue became
the merge train's bottleneck. ADR-0003 §3 argues that readings from the same
weights share blind spots; for reviews, the owner accepts that trade for
throughput, and fresh context is the independence required.

## Decision

**1. `revbot` and `rev2bot` are the accounts of reviewer ordinals 1 and 2.**
They name roles. Nothing ties an account or an ordinal to a model, and nothing
requires the two reviewers to run different models. A rule, plan, program or
default that does either is a defect against this ADR.

**2. A reviewer's only requirement is a fresh session.** Each review runs in a
session that did not author the change and is not the other reviewer's
session. Which runner fills each reviewer position, for each repository, is
configuration in `bin/fleet-models` (ADR-0013 §3); any pinned runner is
valid there, and both ordinals may name the same one. The mediator and the
testers are configuration on the same terms.

*(Amended by [ADR-0026](0026-any-runner-may-hold-any-position.md), 2026-10-22. This read "any pinned, hosted runner". A local
runner may hold a reviewer position too.)*

**3. The number of reviews a pull request needs is unchanged.** Where a
position has two reviewers, both approvals are needed, one per account
(ADR-0013 §4, ADR-0019 §3–§4).
*(Amended by [ADR-0026](0026-any-runner-may-hold-any-position.md) §7, 2026-11-05:
on a repository whose reviewer seats are all the owner's, no agent review is needed. This note was withdrawn by the owner, 2026-11-05, with ADR-0026 §7; nothing was built on it, and the text above binds as it did before.)*

**4. A review is identified by its ordinal and account, never by its model.**
Code that matches, settles, counts or refuses a review does so by ordinal or
posting account. A review still names the model that wrote it, by a pinned id
(ADR-0013 §5), as a record, not as an identity.

## Consequences

**Landing order.** Before any repository configures both reviewer ordinals with
one runner, three things land, each as its own plan:

1. `bin/mopsus` settles a stopped review by ordinal or account. It matches by
   model today, so one reviewer's review would settle the other's `no-verdict`
   stop and the pull request would wait unpaged (abandoned plan 0131's D1).
2. `bin/fleet-meet` stops refusing two reviews with one model (plan 0138).
3. Plans 0118 C3 and 0120 A3, and `docs/change-management.md` C3, drop
   "different models" as a control.

Until all three have merged, the two ordinals stay on different runners.
