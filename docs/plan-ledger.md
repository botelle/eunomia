# A plan and what became of it

*Companion to `docs/plan-triage.md` (why a plan's status is a claim, not proof),
`docs/plan-graph.md` (the same front matter, drawn) and `docs/fleet-db.md` (the
`dispatch` view this reads). Implements plan 0061.*

`fleet-plan ledger` puts each plan next to the dispatches that ran it: how many
runs, how the latest one ended, which pull request it produced, how many review
rounds it took, and how long the implementer sessions ran. It is **provenance,
not a verdict.** Whether a plan's work exists is `fleet-plan audit`'s question,
answered from the work; this answers *who ran it and what merged*.

```
fleet-plan ledger [paths...] [--format table|json] [--db FILE] [--repo owner/name] [--verdict] [--cost]
```

With no paths it reads every plan under `plans/`. It writes nothing — not a
plan, not the database, not a marker file — and it exits 0 whatever it finds,
because a finding about a plan is not a failure of the command.

## Why it exists

A plan's `status` is a claim someone typed; a dispatch row is a thing that
happened, and nothing put them side by side. `fleet-plan list` reads only the
files, the `dispatch` view reads only the runs, and `fleet-plan audit` asks
whether a plan *can* dispatch. On 2026-09-17 `0057` failed at 12:16Z with
*"round 1: fix session timed out after 90 min"* while its plan file read
`ready` — indistinguishable from a plan nobody had merged — and it was found by
a hand-built join, not by any tool.

## Columns

| column | meaning |
|---|---|
| `id`, `status` | the plan file's own front matter |
| `runs` | dispatches recorded for this plan **in this repo**; `-` when there is no dispatch data |
| `outcome` | how the newest run ended: `merged`, `closed`, `failed`, `not-terminal`, `none` (no run), `unknown` (no dispatch data) |
| `pr` | the newest run's pull request number |
| `pr_state`, `pr_source`, `pr_as_of` | that pull request's state, **where it came from**, and when — see below |
| `rounds` | review rounds, summed across every run |
| `impl_seconds` | implementer session seconds, summed across every run and every fix-round exit (`dispatch.seconds` alone is only the *last* exit, which is why it is not used) |
| `class` | `claimed`, `stalled`, or `-` |
| `reason` | for a failure, the run's own `failure_reason` |

`not-terminal` is deliberate wording: a lease with no terminal record may be
running, or may have died without writing one (`docs/fleet-db.md`, the terminal
phase rule). The ledger does not call it running.

## What it cost (plan 0076)

Under a plan's summary row, when `--cost` is given or the plan has any linked
session, an indented block: one line per dispatch, one per review round of that
dispatch's pull request, then a `total`.

```
0068-the-dispatch-cap | ready | 1 | merged | 68 | ...
    branch--feat-0068-…--002  2026-09-22T01:53:00Z  merged  processed 46.7M  output 118k  turns 204  minutes 34.8  sessions 1
      round 1  REQUEST_CHANGES  processed 650k  output 21k  minutes 5.0
      round 2  APPROVED  processed —  output —  minutes —
    total  processed 47.4M  output 139k  partial
```

* **`processed`** is every input token the session read — fresh, cache read and
  cache write; **`output`** is what it wrote. Say which; never call output
  "used". The unit is tokens, **never dollars**: everything here runs on a
  subscription, and the CLI's `total_cost_usd` is an API-equivalent the fleet
  does not pay.
* A dispatch line sums **over that lease's linked implementer sessions** (the run
  and each fix round), with the session count. If any of the lease's pairs could
  not be linked it carries **`partial`** after the numbers — a partial sum is
  printed only when labelled. A run or round with no linked session prints `—`
  in each cost column, not 0.
* `total` sums the runs and rounds that linked, and carries `partial` if any
  dispatch line does **or any round is unlinked**.
* The link is deterministic: a run to its session through the worktree path and
  the run's time window, a review through the `session=` field of its provenance
  line, each **unique or absent** (`docs/fleet-db.md`, `dispatch_session`). The
  153 reviews before that field existed show `—`; they are not backfilled by
  window.
* `--format json` adds, per plan: `dispatches: [{lease, started, outcome, pr,
  sessions, unlinked, cost}]`, `review_rounds: [{pr, round, verdict, cost}]` and
  `total: {processed, output, partial}`, with raw integers (`cost` is `null` when
  nothing linked). The rounds list is `review_rounds`, not `rounds`, because
  `rounds` is already the summary column's count.
* When `fleet.db` predates the two views, or has no `review` table, the summary
  prints unchanged and `--cost` says `cost: unavailable`.

**How to read it: processed grows with turns × context, so a run past ~60 turns
is a plan asking too much of one session.** The measurement: 0067 at 32 turns
processed 2.2M, 0019 at 64 turns 9.8M, 0068 at 222 turns 46.7M. Two turn counts
exist and they differ: `num_turns` on the CLI's result line (222 for 0068) counts
every API round-trip including tool loops, while the `session` table's `turns`
column counts distinct assistant messages (204 for the same run). The block
prints the table's; the rule of thumb above is the CLI's.

## Two classes, and how to read them

Both are pointers at a place to look. **Neither says the plan is unfinished.**

| class | meaning |
|---|---|
| `claimed` | `status: done` and **no dispatch record**. A person decided it; the fleet has no run to show. |
| `stalled` | a failed dispatch and no successful one, whatever the file says. |

**`claimed` is not an accusation.** `status: done` answers *"did someone decide
this was finished"*, and it is routinely read as *"did the fleet build it"*.
Most of the twelve predate the dispatcher and were done by hand because there
was nothing to dispatch them — several of them **are** the dispatcher
(`0004-plan-dispatcher`, `0010-implementer-session`, `0011-review-loop`). The
report prints that sentence itself, in both formats, so the column is not read as
a list of lies.

**A `stalled` row is not an unreviewed pull request.** `0057` is both: its
dispatch failed, and its pull request #443 sat green with no review. This tool
reports the first and only *names* the pull request; the second is
`fleet-stalled`'s (plan 0060). Two tools describing one condition in different
words is how an operator learns to read neither.

A plan with a failed run and a later successful one is **not** `stalled` —
`0045` failed on 2026-09-12 and merged as #479 on 2026-09-21.

## The join

**A dispatch belongs to a plan by the plan id the run recorded, exactly — and
to the plan's own repository.** Never by branch name: branch names are
truncated (`feat/0057-the-verb-inventory-covers-fleet-orp`), so a match on them
is a prefix guess that collides the first time two plans share one. Under
`--verdict` the pull request is resolved through the `Plan:` marker, line-anchored,
via `fleet-watch`'s `marked_pr` — `0009-auth`'s lookup cannot land on
`0009-auth-v2`'s pull request.

**Scoped to one repository, and it says which.** The dispatch table carries
every repository's plan ids — on 2026-09-21, 30 of its 58 distinct ids belong to
somebody else. Listing those as plans missing from this repo's pin would be wrong
in the most confusing direction, so the scope is `--repo` or the checkout's
`origin`, and the header names it. If neither can be determined the command
refuses (exit 2) rather than guess. A dispatch of *this* repo whose plan has no
file here (renamed, removed) is named under `unmatched`, not dropped.

## A missing database is not a finding

`fleet.db` is derived, can be mid-rebuild, and `fleet-plan` runs on hosts that
never ran `fleet-collect`. It is opened `mode=ro`. If it is absent, unreadable,
or has no `dispatch` view, **every plan reports `unknown`, no plan is `claimed`,
and the command exits 0** — the absence of a record is not evidence that nothing
ran. `unknown` is a different thing from `none`: `none` means the database was
read and this plan has no run in it.

## `fleet.db` is an index, not an authority

`dispatch.pr` and the outcome beside it are frozen when the event was written;
`fleet-collect` never polls the forge (`docs/fleet-db.md`). So a pull request's
state is one of two things and is always labelled which:

| `pr_source` | meaning |
|---|---|
| `fleet.db` | as the record last saw it, **as of `pr_as_of`**. Not fresh. A pull request closed since still reads `merged` here. |
| `forge` | read just now, under `--verdict` |

## `--verdict`

Opt-in, because it costs forge reads. It does two things:

1. **Each pull request's current state**, from the forge, via the `Plan:` marker.
   The run's stored pull request is read directly and accepted only if its body
   carries `Plan: <id>` on a line of its own and an implementer opened it — the
   two tests `marked_pr` applies; anything else falls through to `marked_pr`'s
   own scan. (A scan per plan took over ten minutes for 48 plans; the direct read
   takes about fifteen seconds.) A stored `merged` that the forge now shows `closed` is reported as `closed`,
   source `forge`. A marker the forge cannot find is `no-marked-pr`. A forge that
   cannot be read leaves the row as-of and says so in the footer.
2. **`audit`'s capability probe** for the rows in either class, in a separate
   `probe` column, quoted in `audit`'s own vocabulary. `audit` itself refuses to
   grade a `done` plan; plan 0061 D2b asks for the probe on exactly those rows, and
   it is the only place that refusal is lifted. The ledger's own columns never
   assert whether a plan was built, and `probe` is path evidence with all the
   limits `docs/plan-triage.md` lists — six of six paths present for both `0025`
   and `0021`, one fully done and one not started.

## `--format json`

The shape a service serves: `scope`, `db` (state, path, detail), `rows` (one
object per table row, keys equal to the column names, plus `probe` under
`--verdict`), `counts`, `unmatched`, `unattributed`, `out_of_scope`, `errors`
and `notes`. `plan-graph` (0047) already publishes the node set; this adds the
outcome per node rather than a second graph. `tests/test_plan_ledger.py`
parses both formats and asserts every row and value agrees.

## What this cannot do

- **It never writes a plan's status.** The tool best placed to "just fix" the
  twelve is exactly the tool `docs/plan-triage.md` forbids to: #292 flipped six
  plans on evidence revbot proved wrong twice.
- It cannot say a plan was built, in either direction. A merged pull request is a
  fact about a pull request.
- It cannot see a run the collector has not ingested, and a lease with no repo or
  plan id (`unattributed`) is counted and never joined.

## First run, 2026-09-21

Against `main` and the live `fleet.db` — 48 plans of this repository, 75
dispatches, 40 of them another repository's:

| | 2026-09-17, by hand | 2026-09-21, `ledger` |
|---|---|---|
| `claimed` | 12 | **12** — the same count four days on; nothing new has appeared |
| `stalled` | 2 (`0045`, `0057`) | **2 (`0057`, `0063`)** |

What the hand-built join could not have shown:

- **`0045` left the class**: re-dispatched and merged as #479 four days after
  the join was built. A join that ran once reports a state that has since changed.
- **`0063` joined it**: `status: ready`, a session that exited 0, a pull request
  #470 opened — and the run failed anyway (*"no verified marked PR … still says
  `status: ready`"*). The hand version chose its two classes from what it happened
  to surface and would never have looked for a plan that *did* open a pull request.
- **`0057` is `status: done` and `stalled`.** Its plan file was flipped after the
  failed run; both statements are on one row, which is the point of a ledger.
- **Both stalled plans' pull requests are merged on the forge** (`--verdict`:
  #443 on 2026-09-17, #470 on 2026-09-19). The dispatch failed and a person
  finished it — so `stalled` names a run that did not complete, and says nothing
  about whether the work shipped. `audit`'s probe on the same two rows says
  `undecidable` for `0057` and, for `0063`, reports a declared path
  (`launchd/org.eunomia.fleet-snapshot.plist`) missing from `main` while its pull
  request is merged. Those two answers are not reconciled here on purpose; they
  are the reason the ledger carries no built-or-not column of its own.
- **No plan with a merged run is anything but `done`**, and `0057` is the only
  `done` plan whose newest run did not merge. Where a record exists, the claim
  and the record agree except on the row the class names.
- Across every plan that has runs, review rounds sum to 31 and implementer time
  to about 13.7 hours (49,470 s).

`claimed` should shrink only if hand-built plans stop appearing after the
dispatcher was armed. Twelve on the first run and twelve four days later is the
baseline; a thirteenth would be a finding about how work actually gets done
here, and belongs in front of the owner rather than in a column.
