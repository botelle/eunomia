# Secret guard — two layers against the leak-into-transcript class

*Three incidents, one mechanism (CF Access token 2026-08-17, `GITHUB_WEBHOOK_SECRET`
2026-08-18, tunnelhost cloudflared token 2026-08-27). Convention failed three times;
this is the enforcement. Roadmap row 13's read-side, built after incident #3.*

## The two layers

**`fleet-secret-guard` — prevention (PreToolUse hook).** Blocks a tool call before it
runs when it would read a protected credential location: a `Bash` command containing
a secret path token or a known credential-dumping command (`systemctl cat …cloudflared`
was incident #3), or a `Read`/`Grep` targeting one. Deny rules live in
`config/secret-patterns.conf`, versioned — extending them is a PR. Exit 2 blocks;
the refusal names the sanctioned alternative and **never echoes the file's contents**.

**`fleet-leak-watch` — detection (launchd, 2 min).** Prevention is never perfect: a
secret can arrive in tool *output* the guard couldn't predict. The watcher scans new
`tool_result` content for the credential shapes in the families table below and
ntfys on first sight — rule, file, line, **never the matched text**. The shape list
is not a literal in the script: it is the `content` rows of
`config/secret-patterns.conf`, the same file the CR content gate reads, and the
script keeps built-in fallback shapes only for a conf it cannot read (it says so on
stderr). Detection-in-minutes plus rotation is the
honest backstop, not a promise of zero leaks.

## Credential families (plan 0071)

One versioned list: the `content` rows of `config/secret-patterns.conf`, with an
optional fourth column, the tier. `always` rows feed the watcher, `redact()` (PR
comments and transcripts) and the CR gate; `paranoid` rows feed the watcher only
under `--paranoid` and `redact_transcript()`. A row without the column is `always`.
The tier is a measurement: `redact()` runs over PR comments, where a head SHA is
what an approval binds to (SPEC §4.6), so nothing that matches a SHA or a commit
URL may be `always`.

| family | rule name | tier | note |
|---|---|---|---|
| GitHub tokens (`ghp_ gho_ ghu_ ghs_ ghr_ github_pat_`) | `github-pat` | always | |
| `sk-…` keys (OpenAI and other hyphenated) | `openai-key` | always | |
| Anthropic API key / Claude OAuth token | `anthropic-key` | always | also matched by `openai-key`; named so the alert says what to rotate |
| Slack | `slack-token` | always | |
| AWS access key | `aws-key` | always | `AKIA` and `ASIA` |
| JWT | `jwt-or-tunnel` | always | requires the dot |
| Cloudflare tunnel token | `cf-tunnel` | always | base64 JSON, no dot; not covered before 0071 |
| PEM private key | `private-key` | always | |
| OpenBao service token | `bao-token` | always | `hvs.` |
| OpenBao legacy token | `bao-legacy` | always | `s.` + 24, at least one digit |
| Forgejo PAT, strong cue | `forgejo-pat` | always | `Authorization: token` or `token=` before 40 hex |
| Forgejo PAT, weak cue | `forgejo-pat-context` | paranoid | `forgejo…`/`forge.example…` within 40 chars of 40 hex; matches a commit URL |
| angelia token, context | `angelia-token` | paranoid | 59 hits over 45 files on 2026-09-29 |
| `token: value` prose | `assignment` | paranoid | |
| any 40+ hex | `long-hex` | paranoid | every git SHA |
| Forgejo PAT, bare | none | **unreachable** | 40 hex with no prefix is every git SHA (2013 hits, 932 files, 2026-09-22) |
| angelia token, bare | none | **unreachable** | `token_urlsafe(32)`: 43 urlsafe chars, no prefix (1222 hits, 482 files) |
| Twilio auth token | none | **unreachable** | 32 hex, no prefix. `AC…`/`SK…` are SIDs, not secrets |

The unreachable rows are unreachable because the credential carries no prefix or
structure, not because nobody looked. Do not file them again as a gap.

Corpus figures (`fleet-leak-watch --dry-run`, counts only): 855 files / ~10 hits on
2026-09-10 (the old docstring figure, stale); 2133 files / 63 hits / 23 synthetic
suppressed on 2026-09-22; 2790 files / 90 hits / 23 suppressed on 2026-09-29 before
plan 0071 and 133 hits / 25 suppressed after.

A change to the rows is live for the watcher only when the eunomia pin advances,
and for the guard only after a root reinstall (below). The guard ignores `content`
rows, so it needs the reinstall only to stay byte-identical with `main`.

## What is and isn't covered

- **Covered:** reading a secret file or dumping a credential via command; a leaked
  token landing in tool output.
- **Covered, as a text rule only (plan 0074):** a login-Keychain password read
  printed into the transcript, by two `cmd` rows. `keychain-read` refuses
  `security find-generic-password` and `find-internet-password` with a display flag
  (`-w`, or `-g` which prints on stderr, including clustered `-ws`) and
  `keychain-dump` refuses `dump-keychain -d`. Attribute-only forms (`-s` alone,
  `-G`, `-D`, bare `dump-keychain`, `-r`) and `add-generic-password` (a write) pass.
  **This does not close the Keychain read.** It is matching over a shell string: a
  `python3 -c` calling the Security framework through `ctypes`, a compiled helper,
  or any program that reads the Keychain itself matches nothing, and a session that
  knows the rule can avoid the word `security`. What covers that is a sandbox and a
  dedicated uid; neither exists yet.
- **Covered, as a text rule only (plan 0109):** a session naming
  `fetch-forgejo-mergebot-token.sh` or `fetch-forgejo-rev2bot-token.sh`, refused by the
  `mergebot-token-helper` and `rev2bot-token-helper` `cmd` rows (ADR-0019 §4-§5). The
  intended holders are `bin/fleet-deps` and `bin/fleet-merge` (mergebot) and talos's
  `dispatch-review.py` (rev2bot); nothing refuses them or the launchd units.
  **This is a command-string guard, not a uid boundary.** A program a session
  launches can still run the helper, as it can the revbot helper today; the
  uid boundary for mergebot and rev2bot is still open work, and talos's `reviewer-uid/`
  pattern is where it would apply.
- **No carve-out.** The conf has no allow kind and no precedence, so an exception
  can only be a negative lookahead on typeable tokens, and round 1's was bypassed
  three ways in one command. The fleet's review dispatch, which today reads the
  token with `security find-generic-password -w`, is refused by this rule until it
  takes its token from `~/bin/fetch-claude-oauth-token.sh` (hosttools plan 0002).
  That helper is the sanctioned path for that credential.
- **Deliberate gaps, do not file them again:** `security export` (key material, not
  password items; "every type except the public ones" is exactly what a deny-only
  conf cannot say, so it needs an allow kind in `bin/fleet-secret-guard`, not a
  cleverer regex); `find-key` (`-w` there is a search predicate, "can wrap"); and
  `find-certificate` (no `-w`; `-p` prints a public certificate).
- **Not covered:** a secret in a *tool_use input* or quoted in *assistant prose* — the watcher scans `tool_result` content only (that is where a leaked read lands, and it keeps the scan fast). *Writing* a secret into a file — that is the seed-script flow
  (row 13's infra half, still open). And a session that legitimately needs a secret
  value: it goes through a **pipe helper** (`fetch-forgejo-token.sh` streams straight
  into a curl header, never stdout) or a service's keyvault pull at startup. Those
  read the secret *internally*, invisible to the guard by design — that is the whole
  reason they exist.

## No override

There is no env var, flag, or comment that bypasses the guard from inside a session.
An override the model can invoke is not a control. If a block is wrong, the fix is a
PR to `secret-patterns.conf`; the override lives with the human, outside any session.

## Install & verify (§8.3 — see each layer fire before trusting it)

**The live guard is a root-owned copy, not the pin.** Since 2026-09-10 the hook
in `~/.claude/settings.json` runs
`/usr/local/libexec/fleet/bin/fleet-secret-guard`, and that directory is
`root:wheel`, read-only:

```
/usr/local/libexec/fleet/
  INSTALLED                    installed_at, pin_commit, exe_sha256, conf_sha256
  bin/fleet-secret-guard       -r-xr-xr-x root wheel
  config/secret-patterns.conf  -r--r--r-- root wheel
```

The guard resolves `config/secret-patterns.conf` relative to its own path, so
the deny rules it applies are the ones beside it, not the ones in any checkout.
Before 2026-09-10 the hook pointed at `~/.local/share/pins/eunomia`; before
2026-09-07 at `~/dev/eunomia` itself, which executed whatever branch a session
last left checked out there. Both are gone. The reasoning is
[ADR-0005](adr/0005-pinned-repo-reads.md) turned on the guard — address a
commit, not a directory — plus the point #222 made: a copy the session's own
user can write is a copy the session can replace. Ownership is the control.

The hook, as installed:
```
{"hooks": {"PreToolUse": [{"matcher": "Bash|Read|Grep|Write|Edit",
  "hooks": [{"type": "command",
    "command": "python3 /usr/local/libexec/fleet/bin/fleet-secret-guard"}]}]}}
```

`Write` and `Edit` are in that matcher for a different reason than the other
three, and the difference matters if anyone trims it. `Bash`, `Read` and `Grep`
are the leak channel — they put a credential in a transcript. `Write` and `Edit`
cannot leak anything; they are matched because the guard's own installed copy is
a file, and the ownership rule above is what makes those two matches belt rather
than braces.

### A merged rule change reaches sessions only when root reinstalls

This is the consequence to hold on to: **advancing the pin changes nothing about
what blocks a session.** `fleet-pin-advance` moves `~/.local/share/pins/eunomia`;
the guard does not run from there. A change to `config/secret-patterns.conf`
merged to `main` is live only after the libexec copy is replaced, and that needs
root. There is no unit that does it. Measured 2026-09-21: the installed
`exe_sha256` / `conf_sha256` match the blobs at `pin_commit=3c1c7f6` **and** at
`main` @ `7d91402` — the guard has not changed since it was installed, so the
gap has cost nothing yet. It will the first time a pattern lands.

Verify the live copy against a commit — no root needed:
```
cat /usr/local/libexec/fleet/INSTALLED
git -C ~/dev/eunomia cat-file -p <commit>:bin/fleet-secret-guard | shasum -a 256
git -C ~/dev/eunomia cat-file -p <commit>:config/secret-patterns.conf | shasum -a 256
shasum -a 256 /usr/local/libexec/fleet/bin/fleet-secret-guard \
              /usr/local/libexec/fleet/config/secret-patterns.conf
```
The four digests pair up when the live guard is that commit.

Reinstall after a merged change — from a terminal, as the owner, with root:
```
c=$(git -C ~/dev/eunomia rev-parse origin/main)
d=$(mktemp -d)
git -C ~/dev/eunomia cat-file -p "$c:bin/fleet-secret-guard"       > "$d/fleet-secret-guard"
git -C ~/dev/eunomia cat-file -p "$c:config/secret-patterns.conf"  > "$d/secret-patterns.conf"
sudo install -o root -g wheel -m 555 "$d/fleet-secret-guard"      /usr/local/libexec/fleet/bin/fleet-secret-guard
sudo install -o root -g wheel -m 444 "$d/secret-patterns.conf"    /usr/local/libexec/fleet/config/secret-patterns.conf
printf 'installed_at=%s\npin_commit=%s\nexe_sha256=%s\nconf_sha256=%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$c" \
  "$(shasum -a 256 < "$d/fleet-secret-guard" | cut -c1-64)" \
  "$(shasum -a 256 < "$d/secret-patterns.conf" | cut -c1-64)" | sudo tee /usr/local/libexec/fleet/INSTALLED >/dev/null
```
The blobs come from a commit, never from a working tree, for the same reason the
hook never pointed at one. This is deliberately manual: a unit with root that
rewrites the guard on every merge is the capability #222 exists to deny.

**The pin still defends itself.** `guard-pin-exe`, `guard-pin-conf` and
`pin-worktree-mutation` refuse writes to `~/.local/share/pins/eunomia` from
inside a session, so the pin the other units execute from cannot be steered by a
session either. Those rules are unchanged; they are just no longer what protects
the guard. Advancing the pin is therefore not a session's job: run it from a
terminal, or let `fleet-pin-advance` do it (that unit runs from launchd, never
through a PreToolUse hook, and is the sanctioned writer;
`launchd/org.eunomia.fleet-pin-watch.plist` points here for this pair):

```
git -C ~/.local/share/pins/eunomia fetch origin
git -C ~/.local/share/pins/eunomia checkout --detach origin/main
```

Re-verify after any reinstall — the refusal must name `/usr/local/libexec/fleet`:
Then, in a session, run `cat ~/agent/.forgejo-token-revbot` and confirm it is
**refused** — a guard is not trusted until seen refusing. Check that the refusal
names the **pinned** script path: the message quotes the command it ran from,
which is how you tell which copy is live.

Watcher — set `FLEET_NTFY_URL` in the plist, bootstrap it, and confirm the baseline
sweep pages (it alerts once per pre-existing finding; triage, then quiet). Prove it
live: write `eyJdummy.dummydummy` into a scratch transcript and confirm an alert.
