# The model policy file

*Plan 0155; ADR-0026 §2 (as amended 2026-10-24), ROADMAP row 22.*

`config/model-policy.json` is a reviewed file that denies a group of models at
positions for a group of repositories. It changes only by a reviewed pull
request: `fleet-models policy` has no write verb and nothing else writes it.
It ships with **no deny rules**, so it changes nothing that runs today.

## The file

```json
{ "v": 1, "model_groups": {}, "repo_groups": {}, "deny": [] }
```

- **Model group**: any of `runners` (names in `RUNNERS`), `families`
  (`Runner.family`), `provenance` (`Runner.provenance`). A runner is in the group
  if it matches any.
- **Repo group**: `{"repos": ["operator/x", ...]}` or
  `{"all_except": ["<repo group>", ...]}`. `all_except` exists so "every
  repository except the bakeoff ones" includes a repository created tomorrow;
  a list of today's repositories would leave a new one allowed, which fails
  open for the rule that needs to fail closed.
- **Deny rule**: `{"models": "<group>", "repos": "<group>", "positions":
  [<position> | "*"], "why": "<text>"}`. `why` is required and printed in every
  refusal.
- There are no allow rules and no precedence: whatever no deny matches is
  allowed, including a runner added after the policy was written.

`fleet-models` reads the file from its own checkout (`config/` beside `bin/`),
which on the fleet is the eunomia pin kept at `main`. No variable or flag points
it elsewhere. A missing file, invalid JSON, an unknown key or `v`, an undefined
group, an unknown runner, family, provenance class or position, or an
`all_except` cycle refuses **every** set and resolve, naming the defect.

## Where it is enforced

1. **Set**: `set_runner()` passes the repository to `validate_runner()`.
2. **Resolve**: `resolve()` does the same with the real repository, so a row that
   got into the table another way still refuses.
3. **Spawn**: `route_implementer()` in `bin/orchestrator` calls
   `fleet-models`' `check_policy()` on the final runner and the plan's
   `repo:` after the override is applied. It sits after the override because
   `resolved_implementer_env()` returns early when `EUNOMIA_IMPL_RUNNER` is set,
   so the bakeoff one-liner would otherwise never reach `fleet-models`. A plan
   with no `repo:` is treated like `default`.

### The `default` row

At set time `default` is treated as a member of every `all_except` group and of
no `repos` group, so a runner denied anywhere "except the bakeoff repositories"
cannot become the owner default. At resolve time the real repository is checked,
so a `default` row denied for one repository refuses there.

## Reading it

```
fleet-models policy check <repo> <position> <runner>   # "allowed" (0) or the rule (4)
fleet-models policy show --json                        # the parsed file
```

## Not in this file yet

The ADR-0026 §6 open-weights box stays in `check_open_weights_box()`. Moving it
here would turn an always-on control into a never-on one until a box rule is
written; that is its own plan. Nor does lynceus filter what it offers: a set the
policy refuses fails with the rule's text.

Follow-ups: the hosted-runner plan's first deny rule; lynceus offering only what
the policy allows; the box moving into the file.

## Proving the alarm

With `validate_runner(runner, position, repo)` in `set_runner()` reverted to
`validate_runner(runner, position)` in a scratch edit, `tests/test_model_policy.py`
went from 20 passing to **16 failed, 4 passed** (2026-10-27) — the deny, all_except,
default and defect-on-set tests all failed. Restored afterwards.
