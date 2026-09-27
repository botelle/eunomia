# ADR-0002 — `candidate-green`: CI proves the branch, something else must prove the merge

- **Status:** Accepted
- **Date:** 2026-09-02
- **Deciders:** the operator (owner/merger)
- **Relates:** `PROVENANCE.md` §2.2 (the queue never rewrites a PR branch; integration
  is proven on a candidate) and §5 (the closed condition set).
  **[ADR-0001](0001-orchestrator-workspace.md)** supplies the worktree mechanism this
  builds on.

## Context

`PROVENANCE.md` names an admission condition and defines it, in §4.5's
`Merge-provenance` prose, in exactly these words:

> `candidate-green` = CI passed on the base ⊕ PR candidate

(The condition *token set* is §5's; the sentence quoted above sits in §4.5, which
points at it. An earlier draft of this ADR attributed the sentence to §5.)

and §2.2 says how the candidate is produced — "built and tested in a scratch ref",
never by pushing an update onto the PR branch, because rewriting a reviewed branch
dismisses an approval for a reason the reviewer would not recognise (§4.6).

**Nothing satisfies that condition today.** Forgejo publishes only the head ref:

```
$ git ls-remote origin 'refs/pull/24/*'
755a5b9…  refs/pull/24/head        ← the only ref
```

No `refs/pull/N/merge`, on `eunomia` or on `agent-bus` (`/5/head`, `/50/head`,
`/51/head`). This is the difference from GitHub Actions, where `pull_request` checks
out a merge ref — the assumption that carried over here, and it is wrong.

The two workflow runs on eunomia #24 confirm it from the other direction: both carry
`head_sha 755a5b9fd2a2…`, one `event: push` and one `event: pull_request`. Same SHA,
same tree, two runs. That identity is why they raced on the self-hosted `cihost`
runner, and it means **neither trigger tests the merge result.**

So the double CI run and the missing admission condition are one question, not two.

## Decision

**Split them: CI proves the branch on push; a separate builder proves the candidate.**

### 1. CI triggers on `push` only

`pull_request` is removed from `.forgejo/workflows/ci.yml`. Since both events build
the identical tree, nothing about *what is tested* changes — but:

- **usually** one run per SHA. This removes the *duplication we observed* — two
  events on one commit — but it is not a proof that no two runs can ever contend:
  pushing an already-built commit to a second ref fires another `push`, and two
  features in flight on one self-hosted runner contend regardless of trigger. The
  concurrency group is therefore complementary, not superseded, and dropping it is
  out of scope here;
- `main` keeps CI coverage after a merge, which a `pull_request`-only trigger drops
  entirely;
- a run *starts* earlier — at push rather than at PR-open — which gives the CI gate
  in `dispatch-review.py` a head start. It does **not** mean the gate never waits:
  that tool treats a *pending* status as a hard refusal
  (`"CI is still running on <sha> — wait for it"`), so a review dispatched
  seconds after a push still refuses. The gain is directional, not absolute; an
  earlier draft of this ADR claimed the absolute version.

### 2. `bin/fleet-candidate` builds and tests base ⊕ head

For a given PR it: attaches a scratch worktree (ADR-0001's backing clone), merges the
**current base tip** into the PR head *in that worktree only*, runs the repo's test
command, and posts a commit status. It never pushes, never writes to the PR branch,
and never merges into `main` — §2.2's rule is enforced by the tool having no push
path, not by instruction.

- **Conflict** → status `failure`, description naming the conflicting paths. A PR
  that cannot be merged cleanly has failed the condition; it has not errored.
- **Status context** is `candidate`, distinct from `tests`, so the two facts stay
  separately legible.

**Known interaction, accepted:** `dispatch-review.py` reads the *combined* status
for a head SHA and refuses to spend a review round on any failing context. A
`candidate: failure` therefore blocks review of that PR. For a genuine conflict
that is arguably right — there is no point reviewing a PR that cannot merge — but
it is a behaviour change to a tool that lives outside this repo
(`~/agent/dispatch-review.py`, still unversioned pending the talos bootstrap), so
it is named here rather than silently introduced. If it proves wrong, the fix is a
context allowlist in that tool, not a weaker status here.

### 3. The status records the base it was built against

A commit status attaches to the head SHA and therefore cannot, by itself, express
"given base X". If `main` moves, a green `candidate` status remains green while
describing a merge that no longer exists.

So the status description carries the base SHA (`base=790c02f`), and **the merge
queue must compare it against the current base tip and treat a mismatch as
unsatisfied**.

**No such consumer exists yet**, and this ADR must not pretend otherwise — by its
own argument, an unchecked green is worse than an absent one. So until a queue
performs that comparison, `candidate` is **advisory**: a status a human reads
alongside the base SHA it names, not a satisfied admission condition. `fleet-candidate`
is on-demand for the same reason; nothing schedules it, so nothing can accumulate
stale greens unattended. `candidate-green` becomes *computable* here and becomes
*binding* when the queue lands.

### 4. Who may post it

The `candidate` status is posted with the same Forgejo token the poster already
holds, and the tool must run as a **non-admin identity** — the standard this repo
already sets by having `fleet-watch._child_env()` strip `FLEET_TOKEN_CMD` from
every child so an agent can never reach the admin helper. `candidate-green` gates
merge admission, so the ability to post it is the ability to satisfy a merge
condition: it belongs to the same least-privilege argument as the poller push
identity (agent-bus ADR-0002), not to whoever happens to have a token. v0 runs it
by hand as the operator; a scheduled runner needs its own account first.

## Consequences

- `candidate-green` becomes computable, and §5's condition set stops containing a
  token nothing produces.
- Two statuses per PR (`tests`, `candidate`) with different meanings: the branch is
  sound; the merge is sound. A reviewer can tell which one failed.
- Rebuild cost on every base move. Bounded in practice by how rarely `main` moves in
  this fleet, and the queue's staleness check makes the cost visible rather than
  letting a stale green through.
- Branches never PR'd now consume CI. Accepted: in this fleet nearly every branch
  becomes a PR, and the cost is one run that would have happened anyway.
- Something must invoke the builder. v0: on demand and at queue admission. A watcher
  trigger is deliberately out of scope until the queue itself exists.

## Alternatives considered

**Keep `pull_request` and drop `push`.** The intuitive reading, and the one this ADR
started from — it survives only while you believe `pull_request` builds a merge ref.
It does not, and it additionally leaves `main` untested after every merge.

**Have CI itself merge the base before testing.** Puts the candidate build inside the
runner, so no second tool. Rejected: it makes every CI run depend on the base tip at
run time, so a green tells you neither what was tested nor when, and the workflow
file — which lives in the PR — becomes able to alter its own admission check.

**Require branches to be current with `main` before merge.** The GitHub
"branch must be up to date" model. Rejected: the only way to make a branch current is
to push to it, which is exactly what §2.2 forbids the queue from doing.

## Definition of done

- `ci.yml` fires once per SHA; a PR push produces exactly one run.
- `fleet-candidate` on a clean PR posts `candidate` success carrying the base SHA;
  on a conflicting PR posts failure naming the paths.
- It never pushes: asserted against a stub that records zero push invocations.
- A candidate built against a superseded base is reported stale, not green.
- The PR branch's head SHA is unchanged after a candidate build.
