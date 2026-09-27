# Deploying a merge — `fleet-deploy` and the `homefleet` provider

*Plan 0087, implementing [ADR-0015](adr/0015-an-environment-is-a-manifest-bound-to-a-provider.md)
for `dev`.* A merge changes nothing until a pin moves and the service restarts.
`bin/fleet-deploy` does both, verifies the result, and puts the previous
version back if the check fails. Every step is on the ledger.

```
fleet-deploy validate <manifest.json>
fleet-deploy run   [--binding dev:<name>] [--dry-run]
fleet-deploy retry --binding dev:<name> [--sha <sha>]
```

## Two identifiers, one component

| | value | where it appears |
|---|---|---|
| lease holder (session id) | `fleet-deploy` | the `service` lease; matches `fleetlib._SID_RE` |
| ledger actor | `svc:fleet-deploy` | every `deploy-started` / `deploy-verified` |

## The manifest — `deploy/manifest.json`, in the service's repository

```json
{"v": 1, "name": "lynceus",
 "health": {"path": "/healthz", "expect": "\"service\":\"lynceus\"", "timeout_s": 60},
 "deps": ["api/requirements.txt"],
 "secrets": ["kv/lynceus/token"]}
```

- `v` must be `1`. Unknown keys, at any level, are refused.
- `name` must equal the binding's name.
- `health.expect` is **required** and non-empty. A status code alone is never a
  health check: a Cloudflare Access page and a login page both answer 2xx.
  `timeout_s` is optional (default 60, at most 600).
- `deps` lists files whose change needs host preparation (below).
- `secrets` lists keyvault references. Informational only; the service's own
  start wrapper still fetches them.
- `artifact` and `start` are optional in v1. For `homefleet` the artifact is the
  repository at the deployed SHA, so `artifact` may only be `"repo"`; `start`,
  if stated, is the argv and must equal the unit's `ProgramArguments`
  (launchd only). A disagreement is a refusal, not a guess.
- No field may name a host, address, unit label or provider (ADR-0015 §1).
  `validate` checks values for addresses, URLs and reverse-DNS labels.

## The binding — `config/environments.conf`

One line per binding, in `services.conf`'s `key=value` style:

```
bind: dev:lynceus provider=homefleet repo=operator/lynceus service=opshost:lynceus pin=/Users/operator/.local/share/pins/lynceus base_url=http://192.0.2.5:8795
```

`service=` must be enrolled in `config/services.conf`; only `dev` and
`homefleet` exist. A malformed line stops the run and names the line. A missing
file means no bindings. `fleet-deploy` acts only on bindings whose host is the
machine it runs on.

A pin named by any binding is **deploy-owned**: `fleet-pin-advance` refuses it
by name even if it is in `FLEET_PINS` (one mover per pin), while
`fleet-pin-watch` still reports its drift.

## The cycle

`run` holds a single-instance `flock` for its whole duration. For each binding:

1. `git fetch` the pin, read `origin/main` as the target. The fetch rides an
   ssh ControlMaster that is already up (`ssh -O check`) and never dials a new
   one; with none up the binding is skipped this cycle.
2. **Skip an attempted target.** A `deploy-started` for this binding at the
   target with no `deploy-verified` means it was tried and failed. Nothing
   happens until `main` moves to a different SHA.
3. If the pin is not at the target, check that: the manifest at the target
   validates; the target descends from the pin's HEAD; the pin's tree is clean;
   the broker vouches for `main`; and no `deps` file differs between HEAD and
   the target. A failure is refused and paged **once per (binding, target SHA,
   reason)**. A `deps` change pages "host preparation needed" and the pin does
   not move: fleet-deploy never installs dependencies.
4. Claim the `service` lease. If refused, nothing changes and the next cycle
   tries again. With the lease held: emit `deploy-started`, `checkout --detach`
   the target, restart through `fleet-svc`'s gate. A refused or failed restart
   is a failed check, so the pin never sits at a SHA that is not running.
5. Verify: the unit's pid changed since before the restart **and** the body at
   `base_url + health.path` (2xx) contains `health.expect`, within `timeout_s`.
   Then emit `deploy-verified` and release the lease.
6. Otherwise roll back, once. Emit `deploy-started` for the last verified SHA,
   check it out, restart, re-check, and page either way. If the rollback's check
   also fails, page, release and stop. Step 2 keeps the next cycle from retrying.

**First run.** With no `deploy-verified` for the binding, the pin's HEAD is
checked (body only; nothing restarts). If healthy it is recorded as the baseline
`deploy-verified` (`kind: baseline`); otherwise the run refuses and pages.

`--dry-run` prints what each step would do; it fetches nothing, changes
nothing, emits nothing and pages nothing.

**Leaving a stopped binding.** `retry --binding dev:<name> [--sha <sha>]` runs
one cycle ignoring step 2's skip. `--sha` must match the current `origin/main`.
Every check in step 3 and the lease still apply. It is for the owner at a
terminal; nothing scheduled runs it.

## Reading a deploy off the ledger

Events carry `repo`, the deployed `sha`, and in `detail`: `environment`,
`service`, `binding` (`dev:lynceus`), `provider`, `kind`
(`deploy` | `rollback` | `baseline`), and `from`, `lease` on a `deploy-started`.

What is running in dev for a service, and since when, is its newest
`deploy-verified`:

```
fleet-events --type deploy-verified --json --since 30d \
  | jq -s 'map(select(.detail.binding=="dev:lynceus")) | last | {sha, since: .ts}'
```

A commit that failed is a `deploy-started` with no `deploy-verified` at that
SHA. A rollback is a `deploy-started` (`kind: rollback`) for the older SHA,
followed by its own `deploy-verified`.
