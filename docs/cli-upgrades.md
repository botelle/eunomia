# Upgrading codex-cli

A new codex-cli version is installed side by side with the linked one and
checked by `bin/fleet-cli-canary` (plan 0208) before the owner relinks
`/opt/homebrew/bin/codex` to it. The canary never relinks, installs,
upgrades or downloads anything — picking the fleet's CLI is the owner's call,
and a download is new code reaching the host, which a person checks against
the release.

## Take codex out of Homebrew first, once

Today brew holds a receipt for the codex cask
(`/opt/homebrew/Caskroom/codex/.metadata/`). Any `brew upgrade` — including
one run for a wholly unrelated formula — would see that receipt, install the
next codex release, relink `/opt/homebrew/bin/codex` to it with no canary,
and delete the version brew thinks is now old, which is this procedure's
rollback copy.

Run once, before the first canary-gated upgrade:

1. Copy every installed version directory aside (`/opt/homebrew/Caskroom/codex/*`
   to a safe location outside Homebrew's tree).
2. `brew uninstall --cask codex`.
3. Restore the directories to `/opt/homebrew/Caskroom/codex/<version>/`. Both
   `config/codex-tester.sb` and the installed reviewer profile
   (`/usr/local/libexec/fleet/share/codex-review.sb`, operator/talos) already
   allow reads under that path, so the install stays readable by every
   confined run that needs it.
4. Recreate the `/opt/homebrew/bin/codex` symlink, pointing at the version
   that was linked before step 2.
5. Check `brew list --cask` no longer lists `codex`, and that
   `codex --version` reports the same version as before.

`bin/fleet-cli-canary`'s report carries a `brew` field for exactly this: if
`codex` is ever back in `brew list --cask`, the report says so, because the
next `brew upgrade` — for codex or for anything else — will relink it again
with no canary in between.

## The weekly cadence

- Weekly, the owner checks the GitHub releases page for a new codex-cli
  release.
- Download the release tarball by hand, check its sha256 against the
  release, and unpack it to `/opt/homebrew/Caskroom/codex/<version>/` —
  beside the linked version, never over it.
  (2026-11-05: `brew upgrade` linked codex-cli 0.166 straight in with no
  canary, and every confined codex run then failed at start-up — the
  failure `bin/fleet-cli-canary` exists to catch before a relink, not after
  one.)
- Run the canary:

  ```
  bin/fleet-cli-canary codex --candidate /opt/homebrew/Caskroom/codex/<new version>/bin/codex
  ```

  Read the report it prints the path to. Relink only on a pass.
- Relink by repointing the `/opt/homebrew/bin/codex` symlink at the new
  version's `bin/codex`. This program never does that step; it is the
  owner's.
- Roll back a bad relink by pointing the symlink back at the previous
  version, which stays installed (never deleted) until the next upgrade
  passes its own canary.
- Upgrade sooner than the weekly cadence for a model the fleet wants to use
  or a security fix. Otherwise a week with no change is fine — nothing times
  out or expires on its own.

## The positive control

Before trusting a `fail`, confirm the canary can report a `pass` at all on
this host, on the CLI already linked and working:

```
bin/fleet-cli-canary codex --startup-only --candidate /opt/homebrew/bin/codex
```

`--startup-only` is the one case where the candidate may be the linked CLI:
it skips step 1's same-version refusal and step 4's test lane, and runs only
the flag and start-up checks. A `fail` here, on a CLI the fleet already runs
every day, means the canary itself — or the host, or a sandbox profile — has
drifted, not that the candidate is bad.
