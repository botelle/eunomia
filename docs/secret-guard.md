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
`tool_result` content for high-signal credential shapes (`ghp_…`, `sk-…`, `eyJ….…`
JWT/tunnel tokens, `BEGIN … PRIVATE KEY`, AWS keys) and ntfys on first sight — rule,
file, line, **never the matched text**. Detection-in-minutes plus rotation is the
honest backstop, not a promise of zero leaks.

## What is and isn't covered

- **Covered:** reading a secret file or dumping a credential via command; a leaked
  token landing in tool output.
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
