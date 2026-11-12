# The review bar

Each repo has a `review_bar`: the lowest severity that still blocks an
approval. It is set per repo, from `fleet-config` (plan 0085).

| value | blocks | deferred to the follow-up issue |
|---|---|---|
| `high` (default) | HIGH | MEDIUM, LOW |
| `medium` | HIGH, MEDIUM | LOW |
| `low` | HIGH, MEDIUM, LOW | nothing |

There is no fourth value and no `none`: HIGH always blocks.

```
fleet-config set review_bar medium --repo operator/lynceus
fleet-config set review_bar low --repo default      # every repo without its own
fleet-config unset review_bar --repo operator/lynceus  # follow default again
fleet-config get review_bar --repo operator/lynceus --json
```

`--repo` is required for this key and refused for `dispatch_cap` and
`retry_limit`. `default` can be set but not unset.

## `get --json`

```json
{"key": "review_bar", "repo": "operator/lynceus", "value": "medium",
 "layer": "repo", "source": "repo_setting", "changed_at": "2026-09-28T12:00:00Z"}
```

`layer` is `repo`, `default`, or `builtin`. With no rows anywhere the answer is
`high` with `layer: "builtin"`, `source: "builtin"`, `changed_at: null` — which
means the tables were read and hold nothing for this repo or `default`.

**A read failure is an error, not a default.** If `fleet.db` is missing, locked
past the timeout, or lacks the tables, `get` exits non-zero, prints the reason
on stderr and no value. A reader must refuse on a non-zero exit and never
assume `high`. The tables are created by the first `fleet-config set`, `unset`
or `list`.

## Who reads it

Talos plan 0010 (the reviewer) applies the bar, and lynceus plan 0015 shows and
edits it. This repo only holds the answer; the sentence in talos's
`dispatch-review.py` verdict rubric ("APPROVE — no HIGH findings remain") is
replaced there, not here.

The value is durable: every change is one `fleet-setting-changed` event with
`repo` in its detail (`new: null` for an unset), and `fleet-collect` rebuilds
`repo_setting` from those events if `fleet.db` is lost.
