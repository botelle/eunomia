# CI lanes

There are two lanes, both in `.forgejo/workflows/`.

- **The suite** (`ci.yml`, `CI / tests`): every test, the `bin/` parse check and
  the typecheck. It takes about four minutes. It runs on every push except one
  whose changed paths all match `paths-ignore`.
- **The fast lane** (`plans.yml`, `Plans / lint`): runs on pushes that touch
  `plans/**` or `docs/adr/**`. It runs `fleet-plan lint` and the suite tests that
  read those trees, in seconds. Its status context is unchanged, so Minos and
  `dispatch-review.py` read it as before.

## The rule

**A path leaves the suite only with every suite check that reads it.**

`plans/**` satisfies it: `fleet-plan lint` is what `test_repo_plans_are_valid`
runs, and the other tests that read the live `plans/` run in the fast lane.

`docs/adr/**` satisfies it: the only suite tests that read real ADRs are
`tests/test_adr_status.py` and
`tests/test_orchestrator_session.py::test_the_zone_rule_matches_the_documented_rule`,
and both run in the fast lane. An ADR-only change therefore gets a real
`Plans / lint` green, and a red unrelated test can no longer block it.

The rest of `docs/` does not satisfy it, so it stays in the suite:
`tests/test_fleet_cr.py` executes every fenced block in `docs/launch-preamble.md`
and `tests/test_fleet_heartbeat.py` executes `docs/takeover.md`. A docs-only push
with a broken command would skip the suite, merge on a green, and surface as a red
`main` under the next author.

## One list

`COVERED_BY_THE_FAST_LANE` in `tests/test_ci_workflow.py` maps each ignored prefix
to the suite tests the fast lane runs for it. `plans.yml` reads that list with
`python3 tests/test_ci_workflow.py --fast-lane-tests`, and
`test_nothing_the_suite_reads_is_ignored_by_ci` fails, naming file and line, when a
test reads a real file under an ignored prefix and is not on the list. It compares
full prefixes, never directory roots.

To move another path out of the suite: add the glob to `paths-ignore` and
`plans.yml`'s `paths`, and list the tests that read it.
