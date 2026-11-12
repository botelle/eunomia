# Spec and regression evidence agree before merge (plan 0191)

A pull request whose plan touches an API or a screen — the same `spec_watch`
mapping plan 0177's `fleet-plan lint` already enforces, in the target repo's
`governance.yaml` — gets a merge checklist from the orchestrator's steward,
posted as a comment on the **implementation** pull request, scoped to its
exact head. A plan whose lease reaches no `spec_watch` rule is not enrolled
and gets nothing: this changes no behaviour for it (`spec_gate_step` in
`bin/orchestrator`).

This is evidence, not enforcement. It is never a commit status — a required
status would hold the review dispatcher, which the meeting itself depends on
(ADR-0002 §2) — and it never starts a fix round or a review. Ordinary code
review and CI proceed with it pending. The owner reads it and checks its
head sha against the pull request's current head immediately before
merging. **No automated merge enforcement reads this comment.** Building
that is separate work, and it would have to enforce the evidence at the
merge decision rather than through reviewer CI, the same way this plan keeps
it out of required statuses.

## Eligibility

`spec_watch_reached(wt, plan_path)` reuses plan 0177's own functions from
`bin/fleet-plan` (`_spec_watch_rules`, `_rule_reached`), imported read-only
the way `bin/fleet-triage` already imports `bin/orchestrator`. A plan is
enrolled when its own `paths:` lease reaches at least one rule in the
repository's `governance.yaml` `spec_watch:` list — the identical test
`fleet-plan lint` already ran at plan-filing time. No repo's paths are
hard-coded here; a repo with no `governance.yaml`, or no `spec_watch` key,
is simply never enrolled.

## The five states

| State | Meaning |
|---|---|
| `missing` | An enrolled rule is reached, but this pull request's own diff never touched the rule's `specs` glob — the lease covers it (plan-lint already checked that), the PR itself does not. |
| `stale` | No `fleet-meet` findings document covers this exact head yet, or the one on disk is for an older head. Absent and mismatched are the same state: neither is evidence about the PR's current state. |
| `failing` | Findings at this exact head exist but `fleet-meet`'s own `all_passed` is not `true`, and no owner waiver scoped to this head covers it. This covers a failed or errored test, but just as much a lane that was refused or timed out before any test ran, a dispatched lane fleet-meet never read (`not_run`), or a suite that reported no test at all — none of these is a non-passing *test*, so all of them are read from `all_passed` rather than re-derived from `tests`. A mediator classification (`fleet-triage`'s `triage.json`) is recorded in the comment when it exists, but a classification alone never moves the state off `failing` — only a repaired, re-met head or a waiver does. |
| `passing` | Findings at this exact head exist and `fleet-meet`'s own `all_passed` is `true` — every lane ran, no lane was left unread, and every test passed. |
| `waived` | Findings at this exact head record a failure, and the owner posted a matching exception (below) for this exact head. Reported distinctly, with the waiving owner's login, their reason, and the head. |

`missing` is checked before evidence is read at all: a pull request that
never touched its own required spec cannot pass regardless of what its
tests show.

## What the comment carries

- The plan id and the implementation head (full sha).
- The sha256 of every file in the pull request's own diff that matches a
  reached rule's `specs` glob — "the spec documents supplied to the
  tester" — read through the forge's contents API at the exact head, never
  from a worktree that might have moved on.
- For `failing` and `waived`: each item that kept the head from a clean
  pass and its outcome — a non-passing lane/test pair, but also a lane
  that never ran (refused, timed out, a collection error), a dispatched
  lane that was never read, or an empty suite.
- The mediator's identity, when `fleet-triage` has already classified this
  head's findings (read from `triage.json`; never spawned from here).
- For `waived`: the waiving owner's login and their stated reason.
- The owner merge procedure, below, restated in the comment itself.

It opens with
`<!-- eunomia:spec-regression-gate plan=<id> head=<sha12> state=<state> -->`,
and is posted whenever the computed state differs from the state carried by
the **newest** `eunomia:spec-regression-gate` comment already on this exact
head — not merely whenever this exact `(plan, head, state)` triple was ever
posted before. A state that recurs at a head (`stale` → `passing` →
`stale` again, after a lane repair) posts again each time it becomes the
current state, because the owner procedure below trusts only the newest
comment: if a recurring state were silently skipped, that newest comment
could go on showing a state that was no longer current. The forge wrapper
has no comment-edit call, so a new state is always a new comment; old
comments are left as the historical record. Before posting,
`assert_spec_gate_body_safe` refuses a body carrying an `Ack: #n` line, a
`Plan:` line, or any `eunomia:` marker other than its own — the same rule
`fleet-triage`'s `assert_body_safe` applies to its own comment, because a
report must never be readable as an instruction.

## The owner's exception

`Require a repaired run or an explicit ... owner-authorized exception
scoped to the head`: the owner posts a pure two-line comment,

```
Spec-gate-waive: <reason>
Head: <sha or short sha>
```

and nothing else in that comment. `is_spec_gate_waiver` requires exactly
those two lines (the same "pure token" shape `is_unfreeze` uses for
`unfreeze`), so this can never be mistaken for the tail of an unrelated
instruction. It is split out of the ordinary instruction channel by shape,
not by timing: the steward acks it (recording it, never spawning a session
to "do" it) the same cycle a `respawn` control comment is acked, and the
main instruction loop skips a comment of this shape before it ever reaches
`pending_instructions`'s processing.

The `Head:` value is matched as a case-folded hex **prefix** of the current
head, so a short sha still scopes correctly — but it does not scope to any
other head: a waiver posted for one head does not cover a later push. The
newest matching waiver from the owner wins if more than one is posted for
the same head.

## The owner merge procedure (manual; no server-side enforcement)

For an enrolled pull request, before merging:

1. Read the newest `eunomia:spec-regression-gate` comment on the pull
   request.
2. Confirm its `head=` matches the pull request's current head sha exactly.
   A comment for an older head is not evidence about the current one —
   treat it as if there were none (`stale`, in effect).
3. Merge only on `passing` or `waived`. On `missing`, `stale` or `failing`,
   wait for a fresh comment, or ask for the two-line exception above.

This plan does not add a `required` status, a merge button check, or any
other mechanical block — the comment is the whole of the enforcement today,
and the step above is the owner's, done by hand. Automated merge
enforcement reading this comment is separate work, out of this plan's
scope (Boundaries, plan 0191).
