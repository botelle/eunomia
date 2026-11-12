# A plan waits for what it needs

*Companion to `docs/feature-plans.md` (the format `depends_on` lives in) and
`docs/plan-triage.md` (why a plan's `status` is evidence, not proof — the
reason the `path:` form below is preferred). Implements plan 0039.*

## The forms

```yaml
depends_on: [0012-session-pid-binding, path:bin/fleet-bind, external:cihost-caddy]
```

| form | satisfied when |
|---|---|
| `0012-session-pid-binding` | that plan's `status` is `done` **on `main`** |
| `path:bin/fleet-bind` | that path exists **on `main`** |
| `path:<owner/repo>:<path>` | that path exists **on that repository's `main`**; the repository must be in `config/repos.conf` |
| `pin:<owner/repo>:<path>` | that path is **committed at the HEAD of that repository's pin** (`~/.local/share/pins/<repo>`); the repository must be in `config/repos.conf` |
| `owner:<script>.sh` | `~/.local/share/pins/hosttools/bin/<script>.sh --check` exits 0 |
| `plan:<owner/repo>:<id>` | that repository's own `plans/<id>.md` reads `status: done` **on its `main`**; the repository must be in `config/repos.conf` and must not be this one (plan 0180) |
| `external:<slug>` | an owner has attested `<slug>` (plan 0180 — see "What `external:` means" below) |

`fleet-watch` checks every entry before dispatching a `ready` plan. The first
unmet entry, in the order written, is the one reported — not the whole set —
because SPEC asks for "blocked, and on what": one legible cause, not a dump of
every cause at once. An entry is checked at read time only; nothing infers a
dependency from overlapping `paths`, shared files, or plan ids mentioned in
prose. If it isn't in `depends_on`, it does not gate dispatch.

## Prefer `path:`

A plan's `status` is a claim about work; a path is the work. `docs/plan-triage.md`
documents plans that are `draft` on `main` with a merged, reviewed pull request
and deliverables their own handoff records as unimplemented, and plans marked
`done` that were built by hand outside the pipeline entirely. Depending on a
plan id trusts the claim; depending on a path checks the thing itself.

Use the plan-id form only when what you actually need is that a *specific plan's
own PR* has landed — for instance, a lease/branch-shape decision recorded only in
that plan's handoff, with no single file to point at. When there is a concrete
deliverable, name it: `path:bin/fleet-bind`, not `0012-session-pid-binding`.

## `path:` on another repository, and `owner:` scripts (plan 0159)

`path:operator/eunomia:bin/fleet-style` is met when that file exists on
`operator/eunomia`'s `main`. Without the `<owner/repo>:` prefix, `path:` means
this repository, as before. A repository not in `config/repos.conf` is a lint
error and, at runtime, unmet with a reason naming it.

`owner:<name>` is an owner step that a script can check. The script is
`bin/<name>` in `operator/hosttools` (name matching `^[a-z0-9][a-z0-9-]*\.sh$`),
reviewed and merged there first, and installed to `~/bin` by its `sync.sh`.
Each cycle, for an unmet edge only, `fleet-watch` runs the pin's copy with
`--check` and nothing else: no other argument, nothing from the plan, an
environment of `PATH`, `HOME` and `LANG`, and a 30-second timeout.

- Exit 0: met (remembered per plan until it dispatches).
- Exit 1: unmet; the page says `owner step: run ~/bin/<name> --apply`.
- Any other exit, a timeout, a missing pin or script, a name that is not a
  regular file inside the pin's `bin/`: unmet, with a reason. Never met.

`--check` is read-only. `--apply` does the step and is run by the owner; the
fleet never runs it. The pin is created by `fleet-pin-advance --create-missing`.

## `plan:` — wait for another repository's plan (plan 0180)

`plan:<owner/repo>:<id>` is met when that plan's front matter reads `status:
done` **on `<owner/repo>`'s `main`** — the plan-id form, for a repository
other than this one. `<owner/repo>` must be in `config/repos.conf` and must
not be this repository (use the bare id for that).

| reason | meaning |
|---|---|
| `unmet` | the repository is not in `config/repos.conf`, or the plan's status is not `done` |
| `dangling` | no plan with that id exists in `<owner/repo>`'s `plans/` on `main` |
| `abandoned` | the plan is `status: abandoned` — this edge will never resolve |
| `unreadable` | `<owner/repo>`'s `plans/` could not be read this cycle |

`fleet-plan lint` errors on a malformed entry, a repository not in
`config/repos.conf`, this repository named instead of the bare id, or a `404`
on `plans/<id>.md` at that repository's `main`. An unreadable forge is not a
lint error — lint does not guess, the same rule `owner:` follows.

## What `external:` means

`external:<slug>` remains for a prerequisite no script can check and no other
repository's plan can name: a service change applied by hand, an infra step,
a decision recorded nowhere a lint can read. **It is satisfied by an owner
attestation, not by editing the plan** (plan 0180, revising the
delete-the-entry rule below): a row for `<slug>` in `fleet.db`'s
`dependency_attestation` table, written only through plan 0181's
authenticated service. `bin/fleet-watch` reads that table itself on every
cycle it is unmet — never cached, so a revoke before dispatch holds the plan
again.

**The slug namespace is global.** One attestation of `cihost-caddy` clears
every plan, in any repository, that depends on `external:cihost-caddy` — the
slug names a fact about the world, not a plan. Name it
`<repo>-<plan>-<fact>` (`deskapp-0001-deploy-report-and-manifests`) when the
fact is specific to one plan, so two unrelated prerequisites never collide on
a short, generic word.

A slug matches `^[a-z0-9]+(-[a-z0-9]+)*$`, at most 80 characters
(`bin/fleet-watch`'s `EXTERNAL_SLUG_RE`). An `external:` entry that is not a
slug — free text, a sentence — is **never met**, even if a row exists with
that exact text: nothing can attest a sentence without copying it exactly,
so `fleet-watch` reports it `not a slug` and `fleet-plan lint` refuses it at
`ready` or `draft` (not at `done` or `abandoned`, whose edges never gate
dispatch again). Rewrite it as a slug, as `owner:` if a script can check it,
or as `plan:` if it is really another repository's plan landing.

| reason | meaning |
|---|---|
| `unattested` | no attestation row for this slug yet |
| `not a slug` | the entry is free text — rewrite it |
| `store unreadable` | `fleet.db`, or its `dependency_attestation` table, could not be read |

**Not for a step a script can check.** Write that as `owner:` and let the
fleet read it. **Not for another repository's plan.** Write that as `plan:`.

## `pin:` — wait for the code the fleet runs (plan 0167)

`pin:<owner/repo>:<path>` is met when `git -C <pin> cat-file -e HEAD:<path>`
succeeds. It reads the pin's committed HEAD, never its working tree, and never
fetches or advances anything: moving a pin is `fleet-pin-advance`'s job.

Use `pin:` when the dependent **runs** the other repository's code (a runtime
call through `~/.local/share/pins/…`). Use `path:<owner/repo>:<path>` when it
only needs the code to exist (tests against `main`, a file copied at build time).

| reason | meaning |
|---|---|
| `refused` | malformed entry, a `..` segment, or a leading `/` in the path |
| `no pin` | the repository has no pin in `pins()` |
| `pin unreadable` | the pin directory is missing or not a git checkout, or git failed or took over 10 s |
| `not on pin` | the path is not at the pin's HEAD |

Nothing unreadable is ever treated as met. `fleet-plan lint` errors on a
malformed entry or a repository not in `config/repos.conf`, and warns when the
repository has no pin today.

## Overlapping plans wait (plan 0171)

Nothing goes in `depends_on` for this. Before dispatching a plan, `fleet-watch`
holds it while another plan in the same repository is **in flight** and the two
**overlap**, so the second branches from a `main` that already holds the first.

- **In flight:** a *live, non-orphaned* dispatch of that plan whose own status
  on `main` is not `done`/`abandoned`, or a pull request carrying `Plan: <id>`
  is open (a re-land counts). It stops being in flight when its last such pull
  request is merged or closed and no dispatch is running.
- **An orphaned lease is never, by itself, in flight for this check** (plan
  0179 D3, ADR-0029) — whatever its own plan's status, and whatever the
  dispatch scan's own in-flight test (`plan_in_flight()`, unchanged) still
  makes of it for re-dispatching THAT plan. Nothing is running on a dead
  orchestrator's lease, so the overlap it would still leave is the other
  plan's own open pull request and its review, not a lease nobody is using.
  **Any** lease of a plan whose status on `main` reads `done` or `abandoned`
  is the same — live or not — because that plan can no longer be running
  work that would conflict. This changes only the OVERLAP question: it
  releases nothing, and a dead run's lease stays exactly as the sweep left
  it. If that dead run is later `respawn`ed or resumed, its branch predates
  whatever the un-held plan dispatched in the meantime on the shared paths,
  and may conflict with it — that run then fails or rebases like any other
  stale branch, paged like any other failure.
- **Overlap:** some entry of one plan's `paths:` and some entry of the other's,
  leaving out each plan's own `plans/<id>.md`, can name the same file: equal,
  matching as a glob either way, or one a directory prefix of the other.
- **Reported** as `plan-blocked` with `form` `overlap` and `dependency`
  `overlap:<id>`; the detail names the first shared path and the pull request.
  One page per plan, as for any block. The hold clears by itself.
- Two `ready` plans that overlap: the lower id dispatches, the other is held.
- Only `paths:` is read; a plan already running is never stopped.

## Reading what is blocked

`fleet-watch --dry-run` prints every plan it would skip this cycle and why,
without assigning a lease, spawning anything, or paging. For the durable
record, a blocked `ready` plan produces a `plan-blocked` event **on
transition** — the first cycle it becomes blocked, or when the reported reason
changes (SPEC.md "Event record"):

```
fleet-events --type plan-blocked
```

`detail` carries `plan`, `dependency` (the exact `depends_on` entry, or
`pr:#<n>` for a stuck form below), `form` (`plan` / `path` / `pin` / `owner` /
`external` / `overlap` / `built` / `rejected`), and `reason`:

| reason | means |
|---|---|
| `unmet` | not yet true — the dependency plan isn't `done`, the path doesn't exist yet, or a `plan:` names a repository outside `config/repos.conf` |
| `abandoned` | the dependency plan is `status: abandoned` — this edge will never resolve; edit it |
| `unattested` | an `external:<slug>` has no owner attestation yet (plan 0180) |
| `not a slug` | an `external:` entry is free text, not a slug — `fleet-plan lint` refuses this at `ready`/`draft` |
| `store unreadable` | `fleet.db`'s `dependency_attestation` table could not be read |
| `dangling` | the plan id in `depends_on` (bare, or after a `plan:<owner/repo>:`) matches no plan in that repo — a `fleet-plan lint` error for the bare form, not just a dispatch block |
| `unreadable` | a `plan:<owner/repo>:<id>` entry's repository could not be read this cycle |
| `orphan` | a `path:` entry does not exist on main, AND no `ready`/`draft` plan anywhere this cycle read leases it (plan 0211 D1b) — see below |
| `built-not-done` | a plan's newest marked PR is merged and its body carries no `Plan-partial:` line, yet the plan still says `ready` (plan 0211 D1) |
| `partial` | the merged PR's body carries `Plan-partial: <id>` — `detail` is the `Remainder:` text from the next line, or `no Remainder: line` |
| `closed-unmerged` | the plan's newest marked PR closed without merging, and no fresh `redispatch` comment followed |

**`orphan` vs `unmet` for a `path:` entry.** A `path:` dependency that does
not exist on main is `unmet` when *some* `ready`/`draft` plan this cycle read
leases a path overlapping it (the lease comparison `_paths_overlap` uses, not
`fnmatch` or `startswith`) — the producer is simply not merged yet. It is
`orphan` when NO plan leases it. The producer index is built once per cycle
from the plans/ listings the scan loop actually reads that cycle; a repo
never read this cycle (held, skipped, unreadable, not in `repos.conf`, or
past a cap break) is **unknown**, which reads as `unmet`, never `orphan` — a
false orphan would send the owner to re-plan work another repo is about to
build. An orphan is reported, never repaired: fleet-watch edits nothing.

**The stuck forms (`built-not-done` / `partial` / `closed-unmerged`)** are a
`ready` plan whose newest marked PR is merged or closed and the owner has not
freshly `redispatch`ed it (plan 0086). Unlike every other reason above,
`fleet-watch` does **not** page these itself — `mopsus`'s stuck-plan sweep
does, with a handle (`docs/mopsus.md`), so a stuck plan gets one page with
three concrete actions rather than a bare "Plan blocked" with none. `orphan`
is not paged by either program: it is a re-plan for the owner to pick up
from the lynceus view, not something to repeat on a phone.

There is no `plan-unblocked` event: a plan that clears its block dispatches
normally, which already produces `plan-dispatched` — that is the exit signal.

A blocked plan also pages once (except the stuck forms and `orphan`, above),
through the same one-shot discipline as every other stalled-dispatch
condition in this module — not once per cycle, and not once per reason: one
page per plan, cleared when the plan actually dispatches.

## `blocked.json`

At the end of every cycle that reached the per-repo scan loop (not under
`--dry-run`), `fleet-watch` writes `<fleet-dir>/blocked.json` atomically,
whole, every time — lynceus plan 0051 reads this file, never fleet-watch's
own return value or stderr:

```json
{"v": 1, "written_at": "2026-11-06T12:00:00Z", "cycle_complete": true,
 "held_repos": [{"repo": "operator/x", "gate": "vendor:muse",
                 "until": "2026-11-09T00:00:00Z", "seat": "implementer#1 sub-codex"}],
 "skipped_repos": [{"repo": "operator/y", "why": "main-red"}],
 "blocked": [
   {"repo": "operator/eunomia", "plan": "0196-…", "dependency": "overlap:0176-…",
    "form": "overlap", "reason": "overlap", "detail": "…",
    "since": "2026-11-05T21:00:00Z", "pr": 1334, "handle": null, "orphans": []}
 ]}
```

- `blocked`: one entry per `plan-blocked`-shaped report this cycle (dependency
  blocks, overlap blocks, and the stuck forms above), with the same
  `dependency`/`form`/`reason`/`detail` the event carries.
  - `since`: when this exact `dependency|reason` was first seen — stable
    across cycles until the blocker changes.
  - `pr`: the PR number behind an overlap or stuck block, else `null`.
  - `handle`: the mopsus handle paging a stuck form, read from
    `mopsus.state.json`, or `null` before mopsus has paged it.
  - `orphans`: every orphaned `path:` entry in `depends_on`, named exactly as
    written (not just the first blocking edge) — `[]` for every other entry.
- `held_repos` / `skipped_repos`: the repos a dispatch cycle passed over this
  cycle and why, so a reader can tell "nothing blocked" from "not scanned".
- `cycle_complete`: `false` when the dispatch cap cut the scan short, so a
  reader knows the list may be incomplete.

The shape is kept exactly; a future change only adds fields. A cycle that
returns before reaching the scan loop (broker down, no token, orchestrator
not ready, `repos.conf` unreadable) leaves the previous file in place —
`written_at` shows how old it is.

## `fleet-plan lint`

- A `depends_on` entry naming a plan id that exists nowhere in the repo is a
  lint **error** (a dangling id), regardless of the dependent's own status.
- A cycle in the plan-id dependency graph is a lint **error**, naming every id
  in the cycle.
- A `depends_on` entry naming a plan that is `status: abandoned` is a lint
  **error** when the dependent can still dispatch (`ready` or `draft` — it
  never will resolve, and a session reading the plan would be told to wait for
  something that isn't coming) and a **warning** when the dependent is itself
  `done` or `abandoned` (the edge is history the plan carried before its
  dependency was abandoned, not a live hazard — failing lint on it would break
  every already-shipped plan that happens to depend on later-abandoned work).
- `path:`, `pin:`, `owner:`, `plan:` and `external:` entries carry no LOCAL
  plan status, so neither the cycle check nor the abandoned check applies to
  them — they are not nodes in this repository's plan-id graph (`plan:` is
  treated the way a cross-repository `path:` is); the bare-id dangling check
  applies only to a plan-id entry naming a plan in THIS repo.
- `plan:<owner/repo>:<id>` is a lint **error** when malformed, when
  `<owner/repo>` is not in `config/repos.conf`, when `<owner/repo>` is this
  repository (use the bare id), or when `plans/<id>.md` is absent on that
  repository's `main` — an unreadable forge is not an error, the same `owner:`
  rule.
- `external:<slug>` is a lint **error**, at `ready` or `draft` only, when the
  text after `external:` is not a slug (plan 0180).

`fleet-plan lint` reads `depends_on` in both the inline (`[a, b]`) and block
(`depends_on:` / `- a` / `- b`) shapes that front matter is written in — a
reader that understood only one silently treated the other as an empty list,
which is a dependency dropped, not merely unenforced.
