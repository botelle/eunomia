# fleet-attest — the change reconciler

`bin/fleet-attest` tests every change record in `operator/recordstore`
against checks A1–A8 of `docs/change-management.md` and reports what fails. On
the first of each month it writes an attestation for the previous month.
Kind: `docs` (ADR-0014 §1). It is read-only: it holds neither the recorder's key
nor its token, pushes to no remote, writes no record and no disposition, and
calls no model. Its outputs go to `FLEET_ATTEST_SPOOL`
(default `~/dev/.fleet/recordstore-spool/`), which `fleet-record publish`
commits (`reports/**` and `protection/**` only).

## The checks

The period is a SHA range, never a date. For each repository of the pinned
`config/repos.conf`, `check` walks the first-parent history of that repository's
own `default_branch` (read from `GET /repos/<r>`; `master` is walked as `master`)
from the tip named in the latest report to the current tip. A repository with no
report and no `--from <repo>=<sha>` is skipped and reported (`rule`, A1, "no
start SHA"); the others still run. If no repository has a start, `check` refuses.

| id | tests | class on failure |
|---|---|---|
| A1 | every first-parent commit equals some record's `merge_sha`; the start SHA is still an ancestor | `unrecorded`, `history-rewritten` |
| A2 | an agent merge has a `plan`, `status_at_merge: ready`, and the plan commit merged by the human merger | `rule` |
| A3 | an agent merge has an `APPROVED` review at `head_sha` (not dismissed, not stale, `model` set) from both `reviewer` and `second_reviewer` | `rule` |
| A4 | no agent merged a change it authored; no approver authored it; the recorder holds no role | `rule` |
| A5 | every `required_contexts` entry is `success` in `statuses[]` | `red-ci` |
| A6 | the merger is the human, or the agent merger on a repository autonomous at `merged_at`, within ADR-0019 §4's limits judged by path | `rule` |
| A7 | every autonomy change in the period, and any change to the default-branch protection rule since the last snapshot | `control-change` |
| A8 | no commit in the evidence repository since the last report modified or deleted a file under `records/` | `record-edited` |

Notes that are easy to get wrong:

- Matching is by `merge_sha` only. The repository's merge-style settings are not
  read; a first-parent commit with no record is `unrecorded` whatever merge
  style made it.
- Autonomy at `merged_at` comes from the ledger's `fleet-setting-changed`
  events (`events.jsonl` and its archives), never from the record's `autonomy`
  field and never from `fleet.db`.
- An agent-merged PR is a `tests` PR if every file is under `tests/lane/`, else a
  `src` PR. A `src` PR fails A6 if it touches `docs/adr/**`, `tests/lane/**`, a
  file in `ENFORCING`, `launchd/`, `.forgejo/`, any `plans/` file other than its
  own plan, or changes its own plan by more than `status: ready` → `done`
  (read from git, `<merge>^1` against `<merge>`). Operational docs under `docs/`
  are `src` kind and pass.
- `ENFORCING` is a deliberate copy of `bin/fleet-merge`'s; a test holds them equal.
  A2–A6 share no code with `bin/fleet-merge`.
- Owner merges are never exceptions. The report counts them by merger, how many
  had a reviewer approval at the head, and how many were authored by the owner.
- A8 counts records with `source` `late` or `backfill` separately; they are not
  exceptions. Expect them for the time before the recorder ran.
- An A7 snapshot is written to `protection/<owner>/<repo>.json`. The first is a
  baseline, not an exception.

## Exceptions and dispositions

Every exception has a stable id, `<check>-<owner>--<repo>-<key>`, with no `/`.
`<key>` is the commit's first 12 characters for a per-commit exception; for a
repository-level one it is the 12 characters of the tip (`history-rewritten`,
"no start SHA"), of the snapshot (a protection change) or of the ledger line (an
autonomy change). Classes are listed in `docs/change-management.md` §5.

To give an exception a disposition, the owner adds `dispositions/<id>.md` to
`operator/recordstore` by pull request. The report shows it `disposed` if that
file exists, `open` if not. Nothing here writes one.

`check` pages once per run, through `fleet-watch notify`, when there are open
exceptions it has not paged before (ids kept in
`$EUNOMIA_FLEET_DIR/fleet-attest-seen.json`). The page gives counts by class.

## Reading a report

`reports/YYYY-MM.md` and `.json` carry the same facts: the period (per
repository the tip it started from and attested through, the evidence repository
SHA, the ledger cursor), merges, fully evidenced merges and records by `source`
per repository, exceptions by class with each id and its state, and the eunomia
commit that produced it. The next `check` starts from those tips. A report
attests through the tip at the time it ran, not through a date.

`report --month previous` is the calendar month before today, UTC. A month with
a report is refused; `--rerun` writes `YYYY-MM.rN.md/.json` (next free `N`,
starting at 2) over the same starting points and names the report it follows.

## Running `check` for an ad-hoc period

```bash
bin/fleet-attest check --from operator/eunomia=<sha> --from operator/ladon=<sha>
bin/fleet-attest check --no-page          # read the result without paging
```

`--from` overrides the start for the named repositories. Setup: `FLEET_ATTEST_TOKEN_CMD`
(default `~/bin/fetch-forgejo-token.sh`, the implbot PAT: read-write, though fleet-attest only reads with it), `FLEET_ATTEST_REMOTE_TEMPLATE`
(`{repo}` placeholder; default the ssh remote), `FLEET_ATTEST_EVIDENCE_REMOTE`,
`FLEET_ATTEST_CLONES`, `FLEET_ATTEST_EVIDENCE`, `FLEET_ATTEST_SPOOL` and
`FLEET_ATTEST_STATE`. The clones are fetch-only.

The launchd unit that schedules this is plan 0121's.
