# Local runners through epeius (plan 0154)

Every local runner in `bin/orchestrator`'s `RUNNERS` (`local-*`, listed in
`docs/per-repo-models.md`) is spawned as `epeius run` against the lease worktree,
with the task on stdin. epeius leaves edits uncommitted and opens nothing, so the
orchestrator publishes.

## Which host a runner uses (plan 0160)

Each runner names a `host` in `OLLAMA_HOSTS` (default `cihost`). Both are reached
through an ssh forward to the host's loopback ollama; each host has its own lock
(`locks/<host>-ollama.lock`), so a gpuhost run and a cihost run proceed together.

| Runner | Host | Why |
|---|---|---|
| `local-gemma3-12b` | gpuhost | The owner asked for it, and it is faster on gpuhost's RTX 5070 Ti. Same digest (`f4031aab637d`) as cihost's. |
| every other `local-*` | cihost | gpuhost's 16 GB does not fit them; moving one is a decision with a measurement behind it. |

On gpuhost the orchestrator is a guest. It never unloads, pulls or copies a model
(gemma3:12b stays resident for trainrig's teacher). Before spawning it runs
`nvidia-smi --query-compute-apps=process_name,used_memory --format=csv,noheader`
over ssh and stops if any process other than ollama's (`llama-server`, `ollama`)
is on the GPU, i.e. a training run. An ssh or `nvidia-smi` failure is also a stop.

## What a run does

(Steps below read "cihost" for the default host; on gpuhost step 6 skips the unload.)

1. Takes the cihost lock (`locks/cihost-ollama.lock` under the fleet directory):
   one epeius run against cihost at a time, fleet-wide. A second run waits up to
   the implementer timeout, then stops with that reason.
2. Opens `ssh -N -o ExitOnForwardFailure=yes -o BatchMode=yes -L
   127.0.0.1:<port>:127.0.0.1:11434 cihost`, in its own process group, killed in
   a `finally`. The endpoint is `http://127.0.0.1:<port>`.
3. Reads `/api/tags` through the forward and stops unless the runner's model is
   present with a digest starting with the runner's `digest`.
4. Stops if the prompt (characters / 3, plus the target files) plus
   `reply_reserve` (16384 tokens) exceeds the runner's `num_ctx`.
5. Spawns epeius with `PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ` and a fresh
   per-run temp directory as both `HOME` and `TMPDIR`. No agent socket, no real
   home. Each lease path that exists is a `--target-file`, globs expanded, and the
   plan's own file always.
6. Unloads the model (`keep_alive: 0`) whatever happened, then releases the lock.
7. On exit 0 (edits produced): sets the plan's `status: done` if the model did not,
   commits, pushes, and for the first run opens the PR through
   `~/bin/forgejo-open-pr.sh` with the `Plan: <id>` marker. A fix round or an
   instruction commits and pushes and opens nothing. Any other exit class pushes
   nothing. A follow-up branch has no epeius publish path and stops.

## Why the forward, not the bridge

cihost's ollama listens on `127.0.0.1:11434` only. The LAN bridge
`192.0.2.4:11435` is Caddy with basic auth, and an open-weights run may carry no
credential (ADR-0011 §3). The runners declare no endpoint variable at all; the
endpoint is filled at spawn. While a run is live, cihost's unauthenticated ollama
is reachable on opshost's loopback by any local account; the run is short and
serialised. A unix-socket forward would close that, but epeius speaks only TCP.

## Why the orchestrator publishes

epeius by design never commits, pushes or opens a PR (epeius plan 0001 §3), and
the orchestrator verifies a marked PR afterwards whatever the runner.

## Digests

Registry tags move, so the digest cihost reports is the version. A mismatch is a
`Stop`; re-pinning is a reviewed edit to `RUNNERS`.

## Owner steps this plan was held on

`ollama cp qwen3-coder-next:latest qwen3-coder-next:80b-q4_k_m`,
`ollama cp qwen3-coder:latest qwen3-coder:30b-a3b-q4_k_m`, the Muse Glimmer pull,
and `ollama cp` of the Ornith GGUF to `ornith-1.0:35b-q8_0`. All were present on
cihost on 2026-10-26.

## Manual run

**Not yet recorded.** The plan's definition of done asks for one real run of
`local-gemma4-31b` on a throwaway plan in a bakeoff repository, from opshost, ending
in a marked PR. It needs the owner's choice of bakeoff repository and loads a 31B
model on the machine that serves speakhush, and an implementer session may not
open that run for itself. Record here: the forward port, the digest check line from
the transcript, and the PR link.

## Follow-ups

The model policy (ROADMAP row 22), the hosted runners, and reviewer seats for
these runners after talos's review spawn path.

## Hosted runners (plan 0156)

A runner with a `vendor_key` is spawned through the `openai:` backend against a
vendor endpoint, with no cihost lock or ssh forward.

1. The prompt estimate (characters / 3, plus target files) over `max_input_tokens`
   is a `Stop` before the key helper runs. `max_input_tokens` is 200000 for every
   hosted runner except `hosted-glm-5.3`, which carries 400000 (plan 0210,
   2026-11-06): GLM-5.3's context measured at 1,048,576 tokens on OpenRouter,
   wide enough to admit eunomia#1334 round 2's prompt (611,183 characters, an
   estimate of 203,728 tokens), which the previous 200,000 cap refused.
2. `os.pipe()`; the fetch helper (`~/bin/fetch-<vendor>-key.sh`) is spawned with
   stdout the pipe's write end and stdin `/dev/null`; its stderr is read only for
   its exit and discarded. Non-zero exit, or 30 s, is a `Stop` naming the helper
   and its status.
3. `FIONREAD` on the read end must be non-zero. The orchestrator never reads the
   pipe, so the key is never in its memory.
4. epeius is spawned with `pass_fds=(r,)` and `--key-fd r`; the parent closes `r`
   right after the spawn. Its environment is the bare set above, without
   `SSH_AUTH_SOCK`.

Two runners use this path today, both through OpenRouter's default routing
(`routed-hosted`): `hosted-muse-spark-open` and `hosted-glm-5.3` (plan 0172, model
`z-ai/glm-5.3-20260816`, the same `~/bin/fetch-openrouter-key.sh` helper and
balance). The evidence for GLM-5.3 is the reviewer replay in
`operator/bakeoff-ongoing` `records/20261029-reviewer-replay-open-models/`: it
caught the same two of four seeded bugs as Fable, with no false HIGH, where Muse
Spark Open caught none. Neither runner is selected anywhere until the owner
chooses it in the Repos grid.

`hosted-glm-5.3` carries `max_output_tokens = 128000`; every other hosted runner
keeps the default 16,000 (plan 0205). Measured on eunomia#1334 (2026-11-04,
about 145,000 prompt tokens): at 16,000, GLM-5.3 spent the whole cap reasoning
and returned no review text; at 64,000 it used 59,046 output tokens on a good
review, close enough to the cap that a larger PR would be cut off. 128,000, not
GLM-5.3's own 131,072 maximum, because that was the lowest maximum among the
OpenRouter providers it routes through on that date. Checked against epeius
itself (pins/epeius at `47e2036`): `cli.py` only rejects a non-positive
`--max-output-tokens`, with no ceiling of its own, and passes it to
`backends.chat_openai` as `max_tokens`, sent verbatim in the request payload —
so 128,000 reaches OpenRouter unchanged rather than being clamped by epeius.

epeius sends one request and runs nothing the model asks for, so a vendor sees the
plan and its lease files, and only for a `zone: public` plan (ADR-0006 §7).

The spend cap is the vendors' prepaid balances ($50 in total, auto-recharge off),
owner steps. The orchestrator adds no budget logic.

**Known gap:** the pinned epeius (18d3b38) does not put the served provider in its
`--json` result, so the "served provider on every run" record for OpenRouter needs
an epeius change first.

### Manual billed run

**Not recorded.** It needs `hosted-deepseek-v4-pro`, which waits on the vendor-id
measurement (`docs/per-repo-models.md`), and its cost stated first.
