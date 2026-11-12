# `fleet-merge` — mergebot's merge (plan 0108, ADR-0019 §4)

`bin/fleet-merge once` merges, as `mergebot`, the two kinds of PR ADR-0019 §4
allows on an **autonomous** repository: a `src` PR under a live plan, and that
plan's `tests` PR. It is a program, not a model: every check reads forge or
file state, so any merge can be explained by re-running the checks. The
launchd unit `org.eunomia.fleet-merge` runs it every 60 seconds.

The token is `FLEET_MERGE_TOKEN_CMD` (default `~/bin/fetch-forgejo-mergebot-token.sh`),
with no fallback. `GET /user` must return the `agent_merger` login or the run
refuses, merges nothing and pages.

## A `src` PR: ten checks, in this order

The first failure is the one reported — except check 4, which does not stop
evaluation; see below.

1. `fleet-config get autonomy --repo <r>` is `autonomous`. A read failure
   stops the repository and pages. A `critical` repository is not touched at
   all, not even to log.
2. The base is `main`; the body has exactly one line-anchored `Plan: <id>`
   (a longer id never satisfies a shorter one); `plans/<id>.md` is at
   `status: ready` on `main`. A spent (`done`) plan authorises nothing.
3. Under `plans/**` the PR changes at most `plans/<id>.md`, with no rename,
   and that file differs between base and head only in the front-matter
   `status:` line, `ready` to `done`. Compared by content, since the file
   list carries no patch.
4. Nothing under `docs/adr/**` or `tests/lane/**`, and none of the files in
   `ENFORCING` (a constant in `bin/fleet-merge`): the merger itself,
   `fleet-deps`, `fleet-watch`, `fleet-depsd`, `fleetlib.py`, `fleetforge.py`,
   `fleet-config`, `fleet-install-hooks`, `fleet-secret-guard`,
   `config/secret-patterns.conf` and all three launchd units. A hit here does
   **not** stop evaluation (plan 0214): checks 5–10 still run. If one of them
   fails, that is the refusal reported, as if check 4 had never fired. If
   every one of them holds, the PR is refused at check 4 anyway — ADR-0019 §4
   reserves this merge for the owner — but that refusal also pages him once
   per PR head (see "The owner's page", below).
5. The author is none of the `agent_merger`, `reviewer` or `second_reviewer`
   accounts (from the role table, plan 0106).
6. The `reviewer` and `second_reviewer` accounts (two different accounts) each
   have a latest submitted review at the head that is `APPROVED`, not stale
   and not dismissed. A later non-approving review from either on the head
   blocks.
7. The latest `eunomia/deps` status on the head is `success` and was created
   by the `agent_merger` account.
8. Every context required on `main` other than `eunomia/deps` is `success` at
   the head. A missing one is not green, and a `main` that requires no CI
   context at all is refused: CI cannot be "green" on nothing.
9. `fleet-meet`'s findings document for this plan and head
   (`<work root>/meetings/<plan>/<head-sha12>/findings.json`) exists, validates
   against `config/meeting-findings.schema.json`, names this repo, plan, PR and
   head, records at least one test, every test `pass`, and `all_passed` true.
   A `fail`, `error`, `skip`, a missing document or another head's all refuse.
10. Forgejo reports the PR `mergeable`.

If all hold, it calls `fleetforge.merge_pull` with the checked head SHA as
`head_commit_id`, so a push in between makes the forge refuse.

## The owner's page

A `src` PR refused only at check 4 (protected paths), with checks 5–10 all
holding, pages the owner once per PR head — through the same channel as an
undecidable read (`fleet-watch notify`): title `Ready for your merge`, body
`<owner/repo>#<n> (plan <id>) at <head12>: touches <name>. Every other check
holds.` followed by the PR's web URL. A later pass at the same head sends
nothing more; a new head (a push) pages again once it, too, clears checks
5–10. A failed delivery leaves no marker, so the next pass retries. The page
is the only new output here: nothing is merged, commented on, labelled or
auto-merged on his behalf. Every other refusal — including a later check
failing on a protected-path PR, and check 3 (plan diff), which is always the
implementer's to fix — never pages.

## A `tests` PR

A body with a `Tests-for: <id>` line selects this list: autonomous repository;
base `main`; exactly one `Tests-for` line (and no `Plan:` line) naming a plan
on `main` at `ready` or `done`; every changed file under `tests/lane/**` with
no rename; the author is the lane wrapper's account (the `author` role) and
not a reviewer or `mergebot`; both reviewers approved the head; CI green as in
check 8; mergeable. Same `head_commit_id` merge.

## What it never touches

Plan, ADR, docs and deploy PRs; unplanned PRs; any PR that adds or edits a
plan beyond its own `ready`→`done` flip; anything under `docs/adr/**` or
`tests/lane/**` in a `src` PR; the enforcing files; every PR on a critical
repository. It never rebases or pushes, never retries a failed merge in the
same pass, never schedules an auto-merge (`merge_when_checks_succeed`), and
never calls a model.

## What it reports

Every merge and every refusal past check 1 is one line on stdout (the unit's
log): the PR, the head SHA and the deciding check. A PR that stays blocked on
the same check at the same head is logged once. An undecidable read or an
identity failure pages, once per episode, through `fleet-watch notify`; so
does a `src` PR refused only at check 4 with checks 5–10 all holding (see
"The owner's page", above), once per PR head.

## Making a repository autonomous — an owner step

```
fleet-config set autonomy autonomous --repo <owner/repo>
```

(plan 0105). Per ADR-0019 §2 the owner also adds `mergebot` to the repository's
merge whitelist, requires at least two approvals and requires `eunomia/deps`
(after `fleet-deps` has posted it on every open PR, `docs/deps-status.md`).
Making it critical again removes `mergebot` from the whitelist.

eunomia, talos and hosttools hold the code that enforces this ADR. They
should stay `critical` unless the owner decides otherwise.

## Known gap

Plan 0102 ran a lane only after a *human* approved the lane PR. Fleet-meet now
also accepts the two reviewers' no-tool approvals on an autonomous repository
(its `reader: reviewers` path, plan 0138); the owner should confirm that on a
real lane before relying on check 9 to pass without them.
