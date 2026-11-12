# ADR-0027 — An implementer may stop on a question, and an answer from the owner or intakebot resumes it

- **Status:** Accepted
- **Date:** 2026-10-26
- **Deciders:** the operator (owner/merger)
- **Amends:** [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md) — its "What is still not permitted" forbids any retry of the implementer's work; a resume on an answered question is now excepted
- **Relates:** [ADR-0012](0012-a-transient-failure-may-release-its-own-lease.md)
  (its "what is still not permitted" says a new case needs its own record; this
  is that record), [ADR-0016](0016-a-review-stopped-by-a-usage-limit-may-be-retried-by-mopsus.md)
  (the resume-request mechanism this reuses), plan 0031 (the comment and ack
  protocol, and `FLEET_OPERATOR_UID`), plan 0091 (`--resume`), plan 0147 (quoting
  owner text into a session's prompt), and operator/intakebot, the gateway that
  routes questions to the people and agents who answer them.
- **Spec-impact:** `SPEC.md` Principle 4 (a further named exception) and
  `docs/plan-dispatch.md` §"Leases, death, and respawn" (recovery is no longer
  only the owner's comment). Both are updated by a following `docs` pull
  request.

## Context

An implementer that meets a decision its plan does not make has two moves
today: guess, or stop with a free-text reason that pages as a failure. It
always guesses. A guess is invisible until review or rework finds it, and the
author who could have answered in a sentence never sees the question.

## Decision

**1. A question is a stop, not a failure.** An implementer may end its session
with a structured question in place of a guess, when its plan does not decide a
behaviour the work depends on. The orchestrator records it as `plan-question`,
not `plan-failed`. It posts the question where the plan's work lives (the marked
pull request, or an issue for the plan when no pull request exists yet), and it
holds the lease exactly as a stop does. The session cannot wait, so stopping is
how it waits.

**2. Only a question the plan could not answer may stop the run.** A question
about anything the plan, its ADRs or its flow spec already decides is a defect
in the run. The structured form names the plan section it found silent, so
whoever answers can check the claim, and an answer that cites the section that
decides it is recorded as a needless question.

**3. An answer is authority to resume that question, and nothing else.** An
`Answer: <question id>` comment resumes the plan as `--resume`, once, with the
question and the answer quoted into the session's prompt. Two numeric Forgejo
uids may answer: `FLEET_OPERATOR_UID` (the owner) and `FLEET_INTAKEBOT_UID` (the
intakebot gateway, which relays answers from a plan's author, from an agent, or from
the owner, and records who really answered). It binds to the id, never a login.
The intakebot uid's comment is not a `respawn`, an approval or a merge. It is
consumed by the one question it names. With `FLEET_INTAKEBOT_UID` unset, only the
owner can answer.

**4. An answer settles a question inside the plan, and never outranks it.**
Where an answer conflicts with the plan, its ADRs or its flow spec, those win,
and the conflict goes back as a question for the owner. Intakebot never relays
text that came from the session or the account that asked, so an implementer
cannot answer itself. The owner's answer outranks intakebot's. An owner `Answer:`
to a question intakebot already answered resumes the plan once more, with the
correction.

**5. Asking is bounded.** A plan may be resumed on intakebot's answers at most
`question_limit` times, a bounded `fleet-config` key. Past it, the plan pages
the owner, and only the owner's answers resume it. Every automated resume in
this fleet is bounded (ADR-0012, ADR-0016), because each one buys another
session with no person in the loop.

**6. No question is dropped.** A question with no answer pages once after a
configured wait, and again on a longer one. A question is never auto-answered,
and it never expires into a guess.

## What is still not permitted

An implementer may not ask instead of reading. A question resumes nothing until
an answer exists. An answer cannot widen the plan's lease, its paths or its
authority: it settles a decision inside the plan, and anything larger is a new
plan.
