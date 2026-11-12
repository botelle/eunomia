# fleet-rebase — rebase a stale pull request from a comment

`bin/fleet-rebase` (plan 0115) rebases an open pull request's head branch onto
`main` in a fresh temporary clone and pushes it, so CI runs again on the new
head. It is the explicit request from a person that `fleet-stalled` (plan 0060)
leaves as the fix for a `stale-base` red.

    fleet-rebase <repo> <pr> [--dry-run]     one pull request, by hand
    fleet-rebase <repo> <pr> --reland [--dry-run]   an approved one (below)
    fleet-rebase --sweep [--dry-run]         the comment gesture

## When to use it

Use it when a pull request is red only because `main` was fixed after the
branch was last pushed. Nothing re-runs CI on a branch nobody pushes.

Do not use it to get a new review (it dispatches none), to resolve a conflict
(it refuses and names the paths), or on a pull request an orchestrator is still
working (it refuses and names the lease).

## The comment gesture

The owner posts a line reading `rebase` (optionally in backticks) on the pull
request. The sweep acts on it when all of these hold:

- the comment is from the user `FLEET_OPERATOR_UID`, matched by id, never by login;
- it was posted since the head commit's **committer** time. The author date is
  no use: a rebase preserves it, so the request that caused the rebase would
  never age out;
- no reply from the author account carries a line `rebase: <comment id>`.

Each comment it acts on gets exactly one reply, starting with `rebase: <id>`,
giving the old and new head or the refusal. "Please rebase it" does not trigger
it: the instruction must stand alone on its line.

## Refusals, and what to do instead

Every refusal names the check that failed. Nothing is pushed on any of them.

| Check | Meaning | Instead |
|---|---|---|
| 1 open, base, fork | merged or closed, base is not `main`, or the head is in a fork | not this verb's to push |
| 2 approval | an undismissed `APPROVED` review exists, or the reviews could not be read in full | the pull request is frozen; work the change as a follow-up pull request against `main` |
| 3 session | a live `branch` lease holds the branch, a live dispatch lease holds the plan, or the ledger is unreadable | wait for the session, or release its lease if it is dead. A live orchestrator refuses even a `stale-base` red, because its worktree is on the branch (ADR-0005) |
| 4 already on main | `main` is an ancestor of the head | nothing to do; the red is real |
| 4 conflict | the rebase conflicts | rebase by hand; the reply lists the paths |
| 4 plans-only | `main` moved only in `plans/` | see below |
| push | the lease push was refused (the branch moved), or ssh failed | read git's message in the reply and ask again |

Approval is checked again immediately before the push, and the push is
`--force-with-lease` against the head SHA that was checked.

### Plans-only

`.forgejo/workflows/ci.yml` judges a force-push by the rebase delta (its
comment, case (c)). If `main` moved only in `plans/`, that delta is plans-only,
CI's path filter skips the `CI / tests` suite, and the new head carries no such
status (`Plans / lint` still runs). That is worse than the red it replaces,
because `dispatch-review` treats a missing status as honest absence. The verb
refuses instead. It also refuses an empty delta (the tree would be unchanged).

A `rebase` comment older than the head commit is never acted on and gets no
reply: the head moved after you asked, so repost `rebase` if you still want it.

## Why workflow dispatch is not a rerun

Checked on 2026-09-30 (talos#137 and eunomia#713; see plan 0115 D0):

- the forge has no rerun endpoint in 15.0.2 or 16.0;
- a dispatched run writes `(workflow_dispatch)` statuses beside the red
  `(push)` one, which stays on that commit for good, so the pull request stays
  red and so does the review dispatcher's CI gate;
- the web Re-run button is a browser session route, which a token cannot call.

A new push is the only thing that replaces a stale red status. An empty commit
does not work either: it changes no paths, so the path filter skips it.

## Adding `rebase` to Minos

Owner step, after the unit is installed (before then the button posts a comment
nothing reads): add `rebase` to `MINOS_VERBS` in the edgehost quadlet, as described
under "Advanced verbs" in the pr-proxy repository's runbooks, and update that
runbook's "configure only verbs the fleet reads" line to name `rebase`.

## `reland` — an approved pull request that no longer merges (plan 0129)

`rebase` refuses an approved pull request (check 2), and so does
`forgejo-safe-push.sh`: it is frozen, because it is merged the moment the
approval lands, and a push either races the merge or silently discards the
approval. So when an approved pull request conflicts with `main` or needs its
newer commits, nothing could move it. The way out was done by hand three times
on 2026-10-07: talos#145 → #148, eunomia#693 and #794 → #804, and
hosttools#43 → #50. Each was a fresh branch off `main`, the commits replayed, a
new pull request, a new review, and the old one closed with a pointer.
`reland` is that route as a verb.

| Use | When |
|---|---|
| `rebase` | not approved; red only because `main` moved |
| `reland` | **approved**, and it conflicts with `main` or needs `main`'s commits |

The owner comments a line reading `reland` (same rules as `rebase`: the owner's
uid, newer than the head commit's committer time, one reply per comment). Or run
`fleet-rebase <repo> <pr> --reland [--dry-run]`.

In a fresh clone the verb creates `<head-branch>-relanded-<n>` from
`origin/main` (first free `n`), replays the pull request's commits with
`git rebase --onto`, pushes the new branch with `forgejo-safe-push.sh` (it has
no pull request yet, so the guard allows it), and opens the replacement with
`forgejo-open-pr.sh`. The replacement's body starts `Follows: #<pr>`, copies the
old body, and carries **no** `Plan:` marker, which stays on the original. The
reply on the old pull request is `reland: <comment id> → #<new>`. The old one
stays open, and nothing is ever pushed into it.

When the replacement is merged, the next `--sweep` closes the old pull request
with one comment naming the merge. If the replacement is closed unmerged, it does
nothing.

Refusals (nothing is pushed on any of them):

| Check | Meaning | Instead |
|---|---|---|
| 1 open, base, fork | as for `rebase` | not this verb's |
| 2 not approved | no undismissed approval | use `rebase` |
| 3 session | a live branch or dispatch lease, or an unreadable ledger | as for `rebase` |
| 4 nothing to re-land, already on main | the head is already in `main`, or contains it | nothing to do |
| 4 conflict | the replay conflicts; the reply lists the paths | needs a session, below |
| push | the guard or the push was refused | read the message in the reply |
| open | the branch was pushed but the pull request was not opened | open it from the branch named in the reply |

A conflict is never resolved mechanically: it is a judgement about two changes.
To re-land with conflicts, instruct the steward to "rebase onto main on a fresh
branch and open a replacement PR". It can, because the fresh branch is not
frozen. This sentence sits beside the steward's frozen-branch ack (plan 0124),
whose text is not changed here.

Owner step: add `reland` to `MINOS_VERBS` beside `rebase`, and note it in
pr-proxy's verbs runbook. Until then the verb works from a comment typed in
Forgejo.

## Operating notes

- One instance at a time: both modes take a non-blocking lock first, and a
  second instance exits non-zero.
- The sweep covers the `dispatch` repositories of `config/repos.conf` (plan 0134)
  and refuses to start when that list cannot be read or `FLEET_OPERATOR_UID` is
  empty. Install steps and the environment are in the unit's header,
  `launchd/org.eunomia.fleet-rebase.plist`.
- Rebased commits keep their authors. The committer is the author role's commit
  identity, never the merger's.
- No event type is added: the reply comment is the record.
