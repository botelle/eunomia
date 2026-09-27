# `fleet-svc audit` — three registries of what runs, reconciled

*Plan 0050. Closes a gap the 2026-09-14 measurement on opshost made concrete:
`bin/mopsus` was merged, reviewed and built on 2026-09-13 and had never run —
no plist, never loaded, no log — and `bin/fleet-orphans` (plan 0020) was found
in the exact same state the same day. Each view of the fleet looked correct on
its own: the repo shipped the unit, `fleet-svc status` listed the services it
knew about, and `launchctl` listed what was loaded. Nothing compared the
three.*

## What this is

```
fleet-svc audit
```

Read-only, and takes no lease (like `status` — `docs/service-lease.md`).
**It reports. It never installs, loads, repairs or enrols anything.** That is
`bin/fleet-orphans`' own discipline, copied on purpose: a tool that "fixed"
what it found would silently override an operator's own decision to boot a
unit out, and the fleet has exactly one tool that is allowed to act
unattended — the dispatcher, not this one.

It covers **this host only** (opshost, as of this plan). Extending it to edgehost or
cihost needs ssh and a second supervisor's vocabulary, and an audit that is
sometimes wrong about a host it cannot reach is worse than one that says so —
see Boundaries.

## The three registries

| registry | what it says | read by |
|---|---|---|
| **shipped** | the repo's own `launchd/*.plist` | filename stem == label, by this repo's convention |
| **enrolled** | this host's rows in `config/services.conf` | `primary=` label, for `supervisor=launchd` rows whose `host` matches |
| **installed / loaded** | `~/Library/LaunchAgents/<label>.plist`, and `launchctl print gui/$UID` | a strict `plistlib` parse of the installed file; a label found in the domain's `services = {...}` block |

Each is a **fact**, never inferred from another (§3 boundary) — every one of
them was individually correct while mopsus sat undeployed for a day. A tool
that derived "installed" from "shipped", or "running" from "enrolled", cannot
see the class of problem this audit exists to find.

Per unit, the report prints all four: `shipped=`, `enrolled=`, `installed=`,
`loaded=`, plus `drift=` and the two alert-path columns below.

## Scope, and what makes the exit status non-zero

A unit is **in scope** if it is shipped by this repo, or enrolled in
`config/services.conf` for this host. Only in-scope units can make the audit
exit non-zero:

| condition | reported | exit non-zero |
|---|---|---|
| shipped, not installed **or** not loaded | yes | **yes** |
| installed, and drifted (below) | yes | **yes** |
| an in-scope plist fails the strict parse | yes | **yes** |
| enrolled for this host, label not installed | yes | **yes** |
| loaded `org.eunomia.*` this repo does not ship | yes | no — **including its own parse failures** |
| `org.eunomia.fleet-orch.<lease>` (a dispatch in flight) | **not at all** | no |
| `config/services.conf` rows for **another** host | yes, "not audited here" | no |

Scope is decided before condition: an unshipped label's parse failure is
still worth a line — the org.eunomia.lynceus case, measured live on opshost the
day this was written — but it can never fail the audit, because judging a
label this repo did not ship is not this tool's job. Without the
`fleet-orch.<lease>` exclusion the report would be red every time a dispatch
is running, and permanently red on the ledger host — a red that is always red
is a green.

## Drift: classified by what the SHIPPED copy says, never a hand-kept list

Two enumerated-exclusion-list revisions were both wrong (§2's own history —
the first excluded nothing, the second named five keys and still missed
three more `fleet-watch` ships empty). So every `EnvironmentVariables` key is
classified by what the **shipped** copy says:

| shipped | installed | class | counts as drift |
|---|---|---|---|
| empty string | non-empty | `install-filled` | no — information |
| empty string | empty / absent | `unfilled` | no — information |
| non-empty | different | **drifted** | **yes** |
| present | **absent** | **drifted** | **yes** — the install LOST a key |
| absent | present | `install-added` | no — information, see below |

A shipped plist with **no** `EnvironmentVariables` dict at all compares clean
against an installed copy carrying an empty one — the absence is not itself a
difference.

Everything outside `EnvironmentVariables` (`ProgramArguments`,
`WorkingDirectory`, `StartInterval`, `StartCalendarInterval`, `KeepAlive`,
`ThrottleInterval`) is compared directly: **any** difference there is drift.
That is where a real misinstall shows, and it is why `fleet-pin-watch` running
a plist the repo had already fixed (D6's own trigger) would be caught — its
`ProgramArguments`/`StartInterval` would differ byte for byte.

**`install-added` is a blind spot, named as one.** A key present only in the
installed copy is information, never drift — right for `PATH`,
`FLEET_SPAWN` and `FLEET_GIT_SSH_BASE`, which the install script adds. But
this audit cannot distinguish that from a key added by mistake, or by a
session poking at a plist by hand, so the report labels the column
**`install-added (not verified)`** rather than implying a guarantee it does
not carry.

Measured on opshost the day this was written: **every installed unit compared
clean.** In particular `fleet-watch`, which ships six empty placeholders
(`FLEET_WATCH_REPOS`, `FLEET_NTFY_URL`, `FLEET_OPERATOR_UID`,
`FLEET_IMPLEMENTER_UIDS`, plus `PATH`/`FLEET_SPAWN`/`FLEET_GIT_SSH_BASE`
added at install), is not drifted — every one of those differences is
`install-filled` or `install-added`, exactly the classes this rule exists to
separate from drift.

## The alert-path columns: `ntfy` and `angelia`, three states each

`SET`, `EMPTY` and `ABSENT` are different answers. A prior audit loop (the
`fleet-push` skill's own) rendered a blank value and a missing key
identically, because `PlistBuddy` fails on a missing key and its stderr was
discarded — so a unit with **no** alert path at all read exactly like one
that had one and had not filled it in.

`ntfy` reads `FLEET_NTFY_URL`; `angelia` reads `FLEET_ANGELIA_URL` (the var
`bin/orchestrator`'s own `notify()` branches on). Both are read from the
**installed** copy when the unit is installed, falling back to the shipped
copy otherwise — the installed copy is what an actually-running unit uses,
and the fallback keeps the column meaningful for a unit that ships an alert
path but has not been installed yet.

**A clean drift column does not mean a configured unit.** `fleet-pin-advance`
ships `FLEET_NTFY_URL` empty and it is still empty — `unfilled`, information,
not drift. Drift asks "is this the file we shipped"; the alert-path columns
ask "can this unit page anyone". Neither answers the other.

## No secret VALUE, ever — only key names and states

The report prints key **names** (`FLEET_NTFY_URL`, `FLEET_ANGELIA_URL`, an
invented placeholder, whatever a shipped plist happens to carry) and **states**
(`SET`/`EMPTY`/`ABSENT`, `drifted`/`install-filled`/…) — never a value.
`FLEET_NTFY_URL` is a capability URL; a drift or alert-path line that echoed
it would put it in a transcript, a log and a scheduled unit's stdout, through
a tool whose whole job is to describe configuration. A parse-error message is
similarly reduced to the exception's class name (and line number, for an
`ExpatError`) rather than its full text, which can otherwise echo a fragment
of the bytes that failed to parse.

## Shipped but not enrolled: information, and the file's own bar

A unit can be shipped, installed and loaded and still not have a row in
`config/services.conf` — `mopsus` and `fleet-orphans` both do, today. That is
**never** a fault; `config/services.conf`'s own header states its criterion:

> "each one, stuck, stops the dispatch flow in a way the operator cannot see
> from the outside"

— a narrower set than "everything eunomia ships". The report names the unit
and quotes that criterion beside it, for a person to weigh; it never enrols
what it finds. Enrolling automatically would erase the distinction between
the file's lease-protected set and the audit's own inventory, and leave the
file's header's own sentence false. `config/services.conf` is unchanged by
this plan.

## Strict parsing: `plistlib`, never `plutil`

`plutil -lint` and launchd's own parser both accept a `--` inside an XML
comment; `plistlib.load` — Python's `xml.parsers.expat` underneath — does
not. Two installed plists on opshost fail the strict parse for exactly this
reason. A check that agrees with `plutil` reports zero problems on both, so
this audit never calls it — `plistlib` only, always.

## Boundaries this plan set (and why)

- **Report-only.** Never installs, loads, repairs or enrols — `fleet-orphans`'
  own discipline; the fleet has exactly one tool allowed to act unattended.
- **No lease.** A lease gate here would make `audit` refuse on exactly the
  unit holding a lease on itself, `fleet-watch` — the most important row in
  its own report.
- **opshost only.** Remote hosts need ssh and a second supervisor's vocabulary;
  an audit sometimes wrong about a host it cannot reach is worse than one
  that only claims opshost.
- **`config/services.conf` is never written by this tool.** Enrolment stays a
  reviewed PR, per that file's own header.

## Handoff

**What the first real audit found, live on opshost, that this plan did not
predict specifically by name:** `org.eunomia.fleet-stalled` (plan 0060,
merged the same week as this one) — shipped and enrolled nowhere, not
installed, not loaded. It is the *exact* shape D1's own worked example
(`bin/mopsus`, 2026-09-13) describes, produced independently by the normal
pace of merges rather than staged for this plan — which is itself the
finding: a merged unit going undeployed was not a one-time incident, it is a
standing failure mode this audit now catches on the very first real run.
Separately, `org.eunomia.fleet-broker-tunnel`'s **installed** copy still
fails the strict parse (the pre-fix comment plan 0050 §2 describes) — `plutil
-lint` passes it, confirming the strict-parser boundary is doing real work,
not agreeing with a weaker check.

**Whether `audit` belonged in `fleet-svc` or wanted its own binary:** it went
into `fleet-svc` because two of its three registries — `config/services.conf`
and the launchd domain — are exactly what `fleet-svc` already reads and
speaks to, and reusing `_parse_services_conf()` and `_host()` meant the
audit's enrolled-for-this-host reasoning could never drift from `status`'s
own. If the opshost-only boundary above is lifted and remote hosts join, that
judgement is worth revisiting: a second binary would let host-reach concerns
(ssh, a systemd vocabulary) grow without pulling `fleet-svc`'s lease gate
along for the ride.
