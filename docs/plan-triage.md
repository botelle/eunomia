# Comparing what a plan expected against what was built

## The finding that is solid

A plan merged at `status: draft` never dispatches — ignition is `ready` **on
main**. On 2026-09-11 the repo held **20 of them**:

```
plans on main:   done 10 | ready 2 | abandoned 3 | draft 20
```

They read as filed and settled. Each one is a dead end, and nothing in the tree
says so. That is the problem worth fixing, and it needs no per-plan judgement.

## The finding that is not: which of them were built

**This document does not say.** Three probe methods were tried and all three
produce false positives. They are recorded here because an auditor that does not
know its own failure modes manufactures confidence, which is worse than silence.

### Probe 1 — does each declared path exist?

Fails when the path **pre-existed the plan**. Seven of the twenty declare only
files like `bin/orchestrator`, `bin/fleet-watch`, `SPEC.md`. Those exist for
every plan that touches them, so presence carries no information at all.

### Probe 2 — grep for the feature

Fails on names. `0013-fleet-repo-enable` was graded built because `bin/fleet-watch`
contains `def repos(`. It does. Here it is in full:

```python
def repos():
    return [r.strip() for r in os.environ.get("FLEET_WATCH_REPOS", "").split(",")
            if r.strip()]
```

It reads an environment variable. `config/repos.conf` is read by `fleet-orphans`
and `fleet-repo` and **never by `fleet-watch`**, so the allowlist the plan is
about is not wired to dispatch. The grep proved a function by that name exists
and was read as proving what it does.

### Probe 3 — is there a `Plan: <id>` commit on main?

**Wrong three times. It works.** All three wrong versions are kept, because the
last one failed in a way worth more than the answer.

**v1 — "it matches the plan's own filing commit."** The hit was real, the cause
was not: it matched `57908cb`, whose body *quotes* the marker while discussing
dedupe. The probe was run as an unanchored substring.

**v2 — "sound, just run unanchored."** Anchoring removed the false positive, so I
declared it fixed without checking it still found anything.

**v3 — "unrunnable from a clone; the marker lives only in PR bodies."** Reported
as measured, across every plan, zero hits. Two things were wrong at once:

- The pattern used the **numeric prefix** — `^Plan: 0009[[:space:]]*$` — while
  the marker is the **full front-matter id**, `Plan: 0009-agent-asks-a-question`.
  It could not match anything. The zero was guaranteed before any repository was
  consulted.
- The loop that produced it **errored on every iteration**
  (`bad output format specification`) and printed nothing. Thirty-six failures
  were read as thirty-six zeros.

A HIGH-severity design constraint for `fleet-plan audit` was then built on the
output of a command that never ran.

**What it actually returns**, anchored, with the full id, on `origin/main`:

| plan | status | hits |
|---|---|---|
| 0002-fleet-emit-events | done | 3 |
| 0006-secret-guard | done | 3 |
| 0024-container-evaluation | ready | 8 |
| 0032-post-approval-freeze | draft | 6 |
| …and 7 more | | 1–2 each |

Eleven of thirty-six plans, most of them `done`.

**So the probe is usable — in one direction only.** A hit means marked work
landed. **Absence means nothing**: of eleven `done` plans, nine have a marker
commit and two (`0010-implementer-session`, `0018-fleet-bundle`) have none.

**And it is a proxy, not the fleet's own check.** The authority is
`bin/fleet-watch:867`, which anchors the same regex against `pr["body"]` across
pull requests in any state. Commit messages correlate with that and are not it,
so the two can disagree and only one of them gates dispatch.

**For `fleet-plan audit`:** marker commits are legitimate **positive** evidence
available from a clone. Their absence is not evidence of absence, and must never
be reported as such.

**The lesson under all three versions** is not about markers. v1 and v2 each
fixed the previous error's form while keeping its frame. v3 failed differently
and worse: the instrument never ran, and its silence was recorded as a result.
Before a measurement becomes a claim, check that the command produced output at
all — a zero and a failure look identical once they reach a table.

### What actually worked, twice, and does not scale

Reading the plan, then checking the one artifact it promised:

| plan | probe | result |
|---|---|---|
| 0021 | a live `events.jsonl` line, for the `v` and `plan` keys | `actor detail lease pr repo sha ts type` — neither key |
| 0023 | `FLEET_OPERATOR_UIDS` in the watcher | absent; still `FLEET_OPERATOR_UID` |

`0015-ignition-gate` shows why the probe must come from the plan: it greps
positive for "ignition" throughout `fleet-watch` — as a *concept*. Its promise is
an approval check, an open-question check and a SPEC-lease check. None exist.

## The status vocabulary has no word for what happened

```
ready ──(DoD met, PR opened with a Plan: <id> marker, handoff written)──> done
        └─ abandoned, when a plan is dropped rather than built
```

Neither fits **"reality overtook this plan."** `0021-event-schema` proposes a v1
event record; the fleet has emitted v0 all week and every consumer reads them.
That is not debt, and it is not a plan that was dropped — it is a proposal the
system declined by moving on, and no field can say so.

`0032-post-approval-freeze` is the other shape: a real marked PR (#244, five
review rounds) whose own §5 records **three §2 deliverables deliberately not
implemented**, with "the plan does not close until they land." Built, marked,
reviewed — and genuinely incomplete. `done` and `abandoned` are both false.

## What to build

`fleet-plan audit`, and its most important property is a refusal:

1. Report every plan on main at `draft` — the dead ends. Mechanical, always right.
2. For each, report declared paths that are **missing** (evidence of absent) and
   declared paths that **pre-existed the plan's own filing commit** (evidence of
   nothing).
3. **Refuse to grade.** Print the undecidable ones as undecidable. Do not grep
   for a feature name and call it built.

Given that plans are evidence and the running system is authoritative, the useful
output is not a verdict per plan. It is a short list of plans whose declared
world no longer matches the tree, for a human to abandon on sight.

## What was built

`fleet-plan audit [--json]`. Scope is `status: draft` plans, checked against
`origin/main` (defaulted, not the local `main` — the two drift, and this repo's
own worktree caught that drift while this tool was being written: a stale local
`main` reported a real plan's filing commit as "not found"). Never grades `done`
or `abandoned`, and never mutates anything.

Four plan-level states, from path evidence alone — "built" is not one of them:

| state | meaning |
|---|---|
| `not_built` | a declared path (other than the plan's own file) does not exist |
| `undecidable` | every declared path exists, and all of them pre-date the plan's filing commit |
| `touched_after_filing` | every declared path exists, and at least one was added AFTER the filing commit |
| `error` | the plan didn't parse, or its git history couldn't be read — reported as itself, never silently dropped |

`paths` entries are globs, matched against the tree (`fnmatchcase`) rather than
compared as literals, and the shared front-matter reader now parses both the
inline and block-yaml shapes — `0036` and `0038` are read correctly rather than
as declaring no paths.

A shallow clone refuses outright (`git rev-parse --is-shallow-repository`
checked before anything else): a shallow `--diff-filter=A` silently finds no
adding commit, which would otherwise render as the most favourable possible
reading — "nothing pre-dated the plan" — produced by missing data.

**Run against `origin/main` on 2026-09-13**, 16 draft plans (`0000-template`
included — it genuinely carries `status: draft`):

```
5 undecidable, 9 not built, 2 touched after filing, 0 error
```

`0013-fleet-repo-enable` and `0032-post-approval-freeze` — graded built by hand
on 2026-09-11, both wrongly — both come out `touched_after_filing`, not built.
`UNDECIDABLE` is reached by five: `0000-template`, `0021-event-schema`,
`0023-principals`, `0025-capability-published`, `0027-leak-watch-entropy-floor`.

Five of sixteen — under a third — is what this method can settle on its own.
The other eleven have unambiguous path evidence (nine missing paths, two
touched after filing) and still are not proof of "built" or "not built" beyond
what the table above states; they are simply not `undecidable`. Reading the
system directly, plan by plan, is still the only way to close the rest, and
that reading is the honest next step this tool was scoped to leave for a human,
not to shortcut.
