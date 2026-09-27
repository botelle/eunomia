# Operating state — what is built, what is running, and what stands between them

*Measured 2026-09-08 on opshost. Every claim here was checked against the host rather
than read off a plan; where a document and the machine disagreed, the machine won
and the disagreement is recorded.*

## Accounts and roles

Author, reviewer and merger are the design (coder ≠ reviewer ≠ merger, two
approvals, never self-merge) — `bin/fleetlib.py`'s `agent_roles()` is the one
table both `fleet-watch` and the orchestrator resolve it from (plan 0056). The
three positions are fixed; only which forge account fills each is
configuration. This fleet's own mapping:

| role | forge account | env override |
|---|---|---|
| author (implementer) | `implbot` | `FLEET_AUTHOR_ACCOUNT` |
| reviewer | `revbot` | `FLEET_REVIEWER_ACCOUNT` |
| merger | `operator` | `FLEET_MERGER_ACCOUNT` |

The rest of this document, and the rest of the fleet's docs, name these three
accounts directly rather than by role — they are this fleet's real accounts,
and rewriting them to the role names would make the documentation stop
describing the system actually running (ADR-0010). A deployment under a
different set of forge accounts sets the three env vars above; nothing else
in this file changes.

## The short version — re-measured 2026-09-21

The fleet is operational. Every unit below is loaded on opshost from the pin, the
failure channel pages a phone, and merge-is-ignition has fired for eunomia plans
0058–0065 and infra 0009 and 0010. What stands between a merge and a dispatch
now is throughput, not activation — `docs/fleet-watch-remaining.md` has the
list; the first item is that the single dispatch slot was held for eight hours
by an orchestrator waiting on a human merge (plans 0067 and 0068).

The 2026-09-08 measurement that follows the table is kept as history: it is the
record of what activation cost, and the reason the activation order below reads
the way it does.

## What was measured

| component | built | 2026-09-08 | 2026-09-21 |
|---|---|---|---|
| `fleet-emit` / `fleet-events` | yes | CLI | CLI; `EVENT_TYPES` grew with 0058, 0063 |
| `fleet-claim` / `release` / `status` | yes | CLI | CLI |
| `fleet-collect` | yes | loaded | loaded, every 120 s, ingests CI too (0045) |
| `fleet-leak-watch` | yes | loaded | loaded |
| `fleet-broker-tunnel` | yes | loaded | loaded; verdict `ok: true` for every enrolled repo |
| `fleet-pin-watch` | yes | loaded, firing | loaded; drift pages, `fleet-pin-advance` heals eunomia's pin |
| `fleet-pin-advance` | — | not present | loaded |
| `fleet-orphans` | — | not present | loaded, daily; pages (exit 2 = orphans found) |
| `fleet-snapshot` | yes | not present | loaded; exit 1 = "NOT BACKED UP" (see below) |
| `fleet-db-snapshot` | yes (0063) | — | plist on `main`, **not loaded** |
| `mopsus` | yes (0046, 0064) | not present | loaded; briefs with a shortlist and a Minos link |
| `fleet-secret-guard` | yes | PreToolUse hook, from the pin | PreToolUse hook, **root-owned libexec copy** (`docs/secret-guard.md`) |
| `fleet-watch` | yes | **not installed** | loaded, resident, `FLEET_WATCH_CAP` unset (= 1) |
| `orchestrator` | 5 of 5 | phase 1 of 5 | one launchd job per dispatch (0053); `--ready` exits 0 |

Two rows carry a non-zero last exit on purpose and one does not:

- `fleet-orphans` exits 2 when it found orphans; that is the finding, not a fault.
- `fleet-snapshot` exits 1 because its coverage check says `restic installed: NO`.
  That check is on the wrong host: restic 0.19.1 is on **cihost**, where the
  offline DR tier runs (`infra/backup/dr-snapshot.sh`), and opshost's
  `~/dev/.fleet/snapshots` is not in what `stage-opshost.sh` collects. So the
  exit is right about the outcome — the fleet database has no copy off opshost —
  and wrong about the reason. Adding the snapshot directory to the opshost stage
  is an infra change; the coverage check should then say so instead of probing
  for a local binary.
- `fleet-db-snapshot` (0063's online backup of `fleet.db`, WAL-safe) is on `main`
  and not bootstrapped; until it is, `fleet-snapshot`'s VACUUM copy is the only
  local second copy.

## History — the 2026-09-08 measurement

### Four findings worth stating plainly

**The drift alarm works, and nobody can hear it.** `pin-watch.log` carries
`fleet-watch: 2 pin(s) drifted`, repeatedly. Detection is correct; the finding
goes to a file. `docs/plan-dispatch.md` already says why that is not enough — "the
push notifications are the failure channel … rather than in a log nobody reads."

Measured 2026-09-18 — every unit plan 0058 inventories as a pager, not the
ad hoc list this table held before. There are still **three** states, now
illustrated by different units than the ones that first showed them:

| unit | `FLEET_NTFY_URL` |
|---|---|
| `fleet-watch` (`org.eunomia.fleet-watch`, `--resident`) | configured |
| `fleet-pin-watch` (`org.eunomia.fleet-pin-watch`, `--pins-only`) | configured |
| `fleet-leak-watch` | configured |
| `fleet-orphans` | configured |
| `fleet-pin-advance` | present, blank — print-only |
| `mopsus` | configured |
| `orchestrator` | — (no plist: spawned as a subprocess by `fleet-watch`'s `spawn()`, never its own launchd unit) |

`fleet-leak-watch` and `fleet-pin-watch` were the print-only pair in the
2026-09-08 measurement; both now carry a real URL, and `fleet-pin-advance` is
the unit that shows blank-vs-missing today instead. `mopsus` is worth a note:
plan 0058's own Resources section describes it as "not a scheduled unit", and
the machine disagrees — an installed plist, `org.eunomia.mopsus`
(`StartInterval 300`), exists and is configured. Recorded per this document's
own rule at the top: where a document and the machine disagree, the machine
wins.

An earlier revision of this document said the variable was "empty on every
installed unit". That conflated *blank* with *missing*, which is the mistake the
`fleet-push` skill exists to warn about: `PlistBuddy` fails on a missing key, and
a check that discards its stderr renders both states identically. `orchestrator`
has nothing to fill in — no plist at all — and "wiring" it would produce a false
sense of coverage.

**And the variable cannot be pointed at angelia.** `notify()` does a bare POST of
`f"{title}: {body}"` as plain text with no headers — ntfy's contract. angelia
requires `X-Angelia-App` and `X-Angelia-Token` headers and a JSON body, and
answers a headerless POST with `401`. The caller catches it and warns on stderr —
`NOTIFY(failed)` from the watcher, `ALERT(ntfy-failed:<Exception>)` from the leak
detector — so it is not literally silent. It is worse than silent in the way that
matters: the *page* is not delivered, and the evidence that it was not lands in
the same log the page existed to avoid depending on. And the two consumers do not
share a string, so grepping a leak-watch log for `NOTIFY(failed)` finds nothing
and reads as "no pages failed".

**The scheduled units run stale code.** They execute
`~/.local/share/pins/eunomia`, correctly — that pin exists precisely because the
shared clone was seen on three branches in one afternoon. But the pin is 10
commits behind `main`, so every 120 seconds the fleet runs a version of itself
from before ten merges. The detector for this condition is itself running from
the stale pin.

**Merge-is-ignition is fully specified and has never fired.** speakhush#149 merged
five plan files. Nothing happened, and nothing was written anywhere explaining
why — the watcher was not installed, and would have refused to arm if it were.
Both are correct behaviours. Neither was visible.

## Activation order, and why this order

Each step is a precondition for the next being observable.

**1. Decide the channel, then connect it, then break something to prove it.**
This is not a config step. `FLEET_NTFY_URL` speaks ntfy; angelia is the fleet's
push service and speaks headers plus JSON. So either an ntfy endpoint gets stood
up and the field points at it, or `notify()` and `_alert()` learn angelia's
contract — a code change in three units. Choose deliberately; do not fill the
field with an angelia URL, which 401s into a swallowed exception.

Until this is done every later step succeeds or fails into a log, and "it seems
fine" is unfalsifiable.

Proving it needs the steps in the opposite order to the obvious one. `check_pins`
is transition-gated — it records the last-seen state word and says nothing when
that word is unchanged — so a pin that is already `stale` will not page however
much further behind it is pushed. **Fast-forward the pin first** (step 2), let a
cycle record `current`, and only then move it back one commit. `stale -> current
-> stale` is a transition; `stale -> staler` is not. A notify path that has never
delivered is not a notify path.

**2. Refresh the pin, and let its staleness page.**
Fast-forward `~/.local/share/pins/eunomia`. With step 1 done, the next drift
reaches a phone instead of a file. Pin freshness is now the fleet's single most
load-bearing operational fact: it decides which version of everything runs.

**3. Install the `fleet-watch` unit.**
It will log a refusal, because the orchestrator is not ready. That is the point —
it converts "nothing happened" into a recorded reason, which is what speakhush#149
lacked. Do not wait for step 4 to do this.

**4. Wire the orchestrator's four remaining phases.**
Plans 0010 (implementer session), 0011 (review loop), and the comment/ack protocol
and post-approval freeze. This is the substantial work, and `--ready` stays
non-zero — so the watcher stays inert — until all four land. Nothing about steps
1 to 3 depends on it.

**5. Make enrolment mean dispatch.**
`FLEET_WATCH_REPOS` is the allowlist today; `config/repos.conf` is a record with no
reader, and `fleet-repo` says so to the operator's face. Plan 0013's
main-via-contents-API half closes it. Until then, enrolling a repo and expecting a
dispatch is a mistake the tooling actively warns about.

## The rule this document exists to enforce

A component is not operational because it is merged. It is operational when it is
running the current code, from a pin someone can see is current, with its failures
arriving somewhere a person will read them. Three of the four gaps above are of
that shape rather than "not written yet", and they were invisible until someone
went and looked at the host.
