# The dependency structure of the plan set, drawn

*Companion to `docs/plan-dependencies.md` (the three `depends_on` forms and
their clearing rules) and `docs/plan-triage.md` (why a plan's status alone is
evidence, not proof). Implements plan 0047.*

`fleet-plan graph` reads the same front matter `fleet-plan lint` and
`fleet-plan audit` already read, and turns `depends_on` into a picture instead
of forty-two separate files each naming only its own edges. It never edits a
plan, never flips a status, never deletes a `depends_on` entry — it is a
mirror, not an actor.

```
fleet-plan graph [paths...] --format json|mermaid|svg [--out FILE]
```

With no paths, it reads every plan under `plans/`. It exits non-zero when the
plan set contains a `depends_on` cycle — the graph is still emitted; a cycle
is reported, never crashed on, and never silently dropped from the output.

## The three forms stay distinct

A `depends_on` entry is one of three forms, and the graph tags every edge with
which one it is, because they clear by completely different rules:

| form | `to` | clears when |
|---|---|---|
| `plan` | another plan's id | that plan's `status` is `done` **on `main`** |
| `path` | a repo-relative path | that path exists **on `main`** |
| `external` | free text | **never automatically** — only a human deleting the line |

Drawing all three as the same kind of arrow would say the plan set is nearer
done than it is: an `external:` entry looks exactly as clearable as a `path:`
one until a human reads it and decides. `--format mermaid` gives `external:`
its own arrow (`==>`) into its own shape, distinct from a `plan` or `path`
edge; `--format svg` and the JSON both carry the `form` on every edge for the
same reason.

## `--format json`

The shape a service (`operator/lynceus`) serves and every other format renders
from. One node per plan:

```json
{"id": "0016-independent-test-lane", "status": "draft", "repo": "operator/eunomia",
 "paths": [...], "layer": 3, "unreachable": true}
```

One edge per `depends_on` entry, in the order it was declared:

```json
{"from": "0016-independent-test-lane", "to": "roadmap-row-21a-lane-isolation",
 "form": "external", "satisfied": null, "reason": "external"}
```

`satisfied` is a bool for `plan` and `path` edges; always `null` for
`external`, which has no automatic satisfied state to report. `reason` is one
of `unmet`, `abandoned`, `dangling`, `external`, `cross_repo`, or `null` when
the edge is satisfied — the same vocabulary `docs/plan-dependencies.md`
already uses for a blocked dispatch, so a blocked-dispatch event and this
graph never disagree about what a blocker is called.

Top-level: `nodes`, `edges`, `cycles` (a list of cycles, each a list of ids —
empty when the plan set is acyclic), and `errors` (a plan that failed to parse,
reported rather than dropped, matching `fleet-plan audit`'s own rule that
absent must not read as zero).

## Unreachability is computed, not implied

A node's `unreachable` flag is true when its `depends_on` chain — its own
entries, or a plan-id dependency's, transitively — contains an `external:`
entry or a plan-id dependency on an `abandoned` plan: the two things nothing
automatic, or nothing at all, can ever clear. A missing `path:` or a live
`draft`/`ready` plan dependency does **not** make a node unreachable; both can
still land. Colour-by-status is not an answer to "what is actually blocked" —
this flag is.

## `path:` edges are resolved per-repo, and the graph says so

`_plan_dependency_block` (`bin/fleet-watch`) resolves a `path:` entry against
the **dependent plan's own `repo:` field**, never against whatever checkout
happens to be running the check. When a plan's `repo:` differs from the repo
`fleet-plan graph` is run in, this checkout cannot know whether that path
exists on the other repo's `main` — resolving it locally anyway would be a
guess dressed as an answer. Such an edge carries `"satisfied": null,
"reason": "cross_repo"` instead. A reader looking at a multi-repo feature
should not assume this graph is complete across repos: it is complete
*per repo*.

## `--format mermaid`

A `graph TD` Mermaid flowchart, one fenced block, and nothing else — it costs
one code block in a pull request body or a page under `docs/` and needs no
service. Node ids are sanitised (`0016-independent-test-lane` becomes
`n_0016_independent_test_lane`) because Mermaid's own id syntax is stricter
than a plan id; the visible label still reads the real id and status.

## `--format svg`

Nodes are laid out in layers by longest plan-id dependency chain — a plan with
no plan-id dependencies sits in layer 0, and a dependent sits one layer below
the deepest of its own dependencies. Layout is a fixed-size grid, so no two
node boxes can overlap by construction; `tests/test_plan_graph.py` still
asserts it geometrically, on both a small fixture and the full plan set, the
way `tests/test_svg_layout.py` does for the house-style diagrams. An
unreachable node (D5) is outlined in red.

## What this cannot do

It never infers an edge that `depends_on` does not declare — two plans
touching the same file, or a slug that reads like a sequel, draw nothing
between them. If it isn't in `depends_on`, it is not in this graph, on
purpose: the whole value of the artifact is that it shows what the fleet will
actually enforce, not what a reader might guess from prose.
