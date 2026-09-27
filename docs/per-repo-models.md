# Per-repo models — a repo chooses which runner serves each agentic position

*Plan 0048. Closes the gap ADR-0009 named: the fleet's own history could not
answer "does a bigger model change churn enough to justify the cost?" because
every one of the 34 routed dispatches used `sub-sonnet` — there was no second
arm, and no way to run one except a per-invocation environment variable.*

## What this is

`bin/fleet-models` adds two tables to `fleet.db` — `repo_model` and
`repo_model_change` — and is the one program that writes them. A human and
the control app's write route (a later plan; not built here) both go through
it, so every change lands exactly one `repo_model_change` row (ADR-0006 §1 as
amended: a configuration change replaces a reviewed merge for this class of
setting, and this is the audit trail traded for it).

```
fleet-models list                                    every row, as stored
fleet-models get <repo>                               resolved runner, per
                                                       position, with layer
fleet-models set <repo> <position> [<ordinal>] <runner>
fleet-models disable <repo> <position> [<ordinal>]    ordinal defaults to 1
fleet-models enable  <repo> <position> [<ordinal>]
```

`bin/orchestrator` is the one reader that matters operationally: it resolves
the `implementer` position from `repo_model` on every dispatch
(`resolved_implementer_env`), which is what replaced `DEFAULT_RUNNER` as the
first choice a run makes.

## The positions, and why there are exactly seven

ADR-0009 §1 sorted every position in the dispatch flow into mechanical (a
code guarantee) or agentic (a model runs there). Only the agentic ones get a
row — a dropdown that changes nothing is worse than a missing one, and the
first attempt at this screen listed seven roles before three of them turned
out to have no model at all (`fleet-watch`, `fleet-bundle`, and the
orchestrator wrapper itself).

| position | cardinality | default |
|---|---|---|
| `implementer` | exactly 1 | the `default` row |
| `plan_reviewer` | 1..n | 1 |
| `code_reviewer` | 1..n | 1 |
| `tester` | 1..n | 1 (ADR-0006 §3 as amended: one or more) |
| `mediator` | 0 or 1 | none |
| `impl_supervisor` | 0 or 1 | none (ADR-0009 §2) |
| `test_supervisor` | 0 or 1 | none (ADR-0009 §2) |

**The fleet watcher does not appear.** It is fleet-scoped, and a per-repo row
for it would imply the fleet can have more than one dispatcher.

Today, only `implementer` has a consumer (`bin/orchestrator`). The other six
positions can be set and read through `fleet-models` — the schema and the CLI
do not distinguish "wired" from "not yet wired" — but nothing dispatches a
`plan_reviewer` or a `tester` by reading this table yet. Wiring those is
follow-on work, not a gap in this one.

## Resolution: repo, then default, then nothing

For one `(repo, position, ordinal)` slot: the repo's own row where
`disabled = 0`, then the `'default'` row for that same position/ordinal where
`disabled = 0`, then **nothing** — there is no further, built-in fallback a
misconfigured repo silently lands on.

For `implementer` specifically, `bin/orchestrator`'s `EUNOMIA_IMPL_RUNNER`
sits **above** this whole table — the operator's own override always wins,
and it is what makes a bakeoff arm a one-line env var rather than a database
write. That override belongs to the orchestrator, not to this module:
`resolve_repo`/`resolve` never look at it.

**No model is inferred for a repo that has not chosen one.** The `default`
row is the whole mechanism — not tier, not language, not repo size. A repo
with no row of its own reads exactly what `default` says, and deleting the
`default` row for a position makes the next dispatch **stop**, not silently
fall back to some constant. `config/impl-bounds.conf` established the reason:
a setting that silently reverts to a code default is an unattended process
spending more than the operator set, and a model doing the same is the same
defect, just harder to notice because the run still succeeds.

**One exception, and it is a bootstrap concession, not a loophole.** An
install where `repo_model` has never been created at all — no `fleet-models`
write has ever happened here — is not "misconfigured," it is "has not adopted
this feature yet," and `bin/orchestrator` falls back to its own
`DEFAULT_RUNNER` exactly as it always did. The first `fleet-models` write
anywhere creates the table, and from that point on the strict rule above
applies in full — including to a `default` row someone then deletes.

## Removal is `disabled = 1`, and an ordinal is never reused

A row is never deleted. `fleet-models disable <repo> <position> [<ordinal>]`
sets `disabled = 1` and leaves the row exactly where it was; `enable` reverses
it. Adding a new reviewer after disabling #2 of 3 allocates ordinal **4**, not
2 — the change log, and later, review records, name ordinals, and renumbering
would silently rewrite what an existing record meant.

`ordinal` is optional on `set`. A singleton position (`implementer`,
`mediator`, `impl_supervisor`, `test_supervisor`) always targets ordinal 1.
A list position (`plan_reviewer`, `code_reviewer`, `tester`) allocates the
next ordinal that has never been used for that `(repo, position)` — the
maximum existing ordinal plus one, counting disabled rows, so a slot is never
handed out twice.

## A runner name is validated against `RUNNERS`, at write and at read

`bin/orchestrator`'s `RUNNERS` registry is a Python dict, not a SQL fact, so
it cannot be a `CHECK` constraint. `fleet-models set` refuses to write a name
`RUNNERS` does not carry, with the known names in the error. A row can still
go stale if a runner is later retired from the registry — `resolve` (and
therefore `get` and `bin/orchestrator`'s read) raises the same refusal in that
case, rather than silently dispatching to a name that no longer means
anything. This is `impl-bounds.conf`'s rule again: a hard stop that was
deliberately reachable (`local-qwen`, declared but unwired) must stay a hard
stop, not become a dispatch-time crash on some repository nobody was
watching.

## The reviewer positions take Fable runners only

*Plan 0065.* `plan_reviewer` and `code_reviewer` accept a runner only if its
`family` is `fable`; today that is `sub-fable` (`--model claude-fable-5-1`,
`local_cli`, the shape of `sub-opus`). Every other runner is refused, and every
other position — `implementer`, `tester`, `mediator`, `impl_supervisor`,
`test_supervisor` — still takes any runner in the registry, as ADR-0009 §3
says. The restriction is narrower than that sentence and says why:

```
$ fleet-models set operator/eunomia code_reviewer sub-sonnet
fleet-models: 'sub-sonnet' cannot serve code_reviewer: 'Fable on every PR, hard
stop' (the operator, 2026-08-26; ~/agent/dispatch-review.py MODEL). The reviewer
positions accept only 'fable'-family runners (accepted: ['sub-fable'])
```

That is the 2026-08-26 ruling, which was a comment above `MODEL` in
`~/agent/dispatch-review.py` (`operator/talos`) and is now code that refuses.
Without it, giving reviewer positions a vocabulary at all would have let one
repo be set to `code_reviewer = sub-sonnet` — the ruling broken silently, on a
repository nobody was watching.

**It is a property of the runner, not of its name.** `Runner` carries a
`family`, and the rule is `family == "fable"`. A substring match on the name
would break the day a runner is called `api-fable`, and would admit one called
`sub-sonnet-fable-ish`.

**It is enforced at write and at read.** `set` refuses at write. `resolve` — and
so `get`, and any reader that goes through it — refuses the same way at read,
so a row inserted directly into the table, or one that predates this rule, does
not become live by surviving. `get` prints the refusal against that slot
instead of a runner name.

**The pin is exact, and must stay exact.** `sub-fable` passes
`claude-fable-5-1`. The catalog rejects `claude-fable-5.1` and `claude-fable-51`,
and a family alias (`--model fable`) would move the reviewer's model without a
decision. Do not add a Fable runner that resolves an alias.

### Reviewer rows are recorded and not yet read

**Setting a reviewer row changes nothing about what runs.** After

```
fleet-models set operator/eunomia code_reviewer sub-fable
```

the row exists, reads back with layer `repo`, and is in the change log and the
ledger — and the review pipeline still reviews exactly as it did. Do not expect
a second review to appear. This is the "control the operator will believe"
failure ADR-0009 §1 refused dropdowns for; the difference here is that the
setting is real and safe to hold, and is simply not consumed.

The consumer is `~/agent/dispatch-review.py` in `operator/talos`, which spends the
Fable quota and hardcodes `MODEL = "claude-fable-5-1"`. For it to read these
rows, that repository would have to change (each item is its own reviewed
change there, not something this repo can do):

- resolve `code_reviewer` (and `plan_reviewer`, for plan PRs) per repo through
  `fleet-models` — `active_ordinals` then `resolve` — so the read-time refusal
  applies; reading the table by hand would skip it;
- map a resolved runner to the model id it dispatches with, from that runner's
  `argv` in `bin/orchestrator`'s `RUNNERS`, rather than a second copy of the id;
- run one review per enabled ordinal, and carry the ordinal into the review
  record so verdicts from two reviewers on one PR can be told apart and counted;
- decide what a repo with no row does. The rule above says nothing falls back
  silently, but the pipeline reviews every PR today with no rows at all, so
  either a `default` row is seeded first or the absence keeps today's single
  `claude-fable-5-1` review as an explicit, stated exception;
- treat a refusal (an unresolvable or non-Fable row) as a stop on that review,
  not a reason to review with some other model.

## The `tester` position can now name a vendor, and nothing reads it yet

*Plan 0066.* `bin/orchestrator`'s `RUNNERS` registry carries two vendor
runners, `sub-antigravity` (Google's Antigravity CLI) and `sub-codex` (OpenAI's
Codex CLI), so a repo can record

```
fleet-models set operator/eunomia tester sub-antigravity
fleet-models set operator/eunomia tester 2 sub-codex
```

and read both back, in ordinal order, exactly as any other `tester` row does.
Both are cloud backends behind ADR-0006 §7's zone allowlist: only a
`zone: public` plan can resolve either, the same rule that already gates
`sub-sonnet` and the rest of the Claude-family runners.

**`sub-codex` is registered but not wired.** `codex exec -s read-only` confines
writes, not reads (ADR-0006 §6), so the isolation a test lane needs — a
workspace with no repository on disk — does not exist yet outside the CLI.
Selecting it is a hard stop with its own message, the same shape `local-qwen`
has carried since plan 0010.

**Setting a `tester` row changes nothing about what runs**, and not because of
a bug: **nothing dispatches a test lane today.** ADR-0006 §1's flag has no
reader (`plans/0013-fleet-repo-enable.md`'s main-via-contents-API half is
unbuilt), and no code anywhere constructs a tester spawn — no worktree, no
cwd-outside-every-work-root, no settings precondition applied at spawn time.
Recording a runner here is exactly as inert as recording a `plan_reviewer` or
`code_reviewer` row was before plan 0065 gave those a consumer: real, safe to
hold, in the change log and the ledger, and not consumed by anything until
plan 0013's other half exists and something calls `check_settings_precondition`
and builds the blind workspace ADR-0006 §6 requires before it spawns either
vendor CLI.

## Where the rows live

`repo_model` and `repo_model_change` are tables in `fleet.db`, alongside
`review` / `model_price` — `bin/fleet-reviews` is the precedent for a
non-derived, directly-written table sharing that file with `bin/fleet-collect`'s
derived history. See `docs/fleet-db.md` for the full two-tier account: which
tables `fleet-collect` can throw away and rebuild from transcripts, which it
must never touch, and why `bin/orchestrator` reading `repo_model` is not the
staleness risk that doc warns dispatchers away from elsewhere.

## What this plan does not do

**It does not serve HTTP.** The control app's read and write routes are a
later plan. Building them before anything obeyed this table would put an
endpoint in front of a setting nothing consumed yet — the orchestrator
obeying the table is the risk worth taking first; the screen is not.

**It does not wire `plan_reviewer`, `code_reviewer`, `tester`, `mediator`,
`impl_supervisor` or `test_supervisor` into a dispatcher.** Only
`implementer` has a consumer today. The schema and CLI treat all seven
positions alike so that wiring the rest later is a read, not a migration.
