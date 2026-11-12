# Change management controls

This document describes the change controls the fleet claims, the change-record
field that evidences each, and the reconciler check that tests it. It is a
description, not a policy. The binding rules are ADR-0019 and ADR-0020 and the
code that refuses; this page cites their clauses and does not restate them
(ADR-0010 §1). Kind: `docs` (ADR-0014 §1).

## 1. Scope

Every repository in `FLEET_WATCH_REPOS`, whether autonomous or critical
(ADR-0019 §1).

2026-09-29 is where the evidence window starts. It is not the date the
controls came into force. Each control is in force from the date its own
enforcing code merged to `main`, given in the table in §3.

## 2. Limits

- The records bind the agent accounts.
- They are not tamper-evidence against the forge administrator, who is also
  the owner and the human merger, because the evidence repository is on the
  same forge.
- For the owner's own merges, a record is a dated statement made by a program
  on the owner's infrastructure. It is not an independent witness.

## 3. The control table

`in force from` was filled on 2026-10-06 from `git log --first-parent main`
(merge commit SHA and date), never from an ADR's date. `not yet in force`
means the enforcing code is not on `main`. Branch-protection rules live in
Forgejo, outside this repository, and this run did not read them, so they are
not dated here.

| id | control | implemented by | in force from | evidence fields | check |
|---|---|---|---|---|---|
| C1 | Every change to `main` arrives through a merged pull request | branch protection; ADR-0020 §4 | not yet in force (recorder and check not on `main`; branch protection not verified in this change) | `merge_sha`, `pr` | A1 |
| C2 | Planned work on an autonomous repo was authorised by the owner | ADR-0019 §2, §4 | not yet in force | `plan.id`, `plan.status_at_merge`, `plan.commit`, `plan.commit_merged_by` | A2 |
| C3 | Code an agent merges was approved at its head by both reviewer accounts (the model is recorded, not compared); for owner merges, the review each had is recorded | ADR-0019 §3; ADR-0004; ADR-0020 §1 | not yet in force | `reviews[]` | A3 |
| C4 | No agent account approves or merges a change it authored; every owner merge is recorded | ADR-0019 §4; ADR-0020 §2 | not yet in force | `author`, `merged_by`, `reviews[].account` | A4 |
| C5 | Changes pass required tests before merge | ADR-0019 §4; branch protection | not yet in force (branch protection not verified in this change) | `statuses[]`, `required_contexts` | A5 |
| C6 | Only the authorised merger merges, within its limits | ADR-0019 §2, §4 | not yet in force | `merged_by`, `autonomy`, `files` | A6 |
| C7 | Changes to the controls themselves are recorded | ADR-0019 §1; ADR-0020 §3 | not yet in force (the autonomy setting and its ledger entries merged to `main` 2026-09-30, `1a3dd0b`; the recorded change events and protection snapshots are not on `main`) | autonomy change events; protection snapshots | A7 |
| C8 | The record is complete and unedited | ADR-0020 §3, §5 | not yet in force | `source`, `supersedes`, the evidence repository's history | A8 |

## 4. The change record

One record per merge to `main` (ADR-0020 §1). Fields:

- `schema`: record schema version.
- `repo`: the repository, `owner/name`.
- `pr`: pull request number.
- `title`: pull request title.
- `base_ref`: the branch merged into.
- `head_sha`: the pull request head at merge.
- `merge_sha`: the merge commit on `main`.
- `merged_at`: when the forge says it merged.
- `merged_by`: the account that merged.
- `author`: the pull request's author account.
- `files`: paths the change touched.
- `plan`: the plan the PR carried, or null, with
  - `id`: the plan id;
  - `status_at_merge`: the plan's `status:` on `main` at merge;
  - `commit`: the commit that last changed the plan file on `main`;
  - `commit_merged_by`: the account that merged that commit.
- `reviews[]`: each review, with
  - `id`: the review id;
  - `account`: who posted it;
  - `state`: approved, changes requested, and so on;
  - `commit_id`: the commit it was made at;
  - `submitted_at`: when;
  - `dismissed`: whether it was dismissed;
  - `stale`: whether it was not at the head;
  - `model`: the model that made it;
  - `session_uuid`: the session that made it.
- `statuses[]`: each commit status at the head, with
  - `context`: the status context;
  - `state`: its state;
  - `creator`: the account that posted it;
  - `created_at`: when.
- `required_contexts`: the contexts branch protection required.
- `autonomy`: the repository's autonomous or critical setting at merge.
- `recorded_at`: when the record was written.
- `recorder_rev`: the recorder's revision.
- `source`: `observed`, `late` or `backfill` (ADR-0020 §5).
- `supersedes`: the record this one corrects, or null (ADR-0020 §3).

## 5. Exceptions

An exception is anything a check reports. Each falls into one class:

- `unrecorded`: a first-parent commit on `main` with no record. Commits that
  arrive inside a merge are covered by the merge's record.
- `history-rewritten`: the `main` tip attested last time is no longer an
  ancestor of `main`.
- `red-ci`: a required context was not `success` at the head.
- `control-change`: autonomy flipped, or a protection rule changed.
- `record-edited`: an existing record was changed or removed.
- `rule`: any other failed check.

The owner gives each exception a disposition by pull request into
`operator/recordstore`. An exception without one is open.

An owner merge is not an exception. ADR-0019 §2 has the owner merge most PR
kinds. The report counts merges by merger instead.

## 6. Attestation

A monthly report names:

- its period;
- merges per repository, split by merger;
- how many merges are fully evidenced;
- exceptions by class, and which are open or disposed.

The reports live in `operator/recordstore` (ADR-0020 §3).
