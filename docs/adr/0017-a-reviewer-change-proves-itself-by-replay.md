# ADR-0017 — A change to how reviews are made proves itself by replay, and a deployed change that regresses is reverted

- **Status:** Accepted
- **Date:** 2026-09-28
- **Deciders:** the operator (owner/merger)
- **Relates:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  (reviewer is configuration; the next reviewer choice needs A/B evidence; this
  says what counts as evidence), [ADR-0010](0010-a-document-binds-only-if-something-holds-it-true.md)
  (what holds this true, §6), talos plan 0018 (`replay-review.py`, the instrument).

## Context

A change to how reviews are made has no measured effect today. It merges,
deploys, and is never checked again. The live review record can't supply the
check: its totals were blind to a model switch that a labelled replay showed
was about 18 real HIGHs wide (PR body). The difference is in which findings a
reviewer raises, so measuring it needs findings whose truth is already known.

## Decision

Gutting this as I don't want reply-review running as part of any process or flow.  It should be a one time analysis tool (and with dual reviews set up now, completely unnecessary).   

<!--
**1. Scope.** A *reviewer change* is any change to what produces a review of
record:
- talos `dispatch-review.py`;
- eunomia `bin/reviewer-contract.md`;
- a reviewer runner row set through `fleet-models`.

**2. The evidence is a replay.** A reviewer change is measured by
`replay-review.py` over the labelled HIGH corpus. The baseline is the version
deployed now. Live review totals (approve rate, rounds, severity counts) are
monitoring, never proof.

**3. A PR declares one of two things.** A PR carrying a reviewer change carries
either:
- a replay `compare` of the candidate against the baseline; or
- a `No review-behaviour change:` line giving the reason (a refactor, a path, a
  token fetch).

The reviewer judges whether that reason holds, and a false claim is a finding.

**4. Regressed means worse than the reviewer's own noise.** The same version
replayed twice doesn't produce the same review. So a candidate has regressed
when it is worse than the baseline by more than the spread between two
baseline runs, on either measure:
- recall of valid HIGHs at HIGH;
- false HIGHs.

A candidate within the spread is *not proven*. Neither the sign of the
difference nor a mean decides it.

**5. What happens.**
- **Regressed.** implbot opens a revert PR, and the owner merges it like any
  other. For a `fleet-models` row there is no revert PR, since the row is
  database state. The owner is paged with the command that restores the
  previous runner.
- **Not proven.** The change is flagged to the owner and stays.
- **The replay couldn't run** (harness failure, or a usage limit). That is
  *not proven*, never a pass.

**6. Held true by.** A talos CI check refuses a PR that touches a §1 file
without the §3 declaration. A scheduled replay of each newly deployed talos sha
opens the §5 revert. Until those land, only the review and the owner's merge
hold this record true.

**7. Not decided here.**
- The corpus subset size and how often baselines are re-measured belong to
  `bin/fleet-config`.
- Growing the corpus (new validated HIGHs, clean PRs for false positives) is
  plan work.  -->
