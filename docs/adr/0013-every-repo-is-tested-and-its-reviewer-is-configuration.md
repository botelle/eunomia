# ADR-0013 — Every repository has a test lane, and who tests and who reviews it is per-repo configuration

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** the operator (owner/merger)
- **Supersedes:** [ADR-0006](0006-blackbox-test-lanes.md) §1 in full (the
  `extlane` consent flag and its default-off). ADR-0006 §§2-9 survive; §3 is
  amended below. The Fable-only reviewer rule of
  `plans/0065-a-reviewer-is-configurable-and-always-fable.md` (the owner's
  ruling of 2026-08-26, "Fable on every PR, hard stop").
- **Amended by:** [ADR-0026](0026-any-runner-may-hold-any-position.md) §7 (2026-11-05) — §4: a repository whose reviewer seats are all the owner's has no agent review. *(Withdrawn 2026-11-05.)*
- **Amends:** [ADR-0009](0009-a-lane-may-have-a-supervisor.md) §4 (the tester
  minimum now binds every repository); [ADR-0011](0011-a-runner-has-a-provenance-class.md)
  §2 and §4 (which cited plan 0065's rule).
- **Amended 2026-10-06 by the owner** — §4: a configured second reviewer posts its own
  verdict as `rev2bot` on every repository, critical or not.
- **Relates:** `bin/fleet-models` (the per-repo table this makes authoritative),
  `bin/fleet-repo` and `bin/fleet-lane` (the flag's writer and reader),
  `~/agent/dispatch-review.py` in `operator/talos` (the reviewer's hardcoded model).
- **Amended by:** [ADR-0019](0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md),
  2026-09-29 — §4: on an autonomous repository each of two reviewers posts its own
  verdict under its own account.
- **Amended 2026-09-28 by the owner** — §3: the ADR no longer names the default
  reviewer; §4: every configured reviewer runs before the verdict is posted, and
  a HIGH from any of them withholds the approval.
- **Amended by:** [ADR-0024](0024-no-review-requires-a-named-model.md), 2026-10-21 — §4:
  the two reviewers may run the same model; `revbot` and `rev2bot` name ordinals, not models.
- **Amended by:** [ADR-0026](0026-any-runner-may-hold-any-position.md), 2026-10-22 — §2, §3: any runner, not any ADR-0011 §2 allows.

## Context

ADR-0006 §1 made a test lane opt-in, on the grounds that sending a repository's
generated contract to a vendor is a disclosure. Three of 41 repositories ever
answered, so 38 repositories could never have an independent test lane, and
the one that declined (speakhush) did so because the vendors were third parties.
The flag was answering a question that the choice of tester already answers:
a repository whose contract must not leave for Google or OpenAI can name a
Claude tester, which reaches only the service that already holds its code.

Plan 0065 made Fable the only runner the reviewer positions accept. That rule
cannot be tested against an alternative, because there is no alternative the
fleet will run. Choosing the next reviewer needs A/B evidence, and a quota that
resets weekly means Fable is not always the reviewer available.

## Decision

**1. Every repository has a test lane. There is no consent flag.** `extlane` and
`no-extlane` are removed from `config/repos.conf` and from every reader. Which
runner holds the `tester` position is the disclosure decision, made per
repository in `bin/fleet-models`. ADR-0006 §7 is unchanged: only a
`zone: public` plan may reach a cloud backend, tester included.

**2. The `tester` position has one required slot and any number of additional
ones.** Ordinal 1 must resolve, from the repository's own row or the `'default'`
row, or dispatch refuses. The `'default'` row names `sub-opus`: the contract
then goes only to Anthropic, which already holds the code, so the default
discloses nothing new. Any runner ADR-0011 §2 allows at `tester` may replace it. *(Amended by [ADR-0026](0026-any-runner-may-hold-any-position.md), 2026-10-22: any runner.)*
Where two testers disagree, the specification was ambiguous (ADR-0006 §3),
which is the reason to configure a second.

**3. The reviewer is per-repo configuration, and Fable is the default, not the
rule.** `plan_reviewer` and `code_reviewer` ordinal 1 resolve through
`bin/fleet-models` like every other position. The `'default'` row names the
runner pinned to `claude-fable-5-1`. Any runner ADR-0011 §2 allows at a
reviewer position may be configured. *(Amended by [ADR-0026](0026-any-runner-may-hold-any-position.md), 2026-10-22: any runner;
ADR-0011 §2 is withdrawn.)* No reviewer runner may name a model by
alias: an A/B comparison needs to know which model produced each review.

*(Amended by the owner, 2026-09-28. The sentence naming the Fable runner as the
`'default'` row is withdrawn: which runner the `'default'` rows name is
configuration, set in `bin/fleet-models`, and this ADR does not fix it. The rest
of §3 stands.)*

**4. Every configured reviewer runs before the verdict is posted, and a HIGH
from any of them withholds the approval.** Ordinal 1 still posts the pull
request's single forge verdict, since every ordinal posts as the same reviewer
identity and a second approval would dismiss the first. It posts an approval
only when every enabled ordinal at that position produced a review and none of
them holds a HIGH finding. Otherwise it posts a comment, and each other
ordinal's review is posted beside it. A reviewer that did not produce a review
withholds the approval as a HIGH would.

*(Amended by the owner, 2026-09-28. It read: "Ordinal 1 gives the verdict;
additional reviewers annotate. This is ADR-0009 §4's rule, and it is what makes
an A/B arm cheap: a second reviewer runs on the same pull request, and its
review is recorded without approving." An annotation posted after an approval
arrives after the merge, so it could not act as a check. What survives: one
forge verdict per pull request, posted by ordinal 1, and a repository with one
reviewer is unaffected.)*

*(Amended by [ADR-0019](0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md),
2026-09-29, for autonomous repositories only. Two reviewers, `revbot` and `rev2bot`,
run different models in fresh sessions, and each posts its own forge verdict under its
own account. A fallback may not put both on one model. What survives: a reviewer that
did not produce a review withholds, and a single verdict per account.)*

*(Amended by the owner, 2026-10-06, for every repository. Where a position has a
second configured reviewer, that reviewer posts its own forge verdict as `rev2bot`,
critical or not, and everything it writes on the pull request comes from that
account. One reviewer's HIGH no longer withholds the other's approval; it blocks
as that reviewer's own non-approval. A critical repository never gets less review
than an autonomous one: there the owner's merge is required on top of both approvals.
*(Corrected by the owner, 2026-10-07: it read "the owner's approval is required on
top of both". The owner merges, so a separate owner approval adds nothing. See
ADR-0004 as amended 2026-10-07.)*
What survives: a reviewer that did not produce a review withholds the other's
approval, two configured reviewers may not run one model, a single verdict per
account, and a repository with one reviewer is unaffected.)*

*(Amended by [ADR-0026](0026-any-runner-may-hold-any-position.md) §7, 2026-11-05: a
repository whose reviewer seats are all the owner's has no agent review, and the
owner's merge is its review. Every other critical repository is unchanged. This note was withdrawn by the owner, 2026-11-05, with ADR-0026 §7; nothing was built on it, and the text above binds as it did before.)*

*(Amended by [ADR-0024](0024-no-review-requires-a-named-model.md), 2026-10-21. Both
passages above that require different models are withdrawn: "run different models"
and "A fallback may not put both on one model" in the ADR-0019 note, and "two
configured reviewers may not run one model" in the 2026-10-06 note. `revbot`
and `rev2bot` are the accounts of ordinals 1 and 2, and either may run any configured
model, the same one included. What survives: each reviewer's own verdict under its
own account, fresh sessions, and a reviewer that did not produce a review
withholds.)*

**5. A review names the runner and model that produced it.** The
`Review-provenance:` line is written from the resolved runner, never from a
constant. A line that names a model which did not review is a false record.

**6. A checking position may share the producer's family.** No rule refuses a
tester or reviewer for being from the implementer's family. What is recorded
instead: the test pull request names the runner behind each suite, and a review
names its model (§5), so a same-family check is visible. Whether it is worse
is what the A/B evidence is for, and a refusal would prevent collecting it.

## Consequences

- `bin/fleet-repo`, `bin/fleet-lane` and `config/repos.conf` lose `extlane`;
  `fleet-lane`'s consent gate goes. Its zone, adapter, wired, settings and
  contract gates stay.
- `bin/fleet-models` loses the Fable-only refusal and gains `'default'` rows:
  `sub-opus` at `tester`, the Fable runner at `plan_reviewer` and `code_reviewer`.
  Each repository's recorded `extlane` answer (eunomia and ares consent,
  speakhush refuses) becomes explicit tester rows that mean the same thing. The ADR-0011 class check is the
  only refusal left at the reviewer positions.
- `dispatch-review.py` reads its runner from the resolved position, not `MODEL`.
- `fleet-lane` needs an adapter for a Claude runner before a repository can
  name one as its tester. A Claude tester is granted nothing, as ADR-0006 §6
  requires of every tester.
- speakhush's ADR-0012 answered a flag that no longer exists. Its reasoning
  now applies to its choice of tester.
