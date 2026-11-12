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
   one; with none up the binding is skipped this cycle. On opshost the master is
   kept up by `launchd/org.eunomia.fleet-git-master.plist`, which needs a
   `Host forge.example` block in `~/.ssh/config` with `Port 3022` and
   `ControlMaster auto` / `ControlPersist yes` (the plist's header has the
   block). Until 2026-09-30 nothing kept a master up, so every cycle on opshost
   ended `unfetchable` and fleet-deploy had never deployed.
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

## Enrolling a repository is one reviewed line (plans 0122, 0134)

`config/repos.conf` is the only list. The units that run from the eunomia pin
read it from that checkout, never from `~/dev/eunomia` and never over the API:

| Unit | Reads from `repos.conf` |
|---|---|
| `org.eunomia.fleet-watch` | the `dispatch` repositories it scans |
| `org.eunomia.fleet-rebase` | the `dispatch` repositories the sweep covers |
| `fleet-record` | the `dispatch` repositories whose merges it records |
| `org.eunomia.fleet-pin-advance`, `org.eunomia.fleet-pin-watch` | a pin, `~/.local/share/pins/<name>`, for every listed repository |

So merging a PR that adds a `repo:` line, and the eunomia pin reaching that
commit, is the whole of enrolment: no installed plist is edited.
A `repos.conf` that is missing, unreadable or lists no repository scans nothing
and pages once; it never falls back to `FLEET_WATCH_REPOS`, which is no longer
read (a leftover value that differs is warned about each cycle). The file is
intent; the broker's per-cycle protection verdict stays the authority.

A line the parser cannot read (an unknown flag such as `dispach`, a line that is
not a `repo:` line, a `repo:` with no name) does not stop the fleet: every line
that parses is still scanned, but the repository on the bad line is **not**
scanned, and the watcher logs one `WARN` per cycle and pages once per distinct set
of errors, titled "repos.conf has a line the watcher cannot read" and listing each
`repos.conf:<line>: <message>` (plan 0136). The page clears when the file parses
cleanly. `fleet-record` reads the same list from the pin, not from its checkout.

Environment variables still read by these units: `FLEET_PINS` (pins that are
**not** a listed repository, `~/agent` only, `path=ref` syntax kept),
`FLEET_OPERATOR_UID`, `FLEET_IMPLEMENTER_UIDS`, `FLEET_NTFY_URL`,
`FLEET_FORGEJO_URL`, `FLEET_BROKER_SOCKET`, `FLEET_RECORD_*`.
`tests/test_pin_set_covers_the_fleet.py` fails if a plist names a per-repo pin
or a watch list. `org.eunomia.fleet-pin-watch` runs the watcher with the
pins-only flag, which returns before any cycle: now that every unit can read the
list, that early return is its only guard against dispatching.

**The one owner step.** A new repository's pin is created once, by hand, below.
After a change to the plists, reinstall the four units from the pin (bootout,
wait for exit, enable, bootstrap, as above) to drop the hand-filled lists.

**Creating a missing pin.** The scheduled mover still refuses `missing`. After
a deploy, and whenever `repos.conf` grows, the owner runs once by hand:

    fleet-pin-advance --dry-run --create-missing    # `would create` / refusals
    fleet-pin-advance --create-missing

It clones, over `ssh://git@forge.example:2222/operator/<repo>.git` with no
checkout, then detaches at `origin/main`, only for a repository that is in
`repos.conf` and that the broker vouches for. `dirty`, `attached`, `not-a-repo`,
`unfetchable`, a broker that cannot vouch, and a repo with no `main` rule are
still refused. The flag is never set in a plist.

**Fetch load (to be filled in by the owner after the first full-set run).**
Each run fetches every pin over one ssh key; `unfetchable` refusals have
clustered at the front of the list (78 of 854 runs before this change).

| | wall-clock of one run | `unfetchable` over a day |
|---|---|---|
| 3 pins (before) | _not measured in this PR_ | 78 of 854 runs |
| full set (after) | _record after deploy_ | _record after deploy_ |

If the rate rises above 78/854, say so; parallelism is deliberately not added.
