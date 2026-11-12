# Arenas — one repository, many cells (plan 0062)

*Plan 0062. A plan/implementer/reviewer model comparison runs as N cells
against ONE repository — the "arena" — and every cell's record carries the
base commit and the resolved position→runner map it actually ran with, so a
result is attributable from the record itself rather than by joining a
start time against `repo_model_change`.*

## What this is

`bin/fleet-arena` adds two tables to `fleet.db` — `arena_cell` and
`arena_cell_assignment` — and is the one program that writes them, the same
one-writer shape `bin/fleet-models` and `bin/fleet-reviews` already
established for this file (plan 0048's precedent).

```
fleet-arena cell --repo <arena> --plan <repo-relative path> --base <full sha>
           --assign implementer=<runner> [--round <name>]
fleet-arena list [--repo] [--round]
fleet-arena show <cell-id>
```

`cell` runs exactly one cell: it resolves and pins the assignment, checks
the base, writes the record, and only then dispatches `bin/orchestrator` —
a real dispatch, holding a real lease, exactly as `bin/fleet-watch` would
start one, except that the implementer position is pinned to whatever
`--assign` named instead of resolved from `bin/fleet-models`' `repo_model`
table.

## `--assign` accepts exactly one position

`bin/orchestrator` has exactly one `resolve_repo` call site —
`resolved_implementer_env`, with the literal string `"implementer"`. The
other six positions in `bin/fleet-models`' CHECK constraint
(`plan_reviewer`, `code_reviewer`, `tester`, `mediator`, `impl_supervisor`,
`test_supervisor`) are a vocabulary that table admits, not a wiring the
orchestrator honours today. `--assign <anything else>=<runner>` is refused
before anything is written, naming that call site — even a value-valid
runner on a real position, like `code_reviewer=sub-opus`, because the
question is never whether the runner is real, only whether anything reads
the slot.

```
$ fleet-arena cell --repo operator/arenarepo --plan plans/0004-x.md \
    --base <sha> --assign code_reviewer=sub-opus
fleet-arena: --assign 'code_reviewer' is refused: bin/orchestrator has exactly
one resolve_repo call site, resolved_implementer_env, and it names the literal
'implementer'. ...
```

## `--base` is an assertion, checked, never obeyed

`bin/orchestrator` has no base input of its own. A fresh dispatch always
calls `lib.add_worktree(repo, lease_id, "origin/main")` with the ref
hardcoded, and branches from wherever that resolves at dispatch time —
nothing outside `bin/orchestrator` can change that, and this file does not
try to.

So `fleet-arena` fetches the arena and resolves `origin/main` itself, and
**refuses when it differs from `--base`** — before any row is written and
before a lease is minted:

```
$ fleet-arena cell --repo operator/arenarepo ... --base 0000000000000000000000000000000000000000
fleet-arena: --base 0000...0000 is not operator/arenarepo's origin/main —
it is currently a1b2c3.... fleet-arena checks --base, it does not obey it:
reset the arena's main to --base, or state a new base for this round
```

`--base` must be a full 40-character SHA. A short or symbolic ref (`main`,
`HEAD`, a 7-character prefix) is refused with the same shape of message,
before any I/O — it is ambiguous in a repo's future, and the record outlives
the run. eunomia's own `main` moved from PR #95 to #461 in ten days; an
arena that silently drifted underneath a round would make every later cell
incomparable to the earlier ones without anyone noticing.

`arena_cell.base_sha` is the commit this check verified `origin/main` to
be — the commit the orchestrator branches from, unless the arena's `main`
moves between this check and the orchestrator's own fetch, a window
`fleet-arena` does not close — never the raw
`--base` argument echoed back. `arena_cell.plan_sha` is a content hash of
`git show <base_sha>:<plan path>`, read from the same fetched store, never
from a file the operator handed over on the command line.

## Every cell gets seven assignment rows

Not only the position `--assign` named. A record that lists one position
invites the reader to assume the rest were neutral, and they were not: they
were pinned, or read from `repo_model`, or absent entirely. `source` says
which, `effective` says whether `bin/orchestrator` ever reads the value at
all.

| position | `source` | `effective` |
|---|---|---|
| `implementer` | `assigned` — whatever `--assign` named | `1` |
| `plan_reviewer`, `code_reviewer`, `tester`, `mediator`, `impl_supervisor`, `test_supervisor` | `repo_model` / `default` / (`'none'`/`default` if unconfigured) | `0` |

**The reviewer positions are recorded from `repo_model`, not pinned.**
ADR-0013 superseded plan 0065's Fable-only rule: `~/agent/dispatch-review.py`
resolves `plan_reviewer` and `code_reviewer` through `bin/fleet-models`. The
rows are `effective=0` because `bin/orchestrator` does not read them and the
review runs outside the cell, at review time — so the recorded runner is what
`repo_model` said at dispatch, and a later change to the table is not
reflected. `source='pinned'` stays in the CHECK set but nothing writes it.

**The six non-implementer positions are recorded purely for the record.** `bin/orchestrator`
reads none of them — `resolve_repo`'s one call site is `implementer` only —
so whatever `bin/fleet-models`' `repo_model` table would have resolved for
`tester`/`mediator`/`impl_supervisor`/`test_supervisor` is written down with
`effective=0` and either `source='repo_model'` (the arena's own row),
`source='default'` (the shared `default` row), or `runner='none'`,
`source='default'` when neither exists. None of it changes what the cell
runs.

## The lease and branch: a second minting path beside `fleet-watch`'s

`bin/fleet-watch`'s `spawn()` mints a `branch` lease named `feat/<plan-slug>`
— one branch per **plan**, because a plan only ever runs once in the normal
dispatch flow. An arena runs the same plan N times, once per cell, so
`fleet-arena` mints its own lease, naming `cell/<plan-slug>/<cell_id>`
instead:

```python
# bin/fleet-watch's spawn() (unchanged, shown for comparison)
branch = f"feat/{slug_of(plan_id)}"
resource = {"type": "branch", "repo": repo, "branch": branch}
fleet-claim --assign <resource> --holder orch-<slug>-<8 hex> --note "plan:<plan_id>" --ttl <ttl>

# bin/fleet-arena's mint_lease() — same call shape, a per-cell branch
branch = f"cell/{slug_of(plan_id)}/{cell_id}"
resource = {"type": "branch", "repo": repo, "branch": branch}
fleet-claim --assign <resource> --holder arena-<slug>-<last 8 chars of cell_id, lowercased> --note "arena:<cell_id>" --ttl <ttl>
```

Both go through `fleet-claim --assign` — the same collision check, the same
lock, the same lease-record shape (SPEC's Grant flow) — so row 6's pre-push
hook and row 4's heartbeat-derived orphaning apply to a cell exactly as they
do to a normal dispatch. `fleet-claim`'s own per-resource conflict check is
what makes concurrent cells on one plan safe with **no lock of
`fleet-arena`'s own**: each cell's branch name embeds a fresh ULID, so two
cells never name the same resource, and there is nothing to collide over.

There is deliberately no lock held across a cell's lifetime. The one
position `bin/orchestrator` reads is carried in the `EUNOMIA_IMPL_RUNNER`
environment variable of the spawned process — `resolved_implementer_env`
returns before ever touching `repo_model` when that variable is already set
— so two cells on one arena share no state a lock would need to protect. A
lock held "for the cell's duration" would also never release in practice:
after review, the orchestrator enters its steward phase, whose only exit is
the PR reaching a terminal state, and cell PRs stay open (see below) — the
second cell would wait on an operator, forever.

## Runners a cell could not start are refused

`--assign implementer=<runner>` also refuses a runner the orchestrator would
`Stop` on at once: one declared `unwired` (`local-qwen`, `sub-codex`), or one
that needs a credential other than `local_cli` (`api-sonnet`). A cell is
dispatched with `EUNOMIA_INITIATOR` unset, which `resolve_credential` maps to
`local_cli`. Both are refused before any row is written.

## How the orchestrator is spawned

Like `bin/fleet-watch`, `fleet-arena` starts the orchestrator as a supervised
job through `fleetjob` by default, because a bare `Popen` from a process that
exits at once is the shape that breaks LAN git fetches on this fleet's macOS
hosts. `FLEET_SPAWN=popen` selects the bare `Popen`. If the spawn fails, the
cell is marked `failed` and the freshly minted lease is released.

## The assignment is written before dispatch

The `arena_cell` row and its seven `arena_cell_assignment` rows go in under
one `BEGIN IMMEDIATE` transaction that lasts milliseconds; the lease is
minted and the orchestrator is spawned only after it commits. A cell whose
dispatch — the lease mint, or the orchestrator spawn itself — fails
afterward is marked `outcome='failed'` by an `UPDATE`. The row and its
assignment are never deleted: a failed dispatch is evidence, not noise.

`outcome` starts at `'running'` and `fleet-arena` never advances it past
that on success — there is no code path here that learns a PR opened and
flips it to `'opened'`, and none that flips it to `'abandoned'`. Doing that
would mean polling the forge for a marked PR, which is most of
`verify_marked_pr`'s job over again, and nothing in this plan's Deliverables
asked for a second copy of it. Read `pr`/`outcome` as "what `fleet-arena`
itself observed at dispatch time," not as a live status — for that, ask the
forge, or `bin/orchestrator`'s own log for the lease.

## What this file will never do

**Score a cell.** The outcome metric is an independently authored test suite
that never sees any cell's code; a harness that both runs the arm and grades
it is the same model marking its own work one level up.

**Merge a cell branch, or flip a plan's `status:`.** Cells are branches and
pull requests that stay open or get closed by a human. A merged cell would
move the arena's `main` out from under every later cell in the round —
exactly the comparability an arena exists to protect.

## Operating an arena

1. Create the arena repository through the standing new-repo checklist (the
   `new-forgejo-repo` skill) — same private/collaborators/protection/webhook
   setup as any other repo. **Do not add it to `FLEET_WATCH_REPOS`.**
   `fleet-arena` dispatches the orchestrator itself, with the assignment
   pinned; a watcher scanning the same repo would dispatch its own
   unpinned, unrecorded arm off any `ready` plan that reached the arena's
   `main`.
2. Put the plan under comparison on the arena's `main` (a normal plan PR,
   merged the normal way).
3. Note the arena's `origin/main` SHA — that is the round's `--base`.
4. Run one `fleet-arena cell` per arm, same `--repo`/`--plan`/`--base`/
   `--round`, one `--assign implementer=<runner>` each:
   ```
   fleet-arena cell --repo operator/arenarepo --plan plans/0004-x.md \
     --base <sha> --assign implementer=sub-sonnet --round build
   fleet-arena cell --repo operator/arenarepo --plan plans/0004-x.md \
     --base <sha> --assign implementer=sub-opus --round build
   ```
5. `fleet-arena list --repo operator/arenarepo --round build` to see the
   round; `fleet-arena show <cell-id>` for one cell's full record, assignment
   included.
6. Judge the resulting PRs by hand, or against the round's independent test
   suite — never through `fleet-arena` itself.
