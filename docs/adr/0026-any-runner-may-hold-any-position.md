# ADR-0026 — Any runner may hold any position; a restriction, and the open-weights box, are a reviewed policy file that only denies

- **Status:** Accepted
- **Date:** 2026-10-22
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0011](0011-a-runner-has-a-provenance-class.md) §2 (withdrawn),
  §3 (the box is per-repository configuration), §4 and its first two Consequences; [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md)
  §2 and §3 ("any runner ADR-0011 §2 allows"); [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md)
  (the new reviewer positions are `us-hosted` only);
  [ADR-0024](0024-no-review-requires-a-named-model.md) §2 ("pinned, hosted").
- **Amended 2026-10-24 by the owner** — §2: a restriction is a policy file of named
  model and repository groups, edited in a repository and never in an app; §5: every
  runner names a full model version, so an alias no longer keeps a runner out of review.
- **Amended again 2026-10-24 by the owner** (eunomia#1063) — §2: the policy only denies;
  there are no allow rules. §6: the box is set in the same policy file. The title
  follows. Consequences: `fleet-models` refuses an unpinned runner at every position.
- **Amended by:** [ADR-0031](0031-a-model-policy-may-scope-a-repository-group-to-paths.md)
  (2026-11-03) — §2: repository groups may name paths, a deny may cover reading,
  model groups come from a vendored catalog, an app may propose a change as a
  pull request, and a deny may name one seat of a position.
- **Amended 2026-11-05 by the owner** — §7: the owner may hold every reviewer seat of
  a repository. This also amends ADR-0013 §4, ADR-0024 §3 and ADR-0004's 2026-09-09
  revision, for such a repository only; each carries a dated note.
- **Withdrawn 2026-11-05 by the owner** — §7 and its notes in ADR-0004, ADR-0013
  and ADR-0024. Owner-only review is not pursued; nothing was built on it.
- **Relates:** `bin/fleet-models`, `bin/orchestrator` `RUNNERS`,
  `operator/talos` `dispatch-review.py`, `operator/lynceus` `api/lynceus/models.py`,
  `operator/epeius` (the box).

## Context

ADR-0011 §2 tied provenance classes to positions: `us-open` runners could never
review, `cn-open` runners could only implement. ADR-0014 extended that to the new
reviewer positions. These rules applied to every repository, whatever it holds.
ADR-0024 removed the rules that named a model for a seat; these are the same
kind of rule, named by class instead of by model. Which runner suits which seat
is a choice the owner makes per repository, not one the fleet should make once
for all of them.

## Decision

**1. Every runner may be configured at every position, on every repository.**
No ADR, plan, program or default restricts a model, a family, a provenance class
or a hosting arrangement to particular positions. A rule that does is a defect
against this ADR.

**2. A restriction is a policy: which models a group of repositories may use, at
which positions.** The policy is one file in a repository. It names groups of
models (by runner, family or provenance class) and groups of repositories, and
rules that deny a model group at positions for a repository group. There are no
allow rules: everything a deny does not name is allowed (§1), including a model
added after the policy was written. Within what is allowed, whoever configures a
repository's positions chooses freely, and a runner must still be able to run
there (§4). A change to the policy is a reviewed pull request, and the people who
approve it need not be the people who choose a repository's models.
`bin/fleet-models` reads the policy from the merged default branch and enforces it
when a model is set and when a runner is spawned. An app may show the policy and the
rule behind a refusal; no app edits it. None exists today, and building it is
ROADMAP row 22.
*(Amended by the owner, 2026-10-24. It read: "A restriction by provenance, if one
is ever wanted, is configuration for a named repository. It names the repository,
the classes and the positions it refuses." What survives: no restriction exists
until the owner writes one, and it binds only the repositories it names. What
changes: rules name groups rather than single repositories and classes, and the
policy is a file in a repository, not a setting in an app.)*
*(Amended again by the owner, 2026-10-24. The first amendment had rules "that allow
or deny", with a deny winning; an allow then changed nothing, so allow rules are
removed rather than given allowlist semantics. A repository that should use only
some models is written as denies of the rest.)*
*(Amended by [ADR-0031](0031-a-model-policy-may-scope-a-repository-group-to-paths.md),
2026-11-03. It read "An app may show the policy and the rule behind a refusal; no
app edits it", and its groups named whole repositories and lived in this file.
What survives: one reviewed policy, deny-only, read from the merged default
branch. What changes: groups may name paths, a deny may cover reading, model
groups come from a vendored catalog, an app may propose a change as a pull
request, and a deny may name one seat of a position by ordinal (ADR-0031 §8).)*

**3. A runner still declares its provenance class** (ADR-0011 §1), as a record.
A runner from a source no class describes, such as a model served by a
Chinese lab's own service, adds a class with it.

**4. What a position needs in order to run is not a restriction.** A runner
must have a spawn path for the position. Where it lacks one, the work is to
give it one, not to keep it out of the seat.

**5. Every runner names a full model version.** A runner's model is a full,
pinned version id, never an alias, at every position, and a review records it
(ADR-0024 §4), so reviews by different versions, or by two instances of one,
can be compared. A runner defined by an alias is redefined with the full
version, not kept out of review.
*(Amended by the owner, 2026-10-24. It read: "A review is pinned to an exact model
version. A runner at a reviewer position names a pinned model id, never an
alias." What survives: a review names the exact version that wrote it. What
changes: the full version is required of every runner, so an alias runner is
fixed rather than refused at a reviewer position.)*

**6. The open-weights box is set in the same policy file** (ADR-0011 §3), for a
group of repositories, by the same reviewed change as §2. No repository sets it
today.
*(Amended by the owner, 2026-10-24. It read: "configuration for a named repository
too … on the same terms as §2", which after §2's amendment no longer said where.)*

**7. (Withdrawn.)** *Withdrawn by the owner, 2026-11-05, before any of it was
built. It does not bind. It read:* "The owner may hold every reviewer seat of a repository. A repository's
reviewer seats, at every reviewer position and every ordinal, may be set to the
owner instead of runners. They are all the owner's or none are. The change is
made through `bin/fleet-models` in one step, which writes an audit row and a
ledger event for each seat, and the owner can change it back the same way.
Nothing may leave a repository with some seats the owner's and some an agent's,
whether by setting, enabling or disabling a seat or by a change to the `default`
row, and a reader that finds a mix treats the repository as unreadable: a pull
request's forge rule has one approval count, and ADR-0013 §4 makes a reviewer
that produced no review withhold the other's approval, so a mix would block or
silently drop review. No agent review is dispatched on a repository the owner
reviews, and its forge rule requires no agent approval, so the owner's merge is
its review. Mechanical checks still run: ADR-0014's one-kind check and CI are not
reviews. Such a repository is critical (ADR-0019 §1): owner review is refused on
an autonomous repository, and the repository cannot be made autonomous while the
owner reviews it, so `mergebot` never merges there (ADR-0019 §4). The fleet does
not dispatch plans there, because its ignition needs agent approvals. The policy
restricts models, and the owner is not one: no deny refuses an owner seat, and
ADR-0031 §8's minimum class does not apply to it."

## Consequences

- `bin/fleet-models` stops refusing a runner at a position by provenance class
  or by being local, and its pinned-id check accepts any family's pinned id,
  not only `claude-<family>-<digits>`. A plan does this.
- The runners named by an alias (`sub-sonnet`, `sub-opus`, `api-sonnet`) and those
  naming no model (`sub-codex`, `sub-antigravity`, `local-qwen`) are given full
  versions (§5), and `bin/fleet-models` refuses a runner without one at every
  position, not only at reviewer positions. The same plan does this.
- `operator/talos` `dispatch-review.py` launches a review only through
  `claude -p`. Each other runner needs a review spawn path before it can
  review. A plan does this.
- `bin/orchestrator` applies the open-weights box only where the repository
  sets it; the spawn-time refusal (ADR-0011 Consequences) becomes that check.
  No open-weights runner has a spawn path today, so nothing running changes.
- `operator/lynceus` offers every runner at every position, as `fleet-models`
  allows.
- *(Withdrawn with §7, 2026-11-05.)* §7 was to be built by eunomia plan 0204 (`fleet-models` sets and clears owner
  review for a whole repository, and `fleet-config` keeps such a repository
  critical), hosttools plan 0024
  (`forgejo-sync-protection.sh` counts only agent seats) and talos plan 0044
  (`dispatch-review.py` launches no review on an owner-reviewed repository). Until they
  land, no owner seat can be set.
