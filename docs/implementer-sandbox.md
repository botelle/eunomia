# The implementer sandbox (plan 0073)

The writer half of ADR-0005 §5. `bin/orchestrator` spawns every Claude
implementer runner with `--tools` (the seven names, what the session *holds*),
`--strict-mcp-config` (no MCP servers), and `--settings <inline JSON>` built by
`sandbox_settings()` from `config/implementer-sandbox.json` (a `base` plus one
overlay per runner). `--allowedTools` stays: under `-p` Bash degrades without it.
`--restricted` is not passed — it ignores the operator's settings files, which is
where `fleet-secret-guard` is installed.

## What it governs (once enabled; it is currently disabled, see below)

The session's **shell**: Bash subprocesses may reach only `forge.example`
(forge over ssh 3022 and http 3001), `127.0.0.1` (the vault, which
`~/bin/fetch-forgejo-token.sh` logs into on :8200), and the runner's own model
endpoint (`api.anthropic.com` for every wired runner). Each runner also has
its credential variable stripped from each sandboxed Bash command
(`credentials.envVars`, `mode: deny`): `CLAUDE_CODE_OAUTH_TOKEN` for the five
`sub-*` runners (`run_implementer` puts it in the child's env), and
`ANTHROPIC_API_KEY` for `api-sonnet`. The variable is not removed from the CLI
process itself.

## What it does not govern — two open levers

1. **The CLI's own API traffic.** The model request leaves the CLI process, not
   a Bash subprocess. Nothing here bounds it; do not read this document as
   saying the session's traffic is bounded.
2. **`SSH_AUTH_SOCK`.** It stays in `_ENV_ALLOW` because the push needs it. An
   agent socket is a signing oracle for every host the operator's key reaches.
   What bounds it is the network allowlist, not anything about the socket —
   and see "Not measured" for whether that allowlist reaches ssh at all. The
   durable fix is the per-dispatch deploy key named in `bin/orchestrator`.

## Measured, 2026-10-08, CLI 2.1.295 on opshost

- Tool count (live arm of `tests/test_implementer_sandbox.py`,
  `EUNOMIA_SANDBOX_LIVE=1`): the `init` message reports **6** tools, all within
  the named seven (this build ships no `TodoWrite`), against 64–66 before
  (`docs/measurements/adr-0005-pinned-repo-reads.md`).
- The settings document is passed inline and the CLI accepts it.

## Measured, 2026-10-11, CLI 2.1.296 on opshost: the sandbox ships DISABLED

One live `claude -p` spawn with `sandbox.enabled: true` and this allowlist ran
three probes in the sandboxed shell; all three were refused:

- `curl https://127.0.0.1:8200` — exit 7. **Loopback is not exempt** (D2 Q2),
  and listing it did not help: the token helper would fail.
- `ssh -p 3022 forge.example` — name resolution failed and the `~/.ssh/cm`
  ControlMaster socket was `Operation not permitted`. **The allowlist does not
  carry the ssh push** (D2 Q1): it is HTTP-proxy shaped and the ssh path is
  refused outright. The push cannot be claimed as bounded by it.
- `touch <work_root>/<slug>/.git-store/.git/x` — `Operation not permitted`. The
  default write policy is the cwd; a worktree's commits write to the shared
  store outside it, so `git commit` would fail.

Shipping that on every runner would burn each dispatch at the last step, which
plan 0073's boundaries forbid. So `config/implementer-sandbox.json` carries
`sandbox.enabled: false`; `--tools`, `--strict-mcp-config` and the per-runner
document still ship. `allowUnsandboxedCommands: false` is set so that, once
enabled, the model cannot retry a refusal with `dangerouslyDisableSandbox`
(which `--allowedTools Bash` would pre-approve). To enable it, a follow-up must
add `sandbox.filesystem` write access for the store and `~/.ssh`, find an
egress path for ssh :3022 and the loopback vault, and run the DoD 4 fixture.

## Not measured — plan 0073 DoD 2–4 are NOT met by this change

This run had no LAN listener, no scratch repository, and no authority to mint
and then rotate a PAT, so the following were **not** done and are not claimed:

- the D2 allowlist is the one the plan derived by reading the code paths
  (forge, loopback vault, model endpoint); **no real dispatch has been run with
  the sandbox on**, so no refusal has been recorded and the list may be missing
  an entry. Run one before trusting it;
- D2 questions: (1) does the allowlist govern ssh on :3022; (2) does
  `https://127.0.0.1:8200` need an explicit entry or is loopback exempt (it is
  listed explicitly, which is safe either way); (3) does `--settings` leave the
  `fleet-secret-guard` hook in place — expected yes, **unproven**. If (3) is
  no, stop: the sandbox removed the guard;
- the injected-instruction fixture, the before case on `main`, the refusal
  after, and the push-still-works half. Until that exists this is not evidence
  of a bound. If the before case shows the token arriving, rotate the PAT.

## The test lane (plan 0066, D3)

Not sandboxed by this mechanism. `sub-antigravity` and `sub-codex` are not
Claude Code: `--tools`, `--strict-mcp-config` and `--settings` do not exist for
them. Their equivalents are the pinned literal argv (`--sandbox`, `--mode
plan`; `-s read-only`) and `check_settings_precondition`; neither restricts
network egress. (`sub-codex`'s argv is confined by a read-denying
`sandbox-exec` profile, plan 0101; it leaves outbound TCP open.) What bounds the test lane today is
ADR-0006's measurements. `local-qwen` is also unwired; when a wiring plan lands
it must add its `OLLAMA_HOST` host to its own overlay.
