# ADR-0007 — colima serves the CI lane; services run under Apple `container`

- **Status:** Accepted
- **Date:** 2026-09-07
- **Deciders:** the operator (owner/merger)
- **Supersedes:** the first draft of this ADR (PR #175), which proposed colima for
  *both* CI and services. That decision was rejected by the owner. The argument it
  rested on, and why it was wrong, is kept below under Alternatives — a rejected
  decision is worth more written down than deleted.
- **Relates:** `plans/0024-container-evaluation.md`, which asked for this decision
  with numbers beside it rather than a feature comparison. Track A (the
  `cihost-linux` CI label) is merged as **#131**; its measurements are in
  [docs/fleet-history.md](../fleet-history.md) via **#165**.

## Context

Two runtimes are on cihost. colima has run for months, is `brew services`-persistent,
and since #131 is what the Forgejo runner's `cihost-linux` label executes against.
Apple `container` 1.3.1 was installed 2026-09-06 (`INSTALL_RECEIPT.json`; the
2026-08-28 on the Cellar files is the bottle's build date, not the install) with
its apiserver stopped, holding 1.4 GiB of kernel and image assets.

### What was measured

One image, `nginx:alpine`, pinned to the same digest (`sha256:72ba65eb…`) in both
runtimes, same host, same loopback port shape, five trials per cell, alternating
arms. Harness committed at [`tools/ctr-bench.py`](../../tools/ctr-bench.py).

**Time to first HTTP 200:**

| | cold (create → 200) | warm (stop → start → 200) |
|---|---|---|
| colima / Docker | **0.11 s** (min 0.11, max 0.20) | **0.11 s** (max 0.12) |
| Apple `container` | 0.91 s (min 0.91, max 1.01) | 0.74 s (max 0.89) |

5/5 trials succeeded in every cell. Apple is ~8× slower cold, ~7× warm: colima's
VM is already booted; Apple boots a microVM per container.

**Resident memory**, attributed by diffing `com.apple.Virtualization.VirtualMachine`
XPC processes across a container's lifetime:

| | at rest | with one container |
|---|---|---|
| colima | **7,108 MiB** (one persistent VM) + ~212 MiB supervisors | 7,108 MiB — *unchanged* |
| Apple `container` | **0 MiB** — no VM exists | +403 MiB, released entirely on stop |

colima holds its VM whether or not anything runs (configured 8 GiB / 4 CPU).
Apple holds nothing at rest and charges ~400 MiB per running container.

**A measurement trap worth recording.** The first attempt matched processes by
runtime name (`colima`, `limactl`, `qemu`) and reported 71 MiB against 212 MiB —
plausible, and wrong by two orders of magnitude. Neither runtime's VM carries its
runtime's name; both are `com.apple.Virtualization.VirtualMachine` XPC services.
The surviving numbers come from diffing the VM process set around a container's
start and stop. Matching on a name that seems right is a measurement that cannot
fail visibly.

### Why CI cannot leave colima

This is the constraint, and it is specific: **the Forgejo runner is a Docker
client.** v1.0.7 vendors moby's official Go client, and a `docker://` label means
"connect to a Docker Engine API endpoint".

- **Kubernetes is not an option.** Apple ships a `k8s` plugin ("Local Kubernetes
  development cluster management"), but the runner binary contains **zero**
  kubernetes references — there is no executor to attach it to.
- **Podman is not an option.** It is Docker-API-compatible, but on macOS it runs
  its own Linux VM, so it recovers none of the resting memory that would be the
  point.
- **Apple's API is XPC, not a socket.** Its apiserver listens on **no TCP port and
  no unix socket**; it speaks XPC (`com.apple.container.apiserver`, `.runtime`,
  `.network`, `.registry`), and the binary contains no Docker-API-shaped strings.
  Nothing can point `DOCKER_HOST` at it.

Bridging would mean writing a Docker Engine API daemon that translates to XPC —
container create/start/wait/remove, exec per workflow step, archive put/get for
the workspace, image pull, networks, volumes — and placing it in the path that
gates every merge. Rejected on risk, not on effort.

### Why services are not subject to that constraint

**A service never touches the Docker API.** A launchd plist calling
`container run -d -p 127.0.0.1:<port>:<port> <image>` is sufficient, and that is
the exact shape the measurements above exercised. The CI constraint does not
generalise to services, and the first draft of this ADR blurred the two.

## Decision

**colima serves the Forgejo CI lane, and nothing else. Long-lived services run
under Apple `container`.**

1. **colima is scoped to CI.** It exists for the `cihost-linux` runner label.
   Nothing outside the CI lane may acquire a *new* dependency on it, and the one
   service already in it is to be moved out.
   > **Corrected 2026-09-08.** This item originally read "No service is placed
   > in it." That was **false when it was written** — brianpoints has run in
   > colima the whole time. See the Amendment below; it is a migration that has
   > not happened, not a property to ratify.
2. **Services run under Apple `container`**, one container per service, bound to
   loopback, fronted by caddy where a LAN caller needs them.
   > **SUSPENDED 2026-09-09.** Apple `container` 1.3.1 stopped forwarding
   > published ports after cihost's reboot ([#230](../../issues/230)), which is
   > precisely the mechanism this item depends on. No service moves onto it
   > until that is understood. See the second Amendment; fate-sharing is now
   > addressed by a second colima **profile** instead.
3. **Apple `container` is adopted**, not merely retained. Its apiserver stays
   registered (`sh.brew.container`) and the 1.4 GiB of runtime assets stay.
   > **Downgraded 2026-09-09** to *retained, not adopted*, for the same reason.
   > Nothing in the fleet may depend on it while #230 stands.
4. **Reboot survival becomes load-bearing and must be made real before any
   service moves.** See the blocking follow-up; today it is unverified.

### The reason services do not go in the CI VM

The rejected draft argued that colima's 7 GiB is a sunk cost, so a service placed
in the existing VM costs no additional resting memory while the same service under
Apple costs +403 MiB. The arithmetic is right and the conclusion is wrong, for two
reasons:

- **It optimises a resource that is not scarce.** cihost has 128 GiB of RAM at 96%
  free. Trading isolation for 400 MiB on that host is not a trade worth making.
- **It ignores fate-sharing, which is what actually costs.** A service inside the
  CI VM shares its lifecycle: colima restarts for CI reasons, upgrades on CI's
  schedule, and its 4 vCPUs are contended by whatever the merge train is running.
  A CI-driven restart would take production services down with it. Separating the
  two runtimes buys a blast-radius boundary that no memory figure offsets.

Apple's per-container microVM and zero resting footprint are the right shape for a
long-lived service precisely because each service is isolated from every other and
from CI.

### Two properties of the CI lane this ADR ratifies

**Container jobs run as uid 0; host jobs run as `operator`.** Two tests asserting a
permission-denied path are skipped on `cihost-linux` with a printed reason, because
`chmod` cannot deny root (`tests/test_fleet_cr.py:592`,
`tests/test_fleet_watch.py:777`); a third skips for `plutil` on a non-macOS runner
(`tests/test_fleet_watch.py:2198`). Both lanes collect 522 tests on `abc253e`: the
container passes 518 and skips 4, the host passes 521 and skips 1. **That asymmetry
is an argument for keeping the `cihost` label alive beside `cihost-linux`**, not for
migrating everything — a permission-denial test is only testable where the process
is not root.

**The docker-socket gate is closed by absence, and this is the rule as built.**
`DOCKER_HOST` is set in the runner's LaunchDaemon and inherited by both lanes;
inside the container the path it names does not exist, so the route reads closed.
The gate treats a `unix://` URL with an absent in-container path as closed and stays
fail-closed for `tcp://`, `ssh://`, and any socket file that *is* present. This is
looser than plan 0024 §2's "unset" wording. The plan and the gate disagree; the gate
is what runs, so the gate is what is documented here.

## Amendment, 2026-09-08 — brianpoints is in colima, and it cost an outage

**Decision 1 asserted something nobody had checked.** `points.example` —
`brianpoints-api-1`, `brianpoints-core-1`, `brianpoints-db-1`, a compose stack
with a postgres volume — has been running under colima on cihost since before
this ADR was written. The author accepted "colima is the CI runtime" as given
and never ran `docker ps`.

The cost was not hypothetical. On 2026-09-08 the installer for follow-up 1
checked that the **CI queue** was idle, treated that as sufficient grounds to
bounce colima, and stopped points until a rollback restarted it. A Kuma alert
was the first anyone knew. `infra` #90 rewrote that installer to enumerate what
colima is hosting, name it, and refuse without an explicit acknowledgement.

### What this changes, and what it does not

**It does not change the decision.** colima still serves the CI lane and
services still belong under Apple `container`. What changes is that Decision 2
describes work outstanding rather than a state already reached, and the reason
for it is now the one the evidence actually supports.

**The rationale in the original text was the weaker half.** The measurements say
Apple holds nothing at rest while colima holds a persistent ~7 GiB VM — but
colima runs permanently for CI regardless, so **moving brianpoints out reclaims
no memory at all.** It only changes what is inside a VM that stays either way.

The real argument is **fate-sharing**, and 2026-09-08 is the demonstration: a
service inside the CI VM restarts when CI restarts, upgrades on CI's schedule,
and contends for its four vCPUs with the merge train. Anything that touches
colima for CI reasons is an outage for everything else in it. That is the
boundary worth buying, and no memory figure offsets it.

### The migration is not a runtime swap

Recorded so the next session does not underestimate it:

- **No compose.** Apple `container` has none (see Alternatives). Three services
  become explicit units plus wiring.
- **Service-name DNS.** The three find each other on the `brianpoints_default`
  network. Apple's networking is a flat vmnet; `container network` and
  `container system dns` exist and are untested for this.
- **A bind-mounted build.** `brianpoints-core-1` runs a maven image with
  `~/bp-run` mounted at `/src` and an m2 cache volume — a dev-shaped deployment
  in production, and bind mounts are what Apple's runtime restricts.
- **A postgres volume**, which is the data.

**Prerequisite, now met.** When this amendment was written the database had **no
backup of any kind**; a verified dump was taken by hand and `operator/loyalty` #64
adds the nightly encrypted chain with a tested restore. A migration that moves
production data before a restore has been exercised is the one step that cannot
be undone, so it waits on that chain actually running.

### Status of follow-up 1

Done, and it is what surfaced all of the above. colima is a system LaunchDaemon
(`infra` #86, #88, #90), verified serving from the system domain with the three
brianpoints containers confirmed back up after the switch. **Boot survival
remains unproven until cihost reboots** — the daemon makes the boot path correct
and retryable, nothing here demonstrates it.

## Amendment, 2026-09-09 — Apple `container` is held until it matures

cihost rebooted, which is the test follow-up 1 existed to enable. It split
cleanly.

**colima passed.** The system LaunchDaemon loaded at boot with no console login
(pid 334, never exited), docker answered within ~53 s, all three brianpoints
containers returned with the ledger intact, and the CI runner self-healed after
seven `KeepAlive` attempts against dependencies that were not yet up. The
problem this ADR's follow-up work set out to fix is fixed and demonstrated.

**Apple `container` failed, and failed at the exact mechanism Decision 2 needs.**
A published port binds and never forwards. Isolated in
[#230](../../issues/230): the container serves on its vmnet address (HTTP 200),
`ping` is clean, the host port shows `LISTEN` — and nothing proxies between
them. Not load (re-run at load 9.65), not a startup race (an apiserver restart
does not repair it), and it worked on four ports on this host earlier the same
day, including across the LAN from opshost.

So Decision 2 is **suspended** and Decision 3 downgraded from *adopted* to
*retained*. This is not a reversal of the runtime comparison; it is a statement
that the runtime is not ready for the role this ADR gave it.

### The memory argument never favoured Apple, and this ADR implied it did

Worth correcting because it is the number a reader would reason from.

Measured on brianpoints in colima: `core` (a JVM under maven) 498.9 MiB,
`api` 72.5 MiB, `db` 42.5 MiB — **~614 MiB** for the stack. That figure was then
compared against the **403 MiB** this ADR reports for one nginx container under
Apple `container`, and the two are not comparable:

- **614 MiB** is `docker stats` — processes **inside** colima's guest, excluding
  the guest kernel, the docker daemon and VM overhead.
- **403 MiB** is **host-side RSS of an entire microVM** — kernel, overhead and
  the container together.

Apple charges a whole microVM per container. For a three-service stack whose
JVM already uses 499 MiB inside the guest — against Apple's 1024 MB per-container
default — the host-side total would not be smaller than colima's, and no
measurement of a JVM under Apple `container` was ever taken. **The remaining
argument for moving services off the CI runtime is fate-sharing alone**, which
the first amendment already established.

### What replaces it: a second colima profile

colima supports multiple instances (`-p, --profile`, verified on cihost). A
services profile is a separate VM with a separate lifecycle, which is the whole
of what fate-sharing requires, and it keeps what the migration would otherwise
have to rebuild: compose, service-name DNS, bind mounts, and working port
publishing.

Sizing, from the measurements above: 3–4 GiB, with an explicit heap cap on
`core` — none of the three containers has a memory limit today, so the JVM
expands into whatever the VM offers.

This is deliberately the boring option. Building a port-forwarder to compensate
for #230, or reimplementing caddy — which on this host is doing TLS and bcrypt
basic auth, not merely forwarding — would be new machinery owned by us to work
around a defect in a runtime we have not adopted.

### Revisit when

Apple `container` earns another look when #230 has an explanation and a version
that fixes it, or when something the profile approach cannot do is actually
needed. The measurements in this ADR stay valid; the harness at
`tools/ctr-bench.py` reproduces them.

## Alternatives rejected

**Services in colima alongside CI** — the first draft's decision, rejected by the
owner. Reasons above: it optimises non-scarce memory and couples service uptime to
the CI runtime.

**Move CI to Apple `container` and retire colima.** Would make the resting footprint
real. Cannot be done: no Docker API, no kubernetes executor, and a hand-written
bridge in the merge path is unacceptable risk. Not a close call.

**Decide on the feature list.** Plan 0024 §3 forbids it and was right to. Apple has
no compose and no Docker socket without thinly adopted shims; colima has both. That
argument would have reached a different answer for a reason that would not survive a
version bump.

**Uninstall Apple `container`.** Moot under this decision — it is adopted. Recorded
because plan 0024 §3 names `container system stop` and support-directory removal as
the operator's act if the ADR picks colima; this ADR picks colima *for CI only*, so
that boundary does not fire. Noted rather than passed over, per the rule that a plan
may be superseded but not silently.

## Consequences

- **The apiserver stays registered.** `sh.brew.container` remains; the earlier
  draft's proposal to unregister it is withdrawn.
- **Reboot survival is now a blocking prerequisite, and it is unverified.**
  `sh.brew.container` is a **user LaunchAgent**: it loads at login, not at boot.
  The CI runner already had to move to a system LaunchDaemon for this exact reason
  (`/Library/LaunchDaemons/org.eunomia.forgejo-runner.plist`). If cihost reboots
  unattended, services under Apple `container` may not come back. **No service may
  move until this is settled** — see follow-up 1.
- **`brew services list` misreports this service** as `stopped` while the apiserver
  runs, because the plist one-shots `container system start` and exits 0. Anything
  that monitors it must read `container system status`. Recorded so the false-green
  is not rediscovered.
- **Containerising speakhush remains blocked, and not on the runtime.**
  `api/run.sh` execs `tools/vault_exec.py`, which AppRole-logs into keyvault with
  `role_id`/`secret_id` files and a CA cert. Placing those in an image or a mount is
  the shared-bearer shape the workload-identity work exists to remove. That needs its
  own ADR against the SPIFFE/Ghostunnel plane; this one does not decide it.
- **cihost's data volume is at 99% (873 GiB used of 926, 13 GiB free).** Not caused
  by this decision, and it bounds it: colima's disk image is provisioned at 60 GiB
  and Apple's assets are 1.4 GiB. Adding per-service images to a volume with 13 GiB
  of headroom needs its own attention before it becomes an outage.
- **Any future pull comparison must pin `--platform`.** Unpinned, Apple unpacked
  linux/386, ppc64le, riscv64 and s390x alongside arm64 — 12.3 s against 0 s pinned.
- **Plan 0024 stays `ready`, not `done`.** Track B's purpose was to produce the
  evidence for this decision by moving a service into each runtime. The runtime
  question is answered without it, but Track B's remaining half — actually moving a
  service — is now the live work rather than cancelled, and it is blocked on the two
  items above.

### Follow-ups, each its own PR

0. **Move brianpoints onto a second colima profile** (not Apple `container` —
   see the 2026-09-09 amendment), sized 3-4 GiB with a heap cap on `core`.
   Blocked on `operator/loyalty` #64's backup chain actually running: a
   migration that moves production data before a restore has been exercised is
   the step that cannot be undone.
1. **Blocking:** decide and implement the apiserver's launchd tier — user LaunchAgent
   as today, or a system LaunchDaemon as the CI runner needed — and verify it with a
   real reboot. Nothing moves under Apple `container` until this holds.
2. Reconcile plan 0024 §2's `DOCKER_HOST`-unset wording with the gate as built.
3. Migrate the remaining Python-only workflows to `runs-on: cihost-linux` — talos,
   requests-intake, and pr-proxy's syntax job.
4. `bin/fleet-candidate` depends on ambient git identity; the CI job papers over it
   with job-level `GIT_AUTHOR_*`. The hermeticity fix belongs in `bin/`.
5. cihost disk headroom (13 GiB free) — its own investigation, not this ADR's.
