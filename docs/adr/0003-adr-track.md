# ADR-0003 — When a decision changes, tests are written by someone who has not seen the code

- **Status:** Accepted
- **Date:** 2026-09-06
- **Deciders:** the operator (owner/merger)
- **Amended by:** [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md), 2026-09-25 — §2: the ADR pull request no longer
  carries the flow-spec edit; the spec change is its own following pull request.
- **Relates:** `plans/0004-plan-dispatcher.md` (the orchestrator loop this hangs off),
  `plans/0011-review-loop.md` (the reviewer contract, whose independence argument this
  extends from review to tests), **[ADR-0002](0002-candidate-green.md)** (candidate
  green — the mechanism the divergence check should reuse).

## Context

The fleet's review independence is a mandate property, not a credential one. Plan 0011
states it: the orchestrator is graded on *approved*, so it may not author any part of
the review mandate, because a mandate it writes lets it steer the reviewer away from
its own weak spots without ever touching the reviewer's credentials.

Tests have no equivalent. Today the implementer writes the code and the tests in one
session, from one reading of one plan. When both come from the same reading, the tests
prove the code does what the code does. A misread plan produces a passing suite, and
the suite's greenness is then cited as evidence the plan was implemented.

This matters most exactly when it is least visible: when an **architectural decision
changes**. A new or amended ADR is the moment the system's stated behaviour moves.
Everything downstream — the flow spec, the tests, the code — is now describing a system
that did not exist an hour ago, and there is nothing in the loop that notices.

There is a second, cheaper failure underneath it. The written model of the system
(`docs/plan-dispatch.md`, `SPEC.md`) drifts from the ADRs, silently, because nothing
forces them into step. The review rounds on `plans/0010`
(eunomia #34) turned up a live instance: `docs/plan-dispatch.md:244` still says the
built/unbuilt boundary will "release the lease", while `bin/orchestrator:247` documents
the opposite in terms — and that document is the one the implementing session is told
to read.

## Decision

When a change's diff touches `docs/adr/**`, an **ADR track** runs alongside the normal
plan-dispatch loop:

**1. The trigger is a file test on the diff, evaluated where a diff exists.**
Does the change touch `docs/adr/**`? It is deliberately not a judgment — "has this
altered an architectural decision?" is a question a model can be wrong about and, worse,
one the planner benefits from answering *no*.

The question is asked twice, but the two asks are not the same thing, and an earlier
draft of this ADR was wrong to claim they were:

- The **planner** asks while drafting, and the answer shapes the plan: a change that will
  touch an ADR needs the spec update and the test work planned alongside it. This ask is
  **advisory**. It can only read the plan's own `paths:` declaration, which the planner
  wrote, so it establishes nothing about authority.
- The **authoritative** ask happens where a diff and a head SHA exist — at pull-request
  time.

  **This repo does not have that pass yet, and the implementing plan builds or adopts
  it.** An earlier draft said the ask goes "in the pass that already fetches the diff for
  review dispatch and the candidate build," which was two wrong claims in one clause:
  review dispatch is `~/agent/dispatch-review.py`, which ADR-0002 §2 records as living
  outside this repository and unversioned, and `bin/fleet-candidate` is hand-run and
  unscheduled (ADR-0002 §3) and fetches no diff at all. Neither is a place an
  *authoritative* gate can live: one is outside the reviewed-merge authority that §2 says
  every statement here rests on, and the other only runs when a human remembers. An ADR
  about prose drifting from what runs should not itself name a pass that does not run.

`fleet-watch` cannot be the second asker, which is what the earlier draft got wrong.
At dispatch the implementer has not run, so there is no diff; the only artefact available
is the planner's declaration, and re-reading it is not independence. The property being
bought is not two opinions agreeing. It is that the authoritative ask is **mechanical,
and late enough to see the truth**.

The cost of that choice is stated plainly: a change that violates an existing ADR
without editing it does not trip this gate. That case is left to review.

**2. The flow spec is updated before any test is written — and the ordering is checked,
not instructed.**
`docs/plan-dispatch.md` and `SPEC.md` are brought into step with the amended ADR first.
The order is load-bearing rather than tidy: the flow spec is the artefact the test agent
writes from, so a stale spec produces tests that encode the old decision and then
"prove" the new implementation wrong.

Load-bearing steps do not survive as instructions, so this one is a condition: an
ADR-touching pull request must **either** also touch `docs/plan-dispatch.md` or `SPEC.md`,
**or** carry an explicit `Spec-impact: none` line in its body. Failing both, the track
does not dispatch the test agent and says why in a comment. The check is mechanical; the
assertion behind `Spec-impact: none` is a human's, and it reaches `main` only through a
reviewed merge — the same authority every other statement in this system rests on.

*(Amended 2026-09-25 by [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md). The ordering survives: the flow spec is still
updated before any test is written. What changed is where the update lands. A pull
request changes one kind, so the ADR pull request carries `Spec-impact: none` or
`Spec-impact: <spec path>`, and a named impact is a following `docs` pull request,
ordered as ADR-0014 §3 says: a plan whose `flow:` names a spec depends on the plan
that updates it, so the per-plan test lane runs against the current spec. Before this amendment the ADR pull request itself had
to touch `docs/plan-dispatch.md` or `SPEC.md`.)*

**3. Regression tests are written by an agent that has not seen the code.**
A different vendor on purpose — a Gemini or ChatGPT subscription, not the implementer's
model — plans and then writes regression tests from the **ADR and the flow spec only**.
It does not receive the diff, the implementer's session, the branch, or the interfaces.

The vendor split is not superstition about model quality. It is that two readings drawn
from the same weights share the same blind spots, and the property being bought here is
that the tests and the code are *independent* readings of one decision. Same-family
independence is weaker in a way nobody can measure from the inside.

**4. Tests land as their own pull request, with their own human merge — opened by a
credentialed wrapper, never by the test agent.**
Not into the implementer's branch: a feature that carries its own tests can pass by
adjusting them, and a fix round is exactly when that becomes tempting.

The test agent returns file content and nothing else. A wrapper of the same shape as
`bin/orchestrator` commits that content to a branch and opens the pull request —
guarantees in code, judgment in the model. So the third-party session needs no Forgejo
token, no deploy key and no write path of any kind, which is what makes "never receives
credentials" a fact about the architecture rather than a hope about configuration. Note
this is deliberately *not* plan 0010's implementer pattern, where the session holds the
token and opens its own PR; that pattern is available to a first-party model and not to
this one.

**5. Divergence is adjudicated against the ADR, and the adjudicator arms nothing.**
When the tests and the code disagree, a **mediator** (Fable) is spawned. It reads the
ADR and returns two things as **content**: an adjudication saying which side is wrong and
why, and a follow-up plan at `status: draft`. The credentialed wrapper posts the
adjudication as a pull request comment and opens the plan PR. The mediator writes nothing
and holds no token at all.

*Who spawns it is deliberately not named.* `fleet-watch` dispatches `status: ready` plans
reachable on `main`; it has no divergence detection, and divergence surfaces after both
merges, which is not a moment the watcher observes. Naming it here would be the same
class of claim §1 retracts. The implementing plan builds or adopts that host, alongside
the pull-request-time pass §1 needs.

That last sentence is the guarantee, and it has to be architectural for the same reason
§2 gives about the spec ordering: load-bearing steps do not survive as instructions.
"Does not push, does not approve, does not merge" would otherwise be a line in a prompt,
guarding the party with the most authority in the loop.

An earlier draft argued this from token scope — that a token which can comment can also
open a pull request. That is **false for Forgejo**, whose scopes separate the two
(`write:issue` for comments, `write:repository` for pull requests), so a comment-only
token does exist. The decision is unchanged and is deliberately stricter than the scope
argument would require: it does not depend on a scope being configured correctly, only on
the mediator having nothing to configure. Worth keeping in view, though, that if the
mediator ever did hold an implementer-scoped token — implbot's, say — a PR from it
carrying `Plan: <id>` would be indistinguishable from the in-flight marker `fleet-watch`
reads to decide a plan is already being worked. §4 made this structural for the test agent; there is no
argument for making it weaker here. The fix is armed the way everything else is armed: a human merges the plan.

The ADR is the referee because it is the only artefact both sides read and neither
wrote. Any other referee — the code, the tests, the implementer's reasoning — is one of
the parties.

## Consequences

**Expect false divergences, and do not treat them as a defect.** A test written without
sight of the code will be wrong about names, signatures and call shapes. That noise is
the price of the property; a design tuned to eliminate it converges on showing the test
agent the code, at which point agreement means nothing. The mediator exists to absorb
that cost, which is why it adjudicates rather than merely reporting.

**Divergence surfaces after both merges, so `main` can go red.** Tests and feature merge
independently, and nothing gates one on the other. This is the accepted cost of not
making a test-agent outage a hard stop on all feature work — but it should be mitigated
rather than lived with: **ADR-0002's candidate build** (base ⊕ head) can evaluate
`main ⊕ feature ⊕ tests` before either merges.

It must report **by comment, not by commit status**, and that is a constraint rather than
a preference. ADR-0002 §2 records that a `candidate: failure` status blocks review
dispatch of that pull request — `dispatch-review.py` refuses when any context is failing.
A status-reporting three-way build would therefore hand the test PR precisely the
blocking authority Alternatives rejects below, and, given this ADR's own "expect false
divergences", it would do so routinely rather than rarely. Separately, ADR-0002 §3's
staleness marker is a single `base=<sha>` slot in the status description, and a build
with two moving inputs cannot be expressed in it. The wiring is left to the implementing
plan; the reporting channel is not.

**A third-party subscription joins the trust surface.** A Gemini or ChatGPT session is a
second vendor holding repository context. It must be scoped to the ADR and the flow spec —
which is what it needs anyway — and it must never receive credentials. `zone: private`
plans cannot use it at all, and that check belongs with the routing rule, not with the
caller.

**This ADR predates its own gate, and is the last of three the gate never sees.** Earlier
drafts claimed the track "fires on its own arrival", which is tidy and untrue: the
authoritative ask happens at pull-request time, the track does not exist until the
implementing plans merge, and by then the pull requests that introduced ADR-0001, 0002
and 0003 are closed. Honouring the claim literally would mean backfilling the gate over
merged history — dispatching a paid third-party test agent retroactively against
decisions nobody is changing. **That is a non-goal.** The first real firing is the next
amendment to `docs/adr/**` after the track is built.

What this ADR can do about itself, it does by hand: its own pull request carries
`Spec-impact: none` under §2, because nothing here is built and the flow spec describes
what runs.

**Cost is bounded by the trigger.** A feature that changes no decision pays nothing
extra — no test agent, no mediator, no second PR.

## Alternatives considered

**Have the reviewer write the tests.** Rejected: the reviewer already reads the diff, so
its tests inherit the implementation's shape, and it would then be reviewing work it
authored — collapsing the coder/reviewer split that plan 0011 exists to protect.

**Same vendor, separate session.** Cheaper and simpler, and it does buy context
independence. Rejected as the default because it does not buy *model* independence, and
the failure this is aimed at — a plausible misreading that survives into both artefacts —
is the failure most likely to be shared within a family. Worth revisiting with evidence;
a bakeoff could measure it.

**Judgment trigger instead of a file test.** Rejected: it makes the gate a question the
planner is graded on, and answering "no" is always cheaper for it.

**Mediator fixes the divergence directly.** Rejected: it would make the mediator an
implementer with an opinion about who was right, and the only agent in the loop with no
counterparty. Adjudicate-and-file keeps arming with a human.

**Block the feature on the test PR.** Rejected by the same reasoning that keeps the
watcher from acting on staleness: it converts an outage in a support system into a stop
on all work. Candidate-green is the way to get the signal without the coupling.

## Definition of done

This ADR is done when the decisions above are recorded and the implementing plans exist.
Three are needed, not two, and the third is the one this ADR spent two rounds discovering
it was missing:

1. the **pull-request-time pass** that hosts the authoritative ask (§1) and the divergence
   check and mediator spawn (§5) — the component this repo does not have;
2. the **test agent** and its credentialed wrapper (§3, §4);
3. the **mediator** and its adjudication path (§5).

It is **not** a claim that any of it is built. Until those merge, the ADR track is a
decision and nothing else.
