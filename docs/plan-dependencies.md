# A plan waits for what it needs

*Companion to `docs/feature-plans.md` (the format `depends_on` lives in) and
`docs/plan-triage.md` (why a plan's `status` is evidence, not proof — the
reason the `path:` form below is preferred). Implements plan 0039.*

## The three forms

```yaml
depends_on: [0012-session-pid-binding, path:bin/fleet-bind, external:cihost-caddy]
```

| form | satisfied when |
|---|---|
| `0012-session-pid-binding` | that plan's `status` is `done` **on `main`** |
| `path:bin/fleet-bind` | that path exists **on `main`** |
| `external:<text>` | **never automatically** — a human attests, by deleting the entry |

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

## What `external:` means

Some prerequisites are not plans and never will be: a service change in another
repo, an infra step applied by hand on a host, a decision recorded nowhere a
lint can read. `external:<text>` names one. It **blocks dispatch exactly like
the other two forms** — this is not merely a note for a human to notice, it is
enforced — and it can never be satisfied by anything automatic, because nothing
in this repo can observe whether it is true.

**The attestation is deleting the entry.** A human who has judged the
prerequisite met removes the `external:...` line in a reviewed pull request —
no flag file, no override command. The commit that removes it is the record of
who judged it satisfied and when; `git log -p` on the plan file is the audit
trail.

**Not for a step the merge already holds.** Under the rule in
`docs/feature-plans.md` (2026-09-11), a plan waiting on an owner step is filed at
`ready` and held by leaving its pull request unmerged until the step is done.
That plan must not also carry the step as `external:`. Its merge is already the
attestation, and the merged pull request is the record. An `external:` line on
top would demand a second pull request to delete it, leaving the plan blocked
between the two. Use `external:` for a prerequisite no merge holds — for example,
a plan merged ahead of its prerequisite on purpose.

## Reading what is blocked

`fleet-watch --dry-run` prints every plan it would skip this cycle and why,
without assigning a lease, spawning anything, or paging. For the durable
record, a blocked `ready` plan produces a `plan-blocked` event **on
transition** — the first cycle it becomes blocked, or when the reported reason
changes (SPEC.md "Event record"):

```
fleet-events --type plan-blocked
```

`detail` carries `plan`, `dependency` (the exact `depends_on` entry), `form`
(`plan` / `path` / `external`), and `reason`:

| reason | means |
|---|---|
| `unmet` | not yet true — the dependency plan isn't `done`, or the path doesn't exist yet |
| `abandoned` | the dependency plan is `status: abandoned` — this edge will never resolve; edit it |
| `external` | a human attestation is outstanding — see above |
| `dangling` | the plan id in `depends_on` matches no plan in this repo — a `fleet-plan lint` error, not just a dispatch block |

There is no `plan-unblocked` event: a plan that clears its block dispatches
normally, which already produces `plan-dispatched` — that is the exit signal.

A blocked plan also pages once, through the same one-shot discipline as every
other stalled-dispatch condition in this module — not once per cycle, and not
once per reason: one page per plan, cleared when the plan actually dispatches.

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
- `path:` and `external:` entries carry no plan status, so neither the cycle
  check nor the abandoned check applies to them; the dangling-id check applies
  only to plan-id entries.

`fleet-plan lint` reads `depends_on` in both the inline (`[a, b]`) and block
(`depends_on:` / `- a` / `- b`) shapes that front matter is written in — a
reader that understood only one silently treated the other as an empty list,
which is a dependency dropped, not merely unenforced.
