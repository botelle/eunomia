# `eunomia/deps` — the dependency hold (plan 0107, ADR-0019 §5)

`bin/fleet-deps once` keeps a commit status, context `eunomia/deps`, on the
head of every open PR targeting `main` in each repository the watcher scans
(the `dispatch` rows of `config/repos.conf`). The launchd unit
`org.eunomia.fleet-deps` runs it every 60 seconds.

## The rules

| The PR | State | Description |
|---|---|---|
| body has no `Plan: <id>` line | `success` | `no plan named` |
| names a plan not on `main`, or on `main` at neither `ready` nor `done` | `failure` | `plan <id> is not on main` / `plan <id> is <status> on main, not ready or done` |
| names a plan whose `depends_on` is unmet | `failure` | `plan <id> waits on <dep>: <detail>` |
| names a plan that is `ready`/`done` with every dependency met | `success` | `plan <id> is <status> on main` / `plan <id> is <status>, its dependencies are met` |
| anything undecidable | `pending` | `cannot read plan <id>`, `cannot read plans on main`, `dependency check failed` |

"Unmet" is `fleet-watch`'s `_plan_dependency_block`, loaded read-only: a plan
id is satisfied at `done` on `main`, a `path:` when the path exists there. A
transient failure reading a `path:` dependency therefore shows as `failure`;
the next pass corrects it.

Plan PRs, ADRs, docs and reverts carry no `Plan:` line and are `success`.
ADR-0019 §2 leaves them to the owner's merge, and this status does not judge
them. It checks nothing about approvals or authorship.

## `pending`, and who is paged

`pending` means the program could not decide: an unreadable PR list, plan file
or plans directory, or a dependency check that raised. It never posts
`success` on a guess. It pages the owner once per episode (the same channel as
`fleet-watch`'s notifications, `FLEET_NTFY_URL`), and pages again only after
the PR has been decided and then goes undecidable again.

## Who posts

The token is `FLEET_DEPS_TOKEN_CMD` (default `~/bin/fetch-forgejo-mergebot-token.sh`).
There is no fallback to the implbot token. Before posting, `GET /user` must
return the `agent_merger` login (`mergebot`); anything else refuses the whole
run, posts nothing and pages.

A status is posted only when its state or description differs from the latest
`eunomia/deps` status *mergebot* created on that head. Another account's status
in the same context neither suppresses nor satisfies it.

## The review dispatcher

Per plan 0107 the review dispatcher does not gate on this context: a PR held
on a dependency is still reviewed. ADR-0019 §5 says the dispatcher treats the
context like any other and would not review a held head; the two disagree, and
this unit changes nothing in the dispatcher.

## Making it required — an owner step

1. Install the unit.
2. Run `fleet-deps once` and confirm mergebot's `eunomia/deps` appears on every
   open PR in every watched repository.
3. Only then add `eunomia/deps` to each repository's required status contexts.

Done earlier, a PR with no status yet is unmergeable, and the context blocks
every merge in the fleet until the poster catches up.

## `eunomia/meeting` (plan 0145)

`fleet-deps` also posts `eunomia/meeting`, under `mergebot`, on each open PR's
head, with the same identity check and the same post-only-on-change rule. It is
what shows a lane PR is ready to merge: its tests met the *merged*
implementation and passed.

| PR | state | description |
|----|-------|-------------|
| a lane PR whose meeting passed (`run.outcome` `ran`, at least one test, every test `pass`) | `success` | `met: N passed` |
| a lane PR whose meeting recorded anything else (failure, error, skip, timeout, collection error, zero tests, refusal) | `failure` | the counts or the outcome |
| a PR with no tests marker (no `agent/tests/` branch, no `Tests-for:` line, no changed file under `tests/lane/`) | `success` | `not a lane pull request` |

**Absent** is a state. No status is posted for a lane with no findings document
at its current head, whose implementation is open or merged at another head,
that was listed in `not_run`, or whose findings document cannot be read (logged
once per pass with its path); and none for a PR with a tests marker that
`fleet-meet`'s `lane_problem` refuses. A missing required status blocks the
merge; `pending` would instead stop the review the meeting itself waits on
(ADR-0002 §2), and `failure` for an unread lane would never clear.

The token commands (`FLEET_DEPS_TOKEN_CMD`, `FLEET_MERGE_TOKEN_CMD`) run through
`sh -c`, so a command with arguments works.

### Making it required — an owner step

After `fleet-deps once` has posted `eunomia/meeting` on every open PR, add
`eunomia/meeting` to `main`'s required status contexts on `operator/eunomia`
(`enable_status_check: true`) through hosttools `forgejo-sync-protection.sh`.
Done earlier, a PR that never received the context is held by it.
