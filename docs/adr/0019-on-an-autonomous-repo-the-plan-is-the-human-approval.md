# ADR-0019 — On an autonomous repository the plan is the human approval, and code merges on two independent reviews

- **Status:** Accepted
- **Date:** 2026-09-29
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0004](0004-ignition-gate.md) — the revised rule's merge
  whitelist ("absolute") and approvals whitelist ("sufficiency"), for
  autonomous repositories; [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  §4 (one forge verdict per PR, posted by ordinal 1);
  [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md) §6 (a `tests`
  PR gets no automatic review), for autonomous repositories.
- **Amended by:** [ADR-0031](0031-a-model-policy-may-scope-a-repository-group-to-paths.md) — §1, §4: a path group may be critical inside an autonomous repository, and the model policy is an enforcing file
- **Amended by:** [ADR-0024](0024-no-review-requires-a-named-model.md) — §3's
  two reviewers need not run different models; §4's merger check names no
  model.
- **Relates:** [ADR-0002](0002-candidate-green.md) (a failing context blocks
  review; accepted here for `eunomia/deps`); `bin/fleet-watch`
  `main_is_protected()`.

## Context

The owner reviews every implementation PR after the reviewer approves it. By
the owner's count, that review has found no defect the reviewer missed
(2026-09-29). It costs the owner's time and serialises the merge train. The
plan is where the owner's judgment changes the work.

Forgejo's merge whitelist decides who can write `main`. A required commit
status blocks a merge for every account, admins included, and can turn green
without a push. But any account with write access can post a status in any
context. So a status can hold a PR, and it cannot show that a human approved
it. Both behaviours were probed on 2026-09-29; the results are in the PR body.

## Decision

**1. A repository is autonomous or critical.** The owner sets this per
repository in `fleet-config`, and each change is recorded in the ledger. The
default is critical. A repository whose merge deploys to a keystone host
stays critical until it has a versioned release that can be rolled back.
*(Amended by [ADR-0031](0031-a-model-policy-may-scope-a-repository-group-to-paths.md)
§6, 2026-11-03. What survives: the owner sets autonomy per repository, default
critical. What changes: a path group in the model policy may make part of an
autonomous repository critical; nothing makes a critical repository autonomous.)*

**2. The merge whitelist carries the human requirement.** On a critical
repository only `operator` may merge `main`. On an autonomous repository
`mergebot` may too: the owner adds it when making a repository autonomous, and
removes it when making one critical. mergebot merges only what §4 allows. Every other PR
is merged by the owner: every `plan`, `adr`, `docs` (flow specs included)
and `deploy` PR, every unplanned PR, and every PR on a critical repository. Merge is still ignition, and a second control
holds it apart from mergebot's code: fleet-watch dispatches a plan only if the
merge that last changed its plan file on `main` was made by `operator`.

**3. On an autonomous repository, an implementation PR needs two approvals
from independent reviewers.** Each posts under its own account:
`revbot` and `rev2bot`. Each runs in a fresh session. An arm that cannot
run withholds its approval, and the PR waits. `rev2bot` and `mergebot` are agent
accounts under ADR-0004.

*(Amended by ADR-0024, 2026-10-21. This section read "and the two run
different models. A usage-limit fallback may not configure one arm to the
other's model." Which model each arm runs is configuration; the two may run
the same model. Two accounts and two separate sessions stand.)*

**4. `mergebot` is a program, not a model: no model decides a merge.** It
merges a `src` PR only when all of these hold:
- the repository is autonomous;
- the PR targets `main` and carries `Plan: <id>`, naming a plan that is at
  `ready` on `main`;
- it is of ADR-0014's `src` kind, except that it may change that plan's
  `status:` line from `ready` to `done`;
- it changes nothing under `docs/adr/**`, and none of the files that
  enforce this ADR. The merger keeps that list; at minimum it names the
  merger itself, the status poster, the verdict, the role table and the
  autonomy setting, and the model policy and vendored catalog (ADR-0031 §7);
- it touches no path of a `critical` repository group in the model policy
  (ADR-0031 §6);
  *(amended by ADR-0031, 2026-11-03: the two clauses naming ADR-0031 were added;
  the rest of §4 stands)*
- both approvals of §3 stand at the head, one from each reviewer account;
  *(amended by ADR-0024, 2026-10-21; this read "the Fable reviewer and the
  Opus reviewer")*
- CI is green at the head: every required context other than
  `eunomia/deps` is `success`;
- the plan's integration tests pass at the head, including every test added
  for this plan, whether by the implementer or the test lane;
- `eunomia/deps` (§5) is `success` and was created by the poster's identity.

It merges a `tests` PR (ADR-0014, under `tests/lane/**`) when the repository is
autonomous, the PR carries `Tests-for: <id>` naming a plan on `main`, CI is
green, it touches no path of a `critical` repository group in the model policy
(ADR-0031 §6), and both reviewers of §3 have approved it at the head.
*(Amended by ADR-0031, 2026-11-03: the critical-group clause was added.)* Each of those
reviews is dispatched without shell or write tools, so no reviewer session
executes vendor-written files.

mergebot never merges a PR it authored or reviewed, and never schedules an
auto-merge. implbot never merges.

**5. `eunomia/deps` is the dependency hold.** A status poster posts it on each
open PR's head. It runs as `mergebot`, an identity no implementer or reviewer
holds:
- `failure` when the PR carries `Plan: <id>` and that plan is not on `main`
  at `ready` or `done`, or its `depends_on` is unmet;
- `pending` when the poster cannot decide, and it pages;
- `success` otherwise.

The review dispatcher treats it like any other context: a PR held on a
dependency is not reviewed until the hold clears. Dispatch already waits on
`depends_on`, so this holds only a PR whose dependency regressed, and a
review of that head would be spent on code about to change (owner,
2026-09-29).

## Consequences

- **ADR-0004's merge whitelist admits one agent, mergebot.** On an autonomous
  repository, two agents may reach `required_approvals`, which must be at
  least 2. The verdict decides from the rule alone, because the broker on
  vaulthost cannot read `fleet-config`: it vouches for such a rule only if the
  rule also requires `eunomia/deps`. The broker's agent-account set must
  include `rev2bot` and `mergebot`. The human requirement moves from the approval
  count to the merge whitelist.
- **On an autonomous repository, `tests` PRs merge without the owner.** Their
  vendor-written files still execute in CI, which is where tests are meant
  to run. ADR-0014 §6's no-shell rule protects the reviewer's session, and
  §4 keeps it. ADR-0006 §5's human reader is replaced there by the two
  no-tool reviews.
- **Landing order.** `mergebot` may be added to a rule only after the verdict
  counts it as an agent, and after fleet-watch's merged-by check exists.
  Before that, the verdict would vouch for an account it does not know.
- **The preventive controls are not the only line.** A plan that ignites
  when it should not have is visible in `fleet.db` and lynceus, and dispatch
  quotas bound what it can spend. A risk that these detective controls
  bound and expose may be accepted rather than closed.
- **A status can go stale.** When the poster is down, a head keeps its last
  status. So mergebot checks the plan's state on `main` itself, and does not
  rely on the status for it.
- **The owner creates the `rev2bot` and `mergebot` accounts.** Their tokens go in
  keyvault, each under its own AppRole. The owner also adds `eunomia/deps` to
  each repository's rule, after the poster runs.

