# ADR-0009 — Every lane position is mechanical or agentic, and a lane may have a supervisor model it does not have by default

- **Status:** Accepted
- **Date:** 2026-09-13
- **Deciders:** the operator (owner/merger)
- **Amended by:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md),
  2026-09-25 — §4's tester minimum binds every repository, not only an
  opted-in one.
- **Amended by:** [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md), 2026-09-25 — §1 gains ADR-PR, docs-PR and deploy-PR
  review, all agentic.
- **Relates:** [ADR-0006](0006-blackbox-test-lanes.md) (§3 amended by this ADR;
  §7's zone allowlist governs which runners a plan may reach),
  `plans/0004-plan-dispatcher.md` (the wrapper this makes optionally
  supervisable), `docs/diagrams/plan-dispatch-flow.svg` (which gains the axis
  below).

## Context

The per-repo configuration screen for `operator/lynceus` asked a question the
design had never answered: **which steps in the flow can have a model at all?**

The first attempt listed seven roles. Reading the code against them, three of
the seven have no model and were never intended to:

- `bin/fleet-watch` — *"never edits a repo, never talks to a model"*, its own
  docstring.
- `bin/fleet-bundle` — the only occurrences of "claude" in it are a directory
  name it skips.
- `bin/orchestrator` — the deterministic wrapper. Its only model invocations are
  the `RUNNERS` registry it spawns the *implementation session* under. There is
  no orchestrator prompt; the two prompt files are the implementer's preamble and
  the reviewer's contract.

That last one had been mislabelled in public. Diagram v1.5 drew node ④ as
"Implementation lane — **Opus**". v1.6 removed the model name because
`DEFAULT_RUNNER` is `sub-sonnet`, which fixed the wrong half: **the error was
attaching any model to ④.**

The reframed question is not architectural but empirical, in the owner's words:

> does giving the lane a higher model planner (opus vs sonnet) increase the
> quality/churn enough to outweigh the extra cost?

**The fleet's own history cannot answer it.** Every one of the 34 routed
dispatches in `fleet.db` used `sub-sonnet`; there is no second arm to compare
against.

## Decision

**1. Mechanical vs agentic is a property of the design, and is documented.**

Not a setting. A position is mechanical because its guarantees are code, and
naming that is what stops a configuration screen growing a dropdown that changes
nothing.

| position | kind |
|---|---|
| ① planning session | agentic |
| plan-PR review | agentic |
| ignition gate (`fleet-watch`) | **mechanical** |
| ④ implementation lane (the wrapper) | **mechanical** — see §2 |
| ⑤ implementation session | agentic |
| ⑥ review loop | **mechanical** loop, agentic reviewer |
| repo opt-in | **mechanical** |
| bundle builder | **mechanical** |
| test lanes | agentic |
| the wrapper that opens the test PR | **mechanical** |
| the meeting (tests run against code) | **mechanical** |
| mediator | agentic |
| follow-up plan PR | **mechanical** |

*(Amended 2026-09-25 by [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md): the table also has **ADR-PR review**,
**docs-PR review** and **deploy-PR review** (all agentic), because a pull request now changes one kind and
each kind has its own reviewer. The per-repo screen's rows below gain ADR,
docs and deploy reviewer(s). The rest of §1 is unchanged.)*

**2. Two positions gain an OPTIONAL supervisor model. The default is none.**

④, the implementation lane, and the test lane's coordinator may each be
configured with a model that supervises the workers beneath them. **Configured
with nothing, both stay exactly as they are today** — deterministic, and free.

The default is what makes this safe to adopt: enabling a supervisor is an
experiment against a recorded baseline, not a migration, and a repo that never
sets one pays nothing and behaves identically.

**3. Any position that takes a model takes any runner in the registry.**

Claude, Codex, Gemini, qwen — the position names a runner, not a vendor. Subject
to ADR-0006 §7, which is unchanged and still an allowlist: only `zone: public`
may reach a cloud backend, and anything else resolves local or refuses.

This is what makes a whole-flow bakeoff possible on a repo where it is cheap to
be wrong, rather than only on the implementation step.

**4. ADR-0006 §3 is amended: one or more vendors, at least one required.**

§3 read "Two vendors, one bundle." It becomes a list with a minimum of one. The
disagreement argument in §3 — that where two vendors disagree the specification
was ambiguous — is a property of running **two or more** and is retained as the
reason to prefer more than one, not as a requirement for exactly two.

The same applies to reviewers: a repo may configure additional model reviewers
beyond the required human approvals. They annotate; they do not approve. Branch
protection's required approvals are a separate mechanism and are not changed by
this ADR.

*(Amended 2026-09-25 by ADR-0013 §2. With ADR-0006 §1 superseded there is no
opt-in, so "at least one required" applies to every repository: one required
tester, any number of additional ones. The reviewer paragraph is unchanged,
and ADR-0013 §4 relies on it — additional reviewers annotate, which is how a
reviewer A/B arm runs.)*

## The baseline any supervisor must beat

Measured from `fleet.db` on 2026-09-13, after plan 0036 made dispatch state
queryable. **All 34 routed dispatches used `sub-sonnet`.**

| | |
|---|---|
| outcomes | 24 merged, 10 failed (29% failure) |
| mean duration | 1,053 s |
| review rounds | 20 runs at 0, 17 at 1, 6 at 2, 2 at 3 |
| churn | **8 of 42 needed more than one round; mean 0.83** |

An experiment that does not move these is a cost increase.

## Consequences

**The cheap arm comes first.** Swapping the *worker* — `EUNOMIA_IMPL_RUNNER=sub-opus`
— already exists as a one-line override, and the orchestrator's docstring calls
that "what makes a bakeoff arm a one-liner". It costs nothing to run and it
informs the supervisor question: if a larger model as the worker does not move
churn, a larger model supervising the worker is a weaker bet.

**The prior evidence does not favour "bigger is better".** This fleet's own
bakeoffs put Sonnet 2–0 on edit discipline, and the v2 result was that which
files the model sees mattered more than which model saw them. Planning is a
different task from editing, so neither is decisive — but the intuition is not
this estate's observed default.

**The `RUNNERS` registry must grow.** It holds four entries today, one of which
(`local-qwen`) is declared unwired on purpose. Codex, Gemini and additional local
models are follow-on work, not part of this decision.

**A fair comparison needs the same plan on two arms.** Plans differ in
difficulty, so a between-plans comparison is confounded. The bakeoff shape this
fleet already has — one spec, several models, several draft PRs — is both
stronger and cheaper than accumulating runs.

**The per-repo screen's rows follow from §1**, and are no longer a matter of
opinion: implementation session, plan reviewer(s), code reviewer(s), tester(s),
mediator, and the two optional supervisors. The fleet watcher is fleet-scoped and
does not appear on a per-repo page at all.
