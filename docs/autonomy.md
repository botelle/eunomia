# Autonomy

Each repo is `autonomous` or `critical`. The decision is
[ADR-0019](adr/0019-on-an-autonomous-repo-the-plan-is-the-human-approval.md):
on an autonomous repo the plan is the human approval; on a critical repo a human
approves every PR.

| value | meaning |
|---|---|
| `critical` (default) | a human stays on every PR |
| `autonomous` | the merged plan is the approval; the merger may merge |

There is no third value. A repo nobody classified is `critical`: the builtin is
`critical`, and the `default` layer may only be set to `critical`, so no single
command makes every unclassified repo autonomous.

Make a repository autonomous:

```
fleet-config set autonomy autonomous --repo operator/ares
fleet-config unset autonomy --repo operator/ares      # back to critical
fleet-config get autonomy --repo operator/ares --json
```

It is built like `review_bar` ([review-bar.md](review-bar.md), plan 0085):
same storage, same `fleet-setting-changed` event, same ledger rebuild.

## `get --json`

```json
{"key": "autonomy", "repo": "operator/ares", "value": "autonomous",
 "layer": "repo", "source": "repo_setting", "changed_at": "2026-09-29T12:00:00Z"}
```

`layer` is `repo` or `builtin` (or `default`, when it holds `critical`). A read
failure exits 1, prints the reason on stderr and no value; a reader must refuse
on a non-zero exit and never assume either value.

## Who reads it

Plan 0107 (the status poster) and plan 0108 (the merger). This repo only holds
the answer.
