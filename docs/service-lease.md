# The `service` lease — one owner of a supervised process at a time

*Row 7 (roadmap), plan 0029. Closes the 2026-09-07 cihost incident: two Claude
sessions each ran a supervised `forgejo-runner daemon` against the same
`~/agent/runner/config.yaml` — one as the root LaunchDaemon
`org.eunomia.forgejo-runner`, the other as the user LaunchAgent
`org.eunomia.forgejo.runner` — and each read the other's daemon as a hand-started
stray and killed it. launchd respawned it inside ~3 seconds; seven eunomia CI
runs failed with `Cannot find: node in PATH` on the daemon without
`DOCKER_HOST`. Nobody held a lease, and nowhere recorded who owned what.*

## What this is

`bin/fleet-svc` gates every mutating operation on a supervised process
(`launchd` label or `systemd` unit) enrolled in `config/services.conf`, behind
an ACTIVE `service` lease:

```
fleet-svc status [<host:unit>]                 # read-only, no lease required
fleet-svc start|stop|restart|kickstart <host:unit>
fleet-svc bootstrap|bootout|enable|disable <host:unit>
fleet-svc kill [--signal SIG] <host:unit>
fleet-svc edit-config <host:unit> [--sync-sha]
fleet-svc audit                                # read-only, no lease required
```

`status` and `audit` never require a lease (§Read verbs, below) — a lease
diagnosis needs is a lease diagnosis will route around, and the incident's
third session was *diagnosing* when it escalated the wrong kill. `audit`
(plan 0050, `docs/unit-audit.md`) reconciles what the repo ships, what
`config/services.conf` enrols and what launchd actually has loaded — a
lease gate on it would make it refuse on exactly the unit holding a lease on
itself, `fleet-watch`, which is the most important row in its report.

## Resource identity: the canonical unit, never the label

A `service` resource is `{"type":"service","host":<host>,"service":"<host:unit>"}`,
where `<unit>` is a **fleet-chosen canonical name** — never a launchd label,
never a systemd unit name. Two supervisors of one config are ONE resource: the
thing there is one of is the running program plus the config and labels it
registers with, not the plist that happens to start it.

Why this must not be the label, measured on this repo before the fix:
`fleet-claim`'s conflict check (`_refuse_conflicting_holder`) compares
resources by `json.dumps(resource, sort_keys=True)` exact equality. Naming the
resource by label reproduces the incident *inside the fix* — two labels, two
accepted leases, one runner:

```
$ fleet-claim --assign '{"type":"service","host":"cihost",
    "service":"cihost:org.eunomia.forgejo-runner"}' --holder session-aaa
service--cihost-org.eunomia.forgejo-runner--001
$ fleet-claim --assign '{"type":"service","host":"cihost",
    "service":"cihost:org.eunomia.forgejo.runner"}' --holder session-bbb
service--cihost-org.eunomia.forgejo.runner--001      # exit 0 — BOTH GRANTED
```

Canonical-unit identity refuses the second claim, because both sessions name
the same string.

**`SPEC.md` §Lease record's own `service` example is the shape this forbids —
`"service": "edgehost:minos.service"`, a systemd unit name — while this plan's own
row for that service is `edgehost:minos`.** `SPEC.md` is deliberately not touched
by this plan (its example fix is a one-token PR sequenced after plans 0014,
0016, 0021, 0025 and 0028 — all of which hold `SPEC.md`/`EVENT_TYPES` today).
Until that PR lands: **the discrepancy is real, `SPEC.md`'s example is wrong,
and `config/services.conf` / `fleet-svc` are the source of truth for what a
`service` value actually looks like.**

## The config file is an attribute of the service, not a second lease

`~/agent/runner/config.yaml` and similar files get no `paths` lease. Two
reasons:

- `bin/fleet-prepush`'s `paths` check matches pushed **repo-relative** paths.
  A config file outside any repo, never pushed, would never trip it — a row
  that looks like protection and enforces nothing is worse than no row.
- Editing the config and restarting the daemon are one act with one blast
  radius (the incident's config edits *and* its kills both cost CI runs).
  Splitting them across two leases reopens the coordination gap one level down.

`edit-config` gates the edit under the same `service` lease and records the
file's sha256 into the lease's `binding.config_sha`. A raw edit outside the
wrapper leaves the sha stale; `fleet-svc status` then shows `DRIFT` — **shown,
never acted on**. `UNKNOWN` (never `ok`) is what an unreadable config renders
as, whether the file is simply gone or the host is unreachable over ssh: `ok`
on an unreadable config is the one output that would hide drift.

## Which operations need the lease

**Require an ACTIVE lease:** every verb above except `status` and `audit` —
starting, stopping, killing, or editing the gated config.

**Never require one:** `status`, `audit`, and anything outside this tool
entirely (`launchctl print`, `ps`, reading logs). Free reads are what keep the
gate credible on the write path — a gate on reads is exactly how people learn
to route around it, and the incident's third party was diagnosing, not
mutating, when it escalated the wrong kill.

## The gate, step by step

Every mutating verb, in order:

1. **Refuse a `<host:unit>` whose host is not this machine**, naming both.
   The ledger is shared; the launchd/systemd domain is not — running
   `fleet-svc start opshost:fleet-watch` from cihost must never act on a
   same-named unit on cihost.
2. **Resolve `<host:unit>` in `config/services.conf`.** An unenrolled name is
   refused by name — a service with no row is simply not protected by this
   tool, and raw `launchctl`/`systemctl` remains untouched.
3. **Require an ACTIVE `service` lease** for that exact resource, held by
   `EUNOMIA_SESSION`.
4. **Refuse if any OTHER lease is `state=active` on that same resource with a
   different holder — regardless of orphanhood.** This is the step that
   actually carries the guarantee; see below.
5. On any refusal in steps 3-4: name the holder(s), `activated`, `ttl_minutes`
   and the lease id(s), and the two legal next moves — coordinate, or
   `fleet-claim --takeover <lease-id>` if the holder is dead
   (`docs/takeover.md`). **It refuses. It never reaps, never auto-takes-over.**
6. **Perform the operation against the row's `primary` label only.** `also`
   labels are enrolment data, reported by `status`, never a target — acting on
   every label a unit registers is the two-daemon fault, performed under a
   lease.
7. **`kill` re-resolves the pid from the supervisor at kill time** (`launchctl
   print` / `systemctl show --property=MainPID`, cross-checked against the
   process's own start time via `ps -o lstart=` — the same idiom
   `bin/fleet-pkill` uses, because it survives `setproctitle` retitling and
   still catches pid reuse) and **refuses if either differs from the lease's
   bound pid**. The lease's `binding.pid` is never signaled directly: a
   `KeepAlive` respawn is ~3 seconds wide in the actual incident, and a bound
   pid is stale that fast.
8. **Bind `{pid, started_at, label, config_sha, bound_at}` onto the lease's
   TOP-LEVEL `binding` field**, under the lease's lock, re-verifying the
   holder under that lock. A bind that fails after a successful operation is a
   loud stderr `WARN`, never a silent success — the alternative is a live,
   unattributed process, the incident's precondition.

### Why step 4 is not redundant with step 3

`fleet-claim`'s own conflict check (`_refuse_conflicting_holder`) skips any
lease `lib.is_orphaned` calls orphaned — deliberately: plan dispatch assigns a
successor *before* retiring a dead lease, and blocking on orphans would strand
a plan whenever that assign failed. So **past a lease's ttl, a plain
`fleet-claim --assign` for the same resource is granted to a second holder**,
measured on this commit:

```
$ fleet-claim --assign '{"type":"service","host":"cihost","service":"cihost:forgejo-runner"}' \
      --holder session-aaa --ttl 60          ->  service--cihost-forgejo-runner--001
$ fleet-claim --activate service--cihost-forgejo-runner--001
$ fleet-claim --assign <same resource> --holder session-ccc
  fleet-claim: ...--001 already holds this resource (holder='session-aaa', state=active)
$ touch -t <now-90min> $FLEET/sessions/session-aaa/hb
$ fleet-claim --assign <same resource> --holder session-ccc
  service--cihost-forgejo-runner--002        # exit 0 — GRANTED, no takeover
```

Two `active` rows, two holders, one resource — and **both individually satisfy
step 3.** Step 4 is the only thing that turns that reachable state into a
visible refusal for both holders instead of a second daemon. It is deliberately
NOT "fixed" by making `fleet-claim` block on orphans (that would break
respawn) — the asymmetry (recovery wants the grant permissive; exclusivity
wants the operation strict) is the point, and it lives in two different tools
on purpose. `tests/test_fleet_svc.py::test_step4_refuses_both_holders_when_two_leases_are_active`
and `::test_ttl_measurement_reproduces_dual_active_via_real_fleet_claim` are the
regression tests for this — deleting step 4 as "obviously redundant" is the
single most likely way to reopen the incident with a green-looking gate.

## Why the binding lives at the lease's TOP LEVEL, not inside `resource`

`bin/fleet-sim` binds its UDID into `resource["udid"]`. That placement is
**correct for `sim`** (SPEC says `udid` is "filled at activation", and sim rows
are capacity — several leases per host is the intent) and **wrong for
`service`**, where there is exactly one legitimate owner. Measured on this
commit, with the binding written into `resource` the way `fleet-sim` does it:

```
# lease --001 ACTIVE, held by session-aaa, with pid bound INSIDE resource
$ fleet-claim --assign '{"type":"service","host":"cihost",
    "service":"cihost:forgejo-runner"}' --holder session-ccc
service--cihost-forgejo-runner--002        # exit 0 — SECOND LEASE GRANTED
```

Binding into `resource` makes it unequal to the bare identity JSON the next
`--assign` compares against, so the very first successful `start` would
silently disarm the one-owner rule it exists to enforce. `binding` at the
lease's top level keeps `resource` byte-identical to what was granted for the
life of the lease — `fleet-svc` never writes `resource`, only `binding`.

This needed **no change to `fleet-claim` or `fleetlib.py`**: `write_lease`
validates only the record's `id` and passes extra top-level keys through
untouched.

## Enrolment: `config/services.conf`

```
# host:unit   supervisor=launchd|systemd  domain=<launchctl domain, or systemd scope>
#             primary=<the ONE label a verb acts on>
#             also=<other labels this unit registers, comma-sep, or - >
#             config=<ABSOLUTE path on that host, or - if none>
```

`supervisor` and `domain` are fields, not inferences: day one already spans
both supervisors (`edgehost` runs systemd units; the rest run launchd), and within
launchd the incident's two daemons were a root LaunchDaemon (`domain=system`)
and a user LaunchAgent (`domain=gui`) — different `launchctl` target strings,
different privilege.

`primary` is the label a verb acts on; `also` is what the unit additionally
registers, reported by `status`, never targeted. The cihost runner's row
carries **both** labels against **one** unit name — that single line is the
incident's fix expressed as data. Which label ends up `primary` is a decision
for the runner's two owning sessions, not this plan; the checked-in row
records the LaunchDaemon as a placeholder their decision overwrites.

`config` is an absolute path on the row's **own** host, never `~` — nothing in
`fleet-svc` expands a tilde, and doing so would resolve against the wrong
host's home whenever `fleet-svc` runs elsewhere (the ledger lives on opshost; the
runner's config lives on cihost).

A service with no row is not protected. Enrolment is a reviewed PR editing this
file, never an environment variable — the incident's two daemons differed
*precisely* by environment (plist `PATH` + `DOCKER_HOST` vs. login `PATH`), so
enrolment that depended on the caller's environment would inherit the bug it
exists to close.

## TTL: 60 minutes, and what expiry does not do

Service leases are granted `--ttl 60` against `fleet-claim`'s 240-minute
default. A service lease's ttl is a **recovery** parameter, bounding how long a
*dead* holder blocks a restart of shared infrastructure — not a work-duration
budget. 60 minutes is far longer than any turn cadence, keeping it clear of
`docs/takeover.md`'s flap trap (a ttl shorter than the gap between turns emits
one `lease-orphaned` per cycle).

**Expiry does exactly what SPEC's Principle 4 says and nothing more:** the
lease becomes claimable — by a plain `--assign` as well as by `--takeover`, per
the measurement above. **It never stops the service, never unbinds the pid,
and never restarts anything.** The process keeps running exactly as it was;
only ownership becomes ambiguous, and step 4 is what makes that ambiguity
visible instead of silent.

**This gate only means something once a live holder's heartbeat is actually
current.** `fleet-claim --activate` writes `sessions/<holder>/hb` once; only
the Stop-hook heartbeat (plan 0005) refreshes it, and as of this writing plan
0005 is recorded `abandoned` rather than `done` — its own note says the work
already exists on `main`, built by hand, but no dispatched PR ever closed it.
**Confirm the heartbeat hook is actually installed on every host that will
hold a service lease before trusting `fleet-status`'s liveness for one** — the
2026-09-08 measurement in plan 0029 found opshost's own `~/.claude/settings.json`
carrying no fleet heartbeat hook at all. Without it, every service lease
derives orphaned 60 minutes after activation whether or not its holder is
alive, and step 4 — not step 3 — is doing all of the real work.

## Off-opshost: fail closed, two connections, never a fresh dial

The ledger lives on opshost. From cihost, edgehost or vaulthost, `fleet-svc` reaches it
through `EUNOMIA_LEDGER_HOST` (the same variable `fleet-prepush` and
`fleet-heartbeat` already use) via their exact reuse-only ssh idiom: `ssh -O
check <host>` first (asks an existing multiplexer whether it is up, dialing
nothing), then, only if that succeeds, one command over `-o
ControlMaster=no -o ProxyCommand=false -o ConnectTimeout=2` — never a fresh
master. A 60-second polling loop with fresh dials once left 90k `TIME_WAIT`
sockets and killed TCP on opshost; this is not a style preference.

**A mutating verb makes exactly two connecting invocations**, not one: the
lease read (steps 3-4) and the bind (step 8), with the local `launchctl` /
`systemctl` call running between them. The bind cannot be a plain remote file
write — `fleetlib.held_lock` is a local lock on the ledger host, and other
hosts do not mount the ledger — so it is one remote command that runs
`fleet-svc --bind` **on the ledger host itself**, taking the lock, re-verifying
the holder, and writing where the lock means something.

**Ledger unreachable ⇒ refuse the operation and say so.** This inverts
`docs/enforcement.md`'s polarity rule for `fleet-prepush`/`fleet-sim`/
`fleet-pkill` (fail open, loud WARN) — deliberately. There, a false block
freezes every push on the host; here, a **missed** block is the incident
itself (two supervisors, seven red CI runs, a respawn inside 3 seconds), and a
false block costs one operator one operation, with raw `launchctl` as the
documented escape hatch. Read verbs (`status`) still degrade rather than
refuse: `UNKNOWN` sha, holder shown as unknown, never an error — the fail-closed
rule is about mutating verbs only.

## This is cooperative tooling, not enforcement

A session running raw `launchctl bootout` or `systemctl stop` bypasses every
word of this plan, and `fleet-svc` cannot and will not stop it (Principle 5).
That is a review finding, not something the gate prevents — its job is to make
the honest path easy and the collision visible, the same model
`docs/enforcement.md`'s other three installers already follow.

## What this plan deliberately does not build

- **No `service-started` / `service-stopped` events.** `SPEC.md`'s
  `EVENT_TYPES` set is leased by plans 0014, 0016, 0021, 0025 and 0028; opening
  a sixth concurrent lane on it is the collision class this plan exists to
  close. The existing `lease-*` events (`lease-assigned`, `lease-activated`,
  `lease-released`) answer *who took this service and when*; the lease's
  `binding` answers *what pid*. If a future incident's diagnosis needs the
  *sequence* of transitions rather than current state, that is the trigger for
  this follow-up — not before.
- **No `bin/fleet-status` changes in this PR.** §2.9's design calls for an
  additive `binding` column there, sequenced strictly after plan 0005 lands as
  `status: done`. As of this PR, 0005 is recorded `abandoned` (its
  functionality already exists on `main`, built by hand, but no dispatched PR
  ever closed the plan) — not `done`. Per this plan's own boundary, the honest
  move on that exact fork is to leave `bin/fleet-status` untouched and ship the
  `binding` column as its own follow-up once a plan formally closes 0005.
- **No deploy wrapper.** `lib/deploy.sh` (infra ROADMAP row 14) is the other
  consumer of this lease and stays a separate row — this plan grants the lease
  that wrapper will later require.
- **No decision about the cihost runner's label split.** Whether the
  LaunchAgent or the LaunchDaemon survives as `primary` is for the runner's two
  owning sessions to decide under the lease this plan grants them, not for this
  plan to decide on their behalf.

## Handoff

The plan's own §5 asks for several things to be recorded once this gate has
actually been operated — the first real refusal (and whether the operator
could act on it without ssh-ing to the host), whether step 4 ever fired and
why, whether the heartbeat hook was installed everywhere a service lease was
held before it mattered, and whether the 60-minute ttl proved right in
practice. None of that history exists yet at the point this PR lands; add it
here, under this heading, the first time any of those questions gets a real
answer instead of speculating one now.
