# ADR-0011 — A runner has a provenance class, and the class decides which positions it may hold and what it may carry

- **Status:** Accepted
- **Date:** 2026-09-22
- **Deciders:** the operator (owner/merger)
- **Amended by:** [ADR-0013](0013-every-repo-is-tested-and-its-reviewer-is-configuration.md),
  2026-09-25 — plan 0065's Fable-only reviewer rule, cited in §2 and §4, is
  replaced by a per-repo reviewer defaulting to Fable.
- **Amended by:** [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md), 2026-09-25 — §2: `adr_reviewer`,
  `docs_reviewer` and `deploy_reviewer` are `us-hosted` only.
- **Relates:** [ADR-0009](0009-a-lane-may-have-a-supervisor.md) §3 (a position
  names a runner, not a vendor; this adds the second runner property a position
  may be tested by), [ADR-0006](0006-blackbox-test-lanes.md) §7 (the zone
  allowlist, unchanged and composing with this),
  `plans/0065-a-reviewer-is-configurable-and-always-fable.md` (the reviewer
  family rule this generalises, and the lesson that a policy tests a declared
  attribute), `plans/0072-a-runner-has-a-provenance-class.md` (the work).

## Context

`RUNNERS` in `bin/orchestrator` carries runners served three different ways: a
US lab's own service on the operator's subscription, the same service on an API
key, and open weights downloaded and run on the fleet's own hosts. Nothing in
the registry records which, so no policy can ask.

Plan 0065 added the first policy that refuses a runner at a position, on
`Runner.family`. The question it did not answer is which runners may be offered
to a position at all.

Anthropic's threat intelligence report of September 2026
(anthropic.com/threat-intelligence-report-september-2026, on illicit
distillation) names Alibaba, Zhipu, DeepSeek, Moonshot and MiniMax as running
unauthorised distillation against Claude, some by proxying their own customers'
requests to it. That is a finding about those companies' conduct, and conduct is
what a provenance judgment is made of. The owner's framing, 2026-09-22: qwen
"is pretty well boxed in and the plan would be to do the same for the others",
and it is "worth treating them in a different class of security than the US
companies".

## Decision

**1. Every runner declares a provenance class**, one of `us-hosted`, `us-open`,
`cn-open`. A runner that declares none is a registry error, not a fourth class.

| class | what it is | example |
|---|---|---|
| `us-hosted` | a model served by a US lab's own service | Anthropic Claude on the subscription or the API; Gemini through oracle's `independent` tier |
| `us-open` | open weights published by a US lab, run on a fleet host | Google Gemma |
| `cn-open` | open weights published by a Chinese lab, run on a fleet host | Alibaba qwen |

The registry carries the names. This record carries the classes.

**2. The class decides which positions a runner may hold.**

- `us-hosted`: every position, subject to plan 0065's reviewer family rule.

  *(Amended 2026-09-25 by ADR-0013 §3: plan 0065's family rule is gone, so
  `us-hosted` holds every position with no further condition.)*
- `us-open`: `implementer`, `tester`, `mediator`. Never `plan_reviewer`,
  `code_reviewer`, `impl_supervisor` or `test_supervisor`.
- `cn-open`: `implementer`, and nothing else.

  *(Amended 2026-09-25 by [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md): the three new reviewer positions,
  `adr_reviewer`, `docs_reviewer` and `deploy_reviewer`, are refused to both open classes, the same
  as `plan_reviewer` and `code_reviewer`.)*

`tester` and `mediator` are the whole of the difference between the two open
classes, and nothing in this record obliges a repo to use them: it says which
seats the class may be offered, not which it is put in.

**3. Both open classes run in the same box**, and it is the box
`operator/epeius` already implements: credential kind `None`, no environment
beyond the one variable naming its own model endpoint, no network reach but that
endpoint, uncommitted edits handed out rather than pushed, and no pull request
of its own. The harness opens the pull request, as agent-bus does for qwen
today, and the gate stays with the caller.

**4. Output from an open-weights runner reaches a merge button only through a
Fable review.** Plan 0065 already makes the reviewer positions Fable-only, so
that reviewer is necessarily from a different family than the runner that wrote
the code; §2 is what keeps an open-weights runner out of the seat that would
review its own class.

*(Amended 2026-09-25 by ADR-0013 §3. The reviewer is no longer necessarily
Fable. What survives is the half that mattered: §2 still keeps every
open-weights runner out of the reviewer seats, so output from one reaches a
merge button only through a `us-hosted` review, and that reviewer is
necessarily from a different family than the open-weights runner.)*

**5. The line between `us-open` and `cn-open` is a provenance judgment about
the labs, and is cited as one.** Open weights cannot phone home; they are
numbers, and §3 gives both classes an identical box. The residual risk is a
trained trigger that makes a model emit subtly wrong code on a cue, and
Anthropic's sleeper-agents research showed such backdoors survive safety
training and resist detection, so absence cannot be demonstrated for any
weights, US or Chinese. The classes bound blast radius. They do not assert
trust, and this line is revisited when the evidence about conduct changes.

## Consequences

- `bin/fleet-models` refuses a class at a position §2 forbids and names this
  record in the refusal. Tested the way plan 0065 established: on the declared
  attribute, never on a substring of the runner name.
- `bin/orchestrator` refuses at spawn time to run an open-weights runner that
  declares a credential, or any environment variable beyond its endpoint. A hard
  stop, not a fallback to a smaller box.
- The box is one box, so hardening it is one change and cannot drift between the
  two open classes. Only §2 differs between them.
- No dispatch changes. Every routed dispatch to date used `sub-sonnet`, which is
  `us-hosted` and eligible everywhere, and `local-qwen` stays declared and
  unwired.
- ADR-0006 §7 is untouched and composes: zone decides whether a plan's text may
  reach a cloud backend, provenance decides which position a runner may hold and
  what it may carry. A plan passes both or it does not dispatch.
