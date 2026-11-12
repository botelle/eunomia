# CI stress rounds (plans 0113 and 0128)

`bin/fleet-ci-stress` runs plan 0113 D3's concurrency measurement: three CI runs
of one tree, queued together on three scratch branches, to see whether two
eunomia runs can overlap safely. It is started by a person. A round puts three
full CI runs on the cihost runner and needs an empty queue, so when to run one
is the operator's call; nothing schedules it.

## Running a round

```bash
bin/fleet-ci-stress round --kind iso --tree <scratch-sha> --n 3
```

- `--kind iso` is an isolated round (the tree with plan 0113 D2). `--kind ctl`
  is a control round (the same tree without D2).
- `--tree` is the scratch commit's sha, already in the checkout you run from.
- `--n` is the round number. The branches are `ci-stress/0113-r<n>-{a,b,c}`.
- `--bound` is the wall-clock limit in minutes (default 60).

It refuses unless no commit among the last 10 of `main` and of every branch has
a `pending` status, and checks again right before the push. It pushes the three
branches in one atomic `git push`, waits until every job of the runs has
finished, reads the jobs from the actions tasks API, and applies D3 step 4's
rule. Then it writes `docs/ci-evidence/0113/round-<kind>-<n>.json` and prints
the path.

The three branches are deleted on every exit path the process can catch: success,
a red job, the bound expiring, a refused push, SIGINT, SIGTERM and SIGHUP.
SIGKILL cannot be caught by anything. If the process is killed that way,
`fleet-orphans` pages on what is left, and the fix is to delete the branches by
hand.

Exit codes: `0` recorded and counted and green; `1` recorded but a job is not
green or the round does not count; `2` refused; `3` the bound expired (no
record); `129`/`130`/`143` interrupted (no record).

A record is not overwritten without `--replace`. A red or uncounted record is
evidence, so a rerun of that round number is a decision, not a side effect.

### A finished round that was not recorded

```bash
bin/fleet-ci-stress rebuild --kind iso --round 1 --runs 1258,1260,1262
```

`--runs` names the three runs' numbers, one per branch. `rebuild` pushes
nothing. It reads the tasks API, takes every task on those branches at the
runs' commit (`plans.yml` has run numbers of its own, so those come too), and
refuses if a named run is missing, if the runs are not the three branches of
that round, or if they disagree on the commit. It resolves the tree and base
from the checkout, else from the forge's git-commit endpoint, else records
`null` and says so.

## The Gate

```bash
bin/fleet-ci-stress gate
```

Exit 0 only when 0113's Gate passes: three counted isolated rounds, every job
of every isolated record green with `Plans / lint` green on each branch, and
three control rounds recorded. Control rounds are recorded, not gated on
their result. It prints one line per isolated round and never edits a plan.

## What a record means

| field | meaning |
|---|---|
| `kind`, `round` | `iso` or `ctl`, and the round number |
| `scratch_sha`, `tree` | the pushed commit and its tree |
| `base_commit` | the commit the scratch commit was built on (its first parent) |
| `tasks[]` | every job: `id`, `run_number`, `workflow`, `name`, `branch`, `result`, `run_started_at`, `updated_at` |
| `overlap` | the pair of full-suite jobs that satisfied the rule, or `null` |
| `counts` | `true` only if `overlap` is not `null` |
| `source` | `round` or `rebuild` |

The interval of a job is `run_started_at` to `updated_at`. The API has no stop
field and `updated_at` can lag the real end. Two jobs overlap only if
`A.start + 60 s < B.updated_at` **and** `B.start + 60 s < A.updated_at`. The
jobs must be full-suite (`tests (cihost)`, `tests (cihost-linux)`, and
`plans.yml`'s `lint`) and on **different** branches. One run's own matrix pair
always runs side by side, so it never counts.

The queue is read from commit statuses, not from the tasks API, because a task
row exists only once a job starts.

## Where the scratch trees come from

Both are one commit on top of a base, and the commit must touch a path under
`plans/` (a whitespace edit to plan 0113's own file will do) or `plans.yml`
never triggers and its `lint` job goes unmeasured.

- **Isolated tree:** the plan 0113 D2 commit, plus the concurrency-group change
  (`group: eunomia-${{ github.ref }}` in all three workflows) and D4's
  `tests/test_ci_workflow.py` change.
- **Control tree:** the same group change and test change, without D2.

The D2 commit is on the first run's `feat/0113-…` branch at `3d8e319d`. That
is the `base_commit` of rounds 1 and 2. The branch has to be deleted or
renamed before the orchestrator will dispatch 0113 again (eunomia#769).

## Boundaries

The script never changes the group on `main` or any workflow file, never opens
a PR, and never edits a plan. The token comes from the token helper and is held
in memory for the Authorization header; it is not in argv and not on disk.
Git goes over the checkout's ssh remote and uses no token.
