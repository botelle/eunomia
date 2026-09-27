# Plan dispatch — the processes that run, and the data each one writes

*Companion to [`docs/diagrams/dispatch-dataflow.svg`](diagrams/dispatch-dataflow.svg)
(v1.0, 2026-09-22). `docs/plan-dispatch.md` is the design and says who decides;
this page says what runs and what it leaves behind, so a reader can go from any
step to the file, table or forge object it wrote — and from any file back to
the step that wrote it. Measured on opshost against `main` @ `245fd0b`; where a
plan will change a row it is named.*

## The stages

| # | step | process | trigger | reads | writes |
|---|---|---|---|---|---|
| ① | plan PR opened | `file-a-plan` skill → `~/bin/forgejo-open-pr.sh`, as implbot | a person | the target repo's `main` (id sweep, paths, broker verdict) | forge: PR on `plans/NNNN-slug` |
| ② | plan review | `~/agent/dispatch-review.py` → a Fable session | the author session, after CI | the PR diff by SHA in a private worktree (ADR-0005) | forge: review, `review-ledger` comment, follow-ups issue (`file-followups.py`) |
| ③ | merge the plan PR | the operator, in Minos | two approvals | — | `main`: `plans/NNNN-slug.md` at `status: ready` |
| ④ | verdict per repo | `bin/fleet-watch --resident` | every 120 s | `FLEET_WATCH_REPOS`; keyvault's forgejo-broker over `FLEET_BROKER_SOCKET` | `pages.jsonl` once on refusal; `leases/<key>.watch-notified` |
| ⑤ | scan, dedupe, gate | `fleet-watch` | same cycle | `plans/` on `main` via the contents API; the repo's PR list (any PR with `Plan: <id>`); `depends_on`; `leases/` (live count vs `FLEET_WATCH_CAP`) | `events.jsonl: plan-blocked` on transition; `watch.log` (`deferred — at cap N`) |
| ⑥ | spawn | `fleet-watch` → `bin/fleet-claim --assign`, `bin/fleetjob.py` | a `ready`, undispatched, unblocked plan and a free slot | — | `leases/<lease>.json` (assigned), `locks/`; `events.jsonl: lease-assigned, plan-dispatched`; launchd job `org.eunomia.fleet-orch.<lease>` with `EUNOMIA_SESSION/PLAN_ID/ZONE/TIER` |
| ⑦ | start | `bin/orchestrator` | the launchd job | the lease; `fleet-models` (runner per position); the private `.git-store` clone | lease → active, heartbeats; `events.jsonl: lease-activated`; `work/<repo>/<lease>/` worktree, `feat/<slug>` branch; `work/logs/<lease>.log` (every phase from here on); `sessions/<sid>/` |
| ⑧ | implementer session | `claude -p` as the resolved runner (default `sub-sonnet`) | ⑦ | preamble + plan + bounds on stdin; the worktree | commits, `git push`; the work PR **with `Plan: <id>`** (the session opens it); `~/dev/.orchestrator-transcripts/<lease>.log` (redacted at write); run log `implementer-start/exit` |
| ⑨ | verify, wait for CI | `bin/orchestrator` | ⑧ exits | the PR by marker (`fleetforge`); the plan file on the branch; combined commit status | forge: PR body (marker stamped); run log `verified` / `unverified`, `ci-waiting`, `ci-resolved` |
| ⑩ | review round | `dispatch-review.py` → Fable; or a CI-fix session | CI resolved | the PR at head SHA; today the failing check names only (plan 0069 adds the stored log) | forge: review, ledger comment, follow-ups issue; `fleet.db review` later via `fleet-reviews` |
| ⑪ | fix round, or stop | a fix session; `bin/orchestrator` | a HIGH, or CI red | the review findings | commits; run log `review-round`, `review-stopped`; `events.jsonl: plan-failed` on stop |
| ⑫ | freeze and steward | `bin/orchestrator` | APPROVE | the PR's top-level comments every 60 s (`unfreeze`, `instruction`, `followup`) | pushes refused; run log `review-approved`, `steward-start`, `steward-*`; forge: ack comments |
| ⑬ | merge the work PR | the operator, in Minos | two approvals | — | `main` |
| ⑭ | release | `bin/orchestrator` | the steward sees merged | — | lease → released; `events.jsonl: lease-released`; run log `steward-end`; the job exits and ⑤ reaps its label |
| ⑮ | orphan sweep | `fleet-watch` | a lease whose heartbeat lapsed or whose process is gone | `leases/`, the process table | `pages.jsonl` + angelia push (`Orchestrator dead`), once; `leases/<lease>.watch-notified`; `events.jsonl: lease-orphaned` |
| ⑯ | brief | `bin/mopsus` | every cycle | `leases/`, `fleet.db` (`dispatch_phase`), the run log; `CLASS_RULES` | `pages.jsonl` + push with the shortlist and a Minos link; `mopsus.state.json` |
| ⑰ | a person answers | the operator | the page | — | forge: a top-level `respawn` comment (bound to `FLEET_OPERATOR_UID`, must be newer than the lease) → back to ⑥ at the cap |

## The stores

| store | writer | readers | notes |
|---|---|---|---|
| forge: PR, reviews, comments, status | implbot (①, ⑧), revbot (②, ⑩), the orchestrator (⑨, ⑫), the operator (③, ⑬, ⑰) | everything | the only store off opshost; `fleetforge` is the one door (plan 0051) |
| `main`: `plans/*.md` | a merge, only | `fleet-watch` via the contents API | never a working tree (ADR-0005) |
| `~/dev/.fleet/leases/*.json` | `fleet-claim` (⑥, ⑦, ⑭, ⑮) | `fleet-watch`, `mopsus`, `fleet-status` | states assigned → active → released or orphaned; a `.watch-notified` sibling means "paged once" |
| `~/dev/.fleet/locks/` | `fleet-claim` | itself | write locks |
| `~/dev/.fleet/events.jsonl` | `fleet-emit` (every unit) | `fleet-events`, `fleet-collect` → `event` | append-only; closed `EVENT_TYPES` in three declarations kept equal by a test |
| `~/dev/.fleet/work/<repo>/<lease>/` | the orchestrator (⑦) | the implementer and fix sessions | a worktree off the private `.git-store` clone, hooks pinned to `/dev/null`; kept after the run as the record |
| `~/dev/.fleet/work/logs/<lease>.log` | the orchestrator | `mopsus`, `fleet-collect` → `dispatch_phase`; the cap count after plan 0067 | the phase stream; one line per `log.note` |
| `~/dev/.orchestrator-transcripts/<lease>*.log` | the implementer and fix sessions | `fleet-collect` → `session/turn/tool_use` | redacted at write; outside the tree atlas renders |
| `~/dev/.fleet/pages.jsonl` | every unit that pages (plan 0058) | `lynceus`, a person | `ts, unit, title, body, delivered` |
| `~/dev/.fleet/mopsus.state.json` | `mopsus` | `mopsus` | which failures were briefed, so a brief is sent once |
| `~/dev/.fleet/sessions/<sid>/` | `fleet-bind` (SessionStart) | `fleet-pkill`, the orphan sweep | sid → pid, host |
| launchd `gui/501` | `fleetjob.py` (⑥) | `fleet-watch` (⑤ reaps finished labels) | one-shot `org.eunomia.fleet-orch.<lease>`; `FLEET_SPAWN=popen` is the other mode |
| `~/dev/.fleet/fleet.db` | `fleet-collect` (schema owner), `fleet-reviews`, `fleet-models`, `fleet-config` (0068) | `lynceus`, `mopsus`, `fleet-ci`, `atlas` | `session turn tool_use event dispatch_phase ci_task ci_log ci_sync ingest review model_price repo_model repo_model_change`; `dispatch` is a view; durable vs derived per `docs/fleet-db.md` |
| `~/dev/.fleet/snapshots/` | `fleet-snapshot` (04:30) | infra plan 0011's restic job | the only local second copy of `fleet.db` today |

## What this makes visible

- **Three writers touch a lease** (⑥ assign, ⑦ activate, ⑭/⑮ release or orphan), and the cap in ⑤ counts leases, not processes. A steward waiting on ⑬ is a live lease. Plan 0067 reads the run log's last line to exclude it.
- **The forge is the only cross-host store**, and every read of it goes through `fleetforge`. The implementer session is the one writer to it that is not the orchestrator: it opens the PR itself, which is why ⑨ has to find the PR by marker rather than knowing its number.
- **The run log is the primary record of a run and `dispatch_phase` is its copy**, 120 s behind. A decision that must be current (the cap) reads the log; a decision that can lag (a brief, a phone view) reads the table.
- **A failure has exactly one way back in**: ⑰'s comment, consumed by ⑥. Nothing in ⑮ or ⑯ dispatches. That is the design (a failure handler permitted to retry is a way to make progress nobody authorised), and it is also why a dead run waits for a person.
- **`pages.jsonl` is the audit of everything the fleet said to a human**, and the forge is the audit of everything a human said back.
