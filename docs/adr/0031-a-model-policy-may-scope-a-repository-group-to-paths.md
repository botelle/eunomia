# ADR-0031 — A model policy may scope a repository group to paths, deny reading as well as changing, and take its model groups from a shared catalog

- **Status:** Accepted
- **Date:** 2026-11-03
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0026](0026-any-runner-may-hold-any-position.md) §2: repository
  groups may name paths; a deny says whether it covers reading; model groups come
  from a vendored catalog; an app may propose a policy change as a pull request;
  a deny may name one seat of a position and set a minimum model class (§8).
  ADR-0026 §1, §3–§6 and deny-only stand. [ADR-0019](0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md)
  §1 and §4: autonomy may be narrowed per path group, and mergebot neither merges
  such a PR nor a change to the policy (§6, §7 below).
- **Amended 2026-11-04 by the owner:** §2, a read deny refuses the model the
  whole repository, and no restricted view of a checkout is built.
- **Relates:** ROADMAP row 22, `config/model-policy.json`, `bin/fleet-models`,
  `bin/orchestrator`, `operator/deskapp` (the app that proposes changes),
  `operator/oracle` (the other reader of the catalog), `PROVENANCE.md` §3 (`tiers.yaml`)

## Context

ADR-0026 §2 restricts models per repository. Some repositories hold code with
different exposure in different paths: in `operator/loyalty`, the trading engine
under `core/` and the API under `api/`. A whole-repository rule must either
over-restrict the API or under-restrict the engine. The same model groups are
also wanted outside the fleet, for routing in oracle.

## Decision

**1. A repository group may name paths.** A group names repositories, or
repositories with path globs. A change deny on a path-scoped group binds work
whose declared or changed paths match it; a read deny binds all work on that
repository (§2). Work whose paths fall in more than one group is held to every
rule of every group it touches: the most restrictive wins. A
plan should be cut so each step stays inside one group; a pull request is never
split automatically.

**2. A deny says what it covers: change, or read and change.** A change deny
refuses a diff from that model touching the group's paths. A read deny refuses
that model all work on that repository, whatever paths its own work touches. A
read deny always covers change. Code that a model must not read, beside code
that stays open to it, goes in its own repository; no restricted view of one
checkout is built. A read deny decides what the fleet gives a model. It is not a
boundary against a session that reads another checkout on its host or uses a
credential that can fetch the repository: where those are kept decides that.
*(Amended by the owner, 2026-11-04. It read: "A read deny also keeps those
paths, and the history that holds them, out of anything that model is given for
that repository, whatever paths its own work touches." That asked for a view of
one checkout without the denied paths. Plan 0199 tried to build one, and each of
two review rounds found another way out of it. What survives: a read deny covers
change, and it binds all work on its repository.)*

**3. Model groups are defined in a shared catalog, not in this repository.** The
catalog names models by family, provenance class and full version, and defines
the model groups. It lives in its own repository and is vendored here, pinned by
hash, as `PROVENANCE.md` is. `bin/fleet-models` reads the vendored copy from
`config/`, so it still reads only this checkout.

**4. An app may propose a policy change as a pull request.** It writes nothing
else. The change is reviewed like any other (ADR-0026 §2).

**5. A repository group may carry a tier.** Where policy groups name a
repository's paths, that repository's `tiers.yaml` is generated from the policy
and checked against it, never edited by hand.

**6. A repository group may be critical inside an autonomous repository.** A
pull request of either kind, `src` or `tests`, touching a critical group's paths
is merged as on a critical repository (ADR-0019 §1–§2): `mergebot` does not merge
it (ADR-0019 §4, both of its merge rules). A group
cannot make a critical repository autonomous. Most restrictive wins here too.

**7. The policy and the vendored catalog enforce ADR-0019.** `config/model-policy.json`,
`config/model-catalog.json` and its pin are on the merger's list of enforcing
files (ADR-0019 §4), so `mergebot` never merges a change to them.

**8. A deny may set a minimum model class for a seat.** The catalog defines
capability classes as model groups (`class-complex`, `class-general`,
`class-focused`, `class-light`), listing models by id, so a class is reviewed
like any other group. A minimum class is a deny on the `all_except` of the
classes at or above it, so a model in no class is below every minimum. A deny may
name a position, or one seat of it by ordinal (`plan_reviewer#1`). An automatic
choice of model (plan 0193) never picks a model a deny refuses at that seat; when
no allowed model has capacity, the work waits, and is never given a lower class.
Which models an automatic tier rotates through is not policy, and changing it
cannot lift a minimum.
