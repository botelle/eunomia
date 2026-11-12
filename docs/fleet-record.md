# fleet-record — the merge recorder

`bin/fleet-record` writes one change record (ADR-0020; fields in
`docs/change-management.md` §4) to `operator/recordstore` for every PR merged
to its repository's default branch on a `dispatch` repository of `config/repos.conf` (plan 0134). Kind: `docs` (ADR-0014 §1).
It records what the forge shows and judges nothing; plan 0120 reads the records.

## What is recorded, and where

One file per merge: `records/<owner>/<repo>/<YYYY>/<MM>/<merge_sha>.json`, the
year and month taken from `merged_at` **in UTC** (a merge at
`2026-10-01T00:30:00+02:00` is September). The shape is
`config/change-record.schema.json`; a record that fails it is not written and
the run pages. Everything comes from forge reads (PR, reviews with their
`dismissed` and `stale` flags as the forge reports them, the latest status per
context at the head, the plan file on `main` at the merge's first parent,
`status_check_contexts` from `GET /repos/<r>/branches/<default>`, and
`fleet-config get autonomy`). Dismissed, stale and old-SHA approvals are
recorded as they are; filtering them is the checker's job.

A record is never modified, overwritten or deleted. A path already on
`origin/main` is skipped, so a second pass writes nothing even if the forge has
since changed. Writes go through a git clone and an ssh push, never the
contents API. A pass makes one commit as the recorder; a rejected push is
fetched, loses any record now upstream, and is retried once, then pages.

## Default branches

Each repository's `default_branch` is read from `GET /repos/<r>` once per
process and used for the merged-PR filter (`base.ref`) and for
`required_contexts`. A repository whose default is `master` (for example
`operator/evalset`) is therefore covered; a PR merged into any other
branch is not recorded. If the default cannot be read the repository is paged
as unreadable, never assumed to be `main`. The evidence repository's own branch
stays `main` (ADR-0020).

## The ssh remote and the greeting check

The push goes to `ssh://git-recordbot/operator/recordstore.git`. Forgejo's
ssh user is `operator` for every account; the **key** picks the account, and the
key is chosen by the `git-recordbot` host alias in `~/.ssh/config` (the code
never names a key file). Override the alias with `FLEET_RECORD_SSH_HOST`, or the
whole URL with `FLEET_RECORD_REMOTE`.

A catch-all block must exclude the alias, or its `IdentityFile` wins and the
push authenticates as the owner (seen 2026-10-07):

    Host * !git-recordbot
        IdentityFile ~/.ssh/id_owner

Once per process, next to `GET /user`, the recorder runs
`ssh -T -o BatchMode=yes <alias>` and requires the greeting
(`Hi there, <account>!`) to name `FLEET_RECORDER_ACCOUNT`. Any other name, no
greeting, or a failing `ssh -T` writes nothing and pages, naming the account the
forge reported. There is no fallback to another key or remote.

## Why the recorder is separate from the merger

ADR-0020 §2: a witness to a merge cannot be a party to it. The recorder is the
`recordbot` account (`FLEET_RECORDER_ACCOUNT`), takes its token from
`FLEET_RECORD_TOKEN_CMD`, and refuses to run unless `GET /user` returns that
account and it is not any account in `agent_roles()`. It does not import
`fleet-merge`'s checks, uses no author or reviewer token, and calls no model.

## `observed`, `late`, `backfill`

`source` depends only on the time between merge and record. `observed`: written
within `FLEET_RECORD_WINDOW` (default 15 minutes) of `merged_at`. `late`:
anything after, including a catch-up after an outage. `backfill`: written by
`backfill`. A late or backfilled record is never presented as taken at merge
time (ADR-0020 §5).

## Running a backfill

    fleet-record backfill --since 2026-09-29

Builds records for merges since that date that have none, with
`source: backfill`. Idempotent: a second run writes nothing. When a repository
is read completely it writes `backfill/<owner>/<repo>.json` naming when the
backfill ran. `once` **skips a repository with no backfill file** (logging it)
and considers only merges at or after that time, so it never sweeps history and
labels it `late`. A repository with an unreadable merge gets no file, pages, and
is retried by the next backfill.

## `publish`

`fleet-record publish` commits what `fleet-attest` (plan 0120) leaves in
`FLEET_ATTEST_SPOOL`, through the same push path, and `once` runs it at the end
of each pass. Only `reports/**` and `protection/**` are accepted; any other
path (or a symlink) is refused and pages. A path already upstream is refused and
never overwritten. Either kind of refused file is moved to `<spool>/rejected/`,
keeping its path, and pages once; files under `rejected/` are never re-read. Files leave the spool only
after the push succeeds. The checker never holds this key: it can only write to
a local spool, and the recorder is the only account that can push to
`recordstore` (ADR-0020 §3).

## Paging

Through `fleet-watch notify`, only for an identity failure, a repository the
recorder cannot read, a record failing validation, or a push still rejected
after its retry. A normal pass logs one line per record written.

## Owner steps before this is run

- the `recordbot` account exists;
- `operator/recordstore` exists, with its protection;
- `recordbot` has read access on each fleet repository;
- `recordbot` has an ssh key on opshost;
- `recordbot`'s token is in keyvault.

Then run the backfill once by hand and note merges found per repository and any
that could not be read. The launchd unit is plan 0121's.

The repository list is `config/repos.conf` **in the eunomia pin**
(`~/.local/share/pins/eunomia/config/repos.conf`), read through fleet-watch's
`conf_repos()` wherever `fleet-record` is started from, and its first line of
output names that path. Run from `~/dev/eunomia` it would otherwise take the list
from whatever branch the shared clone was left on (ADR-0005); the pin is `main` as
the broker vouched for it. If the pinned file is missing, unreadable or lists no
repository, `once` and `backfill` refuse to run and say so; they never fall back
to the checkout's own file (plan 0136).

Configuration: the repository list is `config/repos.conf`; `FLEET_FORGEJO_URL`, `FLEET_RECORD_TOKEN_CMD`,
`FLEET_RECORDER_ACCOUNT`, `FLEET_RECORD_CLONE` (default
`~/dev/.fleet/recordstore`), `FLEET_RECORD_SSH_HOST` (default `git-recordbot`), `FLEET_RECORD_REMOTE` (full ssh URL override),
`FLEET_RECORD_WINDOW`, `FLEET_ATTEST_SPOOL`.
