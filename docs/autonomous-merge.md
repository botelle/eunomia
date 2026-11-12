# Autonomous merge: the three identities

Decision: [ADR-0019](adr/0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md),
which amends ADR-0004. Plan 0106 implements the verdict and dispatch halves.

## Identities

| Role (`fleetlib.agent_roles()`) | Default | Env override | May |
|---|---|---|---|
| `reviewer` | `revbot` | `FLEET_REVIEWER_ACCOUNT` | approve (agent) |
| `second_reviewer` | `rev2bot` | `FLEET_SECOND_REVIEWER_ACCOUNT` | approve (agent) |
| `agent_merger` | `mergebot` | `FLEET_AGENT_MERGER_ACCOUNT` | merge, only behind `eunomia/deps` |
| `merger` | `operator` | `FLEET_MERGER_ACCOUNT` | the human: merges plans and ADRs |
| `author` | `implbot` | `FLEET_AUTHOR_ACCOUNT` | open PRs; never approve or merge |

All but `merger` are agent accounts (`agent_accounts()`). The overrides are read
in the **broker's** environment on vaulthost, like `FLEET_AGENT_ACCOUNTS`; setting
them on the watcher host does nothing.

## The rule `main_is_protected()` accepts

Decided from the protection rule alone (the broker has no `fleet.db`).

- **Merge lane.** No agent may merge, except `agent_merger`, and only when the
  rule requires the status `eunomia/deps` (`enable_status_check` true and
  `status_check_contexts` containing exactly that string — no prefix, glob or
  case folding).
- **Approvals lane.** With `required_approvals >= 2`, `reviewer` and
  `second_reviewer` may reach the count between them when either no agent can
  merge, or the rule requires `eunomia/deps`. Any other agent on the approvals
  whitelist is refused, and the rule must be satisfiable by the whitelisted
  accounts. At `required_approvals < 2` today's sufficiency rule applies.
- Every other lane (push whitelist, deploy keys, teams, `apply_to_admins`, stale
  approvals, `unprotected_file_patterns`) is unchanged.

The fleet's current shape, `[revbot, rev2bot, operator]` at 2 with merge
`[operator]`, is valid.

## Dispatch requires a human merge

Before dispatching a `ready` plan, fleet-watch reads the newest commit on `main`
touching `plans/<id>.md` and the PR it landed through. That PR's `merged_by`
must be the `merger` account. Otherwise (merged by another account, or no PR
found) the plan is refused and pages. Approvals, titles and statuses are not
trusted.
