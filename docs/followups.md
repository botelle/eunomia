# Deferred review findings

An approval can carry MEDIUM and LOW findings that do not block the merge. Those
are filed as an issue titled `Follow-ups from review of #N`, and left there.
Measured 2026-09-25: about 280 of them open across the fleet repos, about 650
unchecked items, some already fixed, some about code that no longer exists.
`bin/fleet-followups` (plan 0083) is the periodic pass that looks at them, and
the owner decides each one.

## Lifecycle

1. **Filed** by talos's `file-followups.py`, as the implementer account, when a
   review approves with findings below the repo's review bar (`docs/review-bar.md`).
   Later reviews of the same PR append a `### Reviewed …` comment.
2. **Triaged** by `fleet-followups triage`, on a per-repo timer
   (`config/followups.conf`, hours; `-1` is off and is every repo's default).
   The unit (`launchd/org.eunomia.fleet-followups.plist`) fires hourly, and a
   run takes only the repos whose interval has elapsed. With every repo off it
   reads nothing from the forge, so installing the unit spends nothing until a
   repo is switched on. One comment per issue: for each
   unchecked item, `holds`, `gone` or `unclear`, with the evidence; the `main`
   SHA and the runner; a recommendation.
3. **Decided** by the owner, with a verb. Nothing is ticked, edited or closed
   before that. A `gone` is a claim; `close` is the decision.

## What a run does

Only issues whose title is `Follow-ups from review of #N` **and** whose author is
an implementer account, in a repo listed in `config/repos.conf`. Any other issue
is not touched and not read.

Verbs are acted on first, and an issue acted on is not triaged in the same run.
Then up to `followup_triage_batch` issues are chosen, in three tiers:

1. never triaged, oldest first (an issue whose triage failed sorts after fresh ones,
   so one that fails every run cannot starve the rest);
2. a `### Reviewed` section from the implementer appended since the last triage;
3. a triage older than 30 days.

Re-triage of an unchanged issue never displaces a first triage. The tiers read
comment order, never `updated_at`, which moves on every comment, the owner's
`keep` included.

Per issue, one read-only session runs on a fresh shallow checkout of `<repo>@main`
under ADR-0005's reader vector: an empty temp cwd outside any repository,
`--add-dir <checkout>`, `--restricted`, `--tools Read,Grep,Glob`,
`--strict-mcp-config`, a hooks-only `--settings` string re-applying
`fleet-secret-guard`, and no `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`. There
is no shell and no network.

The runner is the repo's `followup_triage` seat (plan 0186), a `bin/fleet-models`
position resolved per repo — `fleet-models set <repo> followup_triage <runner>` —
independent of `mediator` and every other seat. Priority, highest first: an
explicit override passed in-process (a test seam), then `FLEET_FOLLOWUP_RUNNER`
(kept as the operator's escape hatch, sitting above the seat the same way
`EUNOMIA_IMPL_RUNNER` sits above `implementer`), then the seat itself, then this
program's own prior default, `sub-sonnet` — so an installation that configures
neither behaves exactly as it did before the seat existed. A local runner is
refused outright. A non-Claude runner is refused too: the reader vector above is
a `claude` CLI session with a pinned `--model`, and a non-Claude runner would
need its own confined-reader sandbox, which does not exist yet (plan 0185 tried
to build one for `mediator` and was reverted when review found its confinement
unmeasured; tracked on eunomia #1271) — refused with that reason rather than run
unconfined.

The session authenticates with the vault's Claude token, fetched once per run
through `bin/orchestrator`'s `claude_token()` (`FLEET_CLAUDE_TOKEN_CMD`, plan
0116) and put only in the child's environment. There is no Keychain fallback:
a failed fetch stops the run before any session, and no issue is marked as
failed for it. A session that fails for any other reason — a timeout, an
authentication error, a crash — has that reason, not just "no output", in
`followups.log`.

## What the wrapper checks

The model's output is checked, not believed.

- Unparseable output or a missing item: a `triage failed` comment, no verdicts,
  a retry next run.
- Every `holds` and `gone` needs evidence found in the checkout: the cited
  `path:line` with at most 3 quoted lines that appear at that path within a few
  lines of the cited line. The comment shows the lines as they are in the
  checkout.
- A `gone` may instead say `absent: <path>`, which holds only if the path is not
  in the checkout **and** existed at the head commit of the PR the issue was
  filed from (`git cat-file -e <head>:<path>` after fetching `refs/pull/N/head`).
  A path that never existed proves nothing.
- Anything that fails its check becomes `unclear`, and the comment says so.

Recommendation: **close** when every item is `gone`, else **fix** when any item
`holds`, else **review**. The comment is written from a template; model prose is
never posted.

## Verbs

Read from a top-level comment newer than the latest triage comment, from
`FLEET_OPERATOR_UID` only (unset disables all verbs). A verb is a **whole line**:
the word, optionally in backticks. `close` inside a sentence is not `close`; a
signature line Minos appends after it does not matter. Two different verbs in
one comment: none acts, and the next triage says so. The newest verb comment wins.

| verb | effect |
|---|---|
| `close` | ticks every item marked `gone`, comments a link to the triage, closes the issue |
| `keep` | nothing; the issue is looked at again after 30 days |
| `fix` | not built yet (plan 0084); the run says so once |

## The budget

`fleet-config set followup_triage_batch <0..50>` (default 10; `0` pauses the job,
and a paused run reads nothing from the forge). Each issue is a model session:
about 280 issues at 10 a day drain in about four weeks. Raise it only once the
verdicts have earned it (plan 0083 §5: record, for 20 `gone` and 20 `holds`
verdicts, whether the owner's decision agreed, and the per-issue token cost
from `followups.log`).

## Events and page

`followup-triaged` (issue, sha, counts, recommendation) and `followup-closed`
(issue, verb author), both in SPEC.md. One page per run that triaged anything:
`follow-ups: N triaged — C close, F fix, R review`.

## Cost of a run

One issue-comment read per matching issue per run (the verbs and the tiers both
need them). That is roughly 280 reads a day against the LAN forge.
