# ADR-0015 — An environment is a service's manifest bound to a provider, and dev deploys every merge

- **Status:** Accepted
- **Date:** 2026-09-25
- **Deciders:** the operator (owner/merger)
- **Amends:** `SPEC.md` Revisions 2026-09-10 ("`fleet-pin-advance` never
  restarts a resident process"). That rule survives for the pins it
  governs. A pin owned by a deployment (§4) is governed here instead.
- **Relates:** [ADR-0014](0014-a-pull-request-changes-one-kind-of-thing.md)
  (the `deploy` kind and `deploy_reviewer`), `bin/fleet-pin-advance` (plan
  0034), `bin/fleet-svc` and `config/services.conf` (plan 0029), SPEC's
  `deploy-started`/`deploy-verified` events, `operator/infra` `lib/deploy.sh`,
  `operator/portunus` `docs/DESIGN.md` §5 (the deploy plane).
- **Spec-impact:** `SPEC.md` (a Revision recording §4–§6) and a new
  `docs/deploy.md`, each in a following `docs` pull request.

## Context

A merge changes nothing until a pin moves and a service restarts. For the
fleet's own tools, `fleet-pin-advance` moves the pin. For a service, nothing
does. On 2026-09-25 the lynceus API had run 13 days behind `main`, and every
screen added in those days answered 404 on the phone. Each service also
carries its own hand-written deploy notes, and nothing can deploy the same
service anywhere other than the host it was written for.

## Decision

**1. A service declares what runs, in a manifest in its own repository.** The
manifest is `deploy/manifest.json`: its name, the artifact, how it starts,
references to its secrets, a health check with a required body substring, and
what it needs from the host. It names **no host, unit label, address or
provider**. A manifest change is a `deploy` pull request (ADR-0014).

**2. An environment binds a manifest to a provider.** The binding (environment,
service, provider, and that provider's parameters, such as host and unit) lives
in reviewed eunomia configuration, not in the service's repository. The same
manifest may be bound in several environments.

**3. There is one provider now, `homefleet`, and its environment is `dev`.**
`homefleet` runs a service from a pin on a fleet host, supervised by launchd or
systemd, with secrets fetched from keyvault at start. Cloud providers for
`stage` and `prod` come later, through the same binding. The provider
interface is written once a second provider exists to test it against, and
not before.

**4. `dev` deploys every merge to the service's `main`.** For a bound service,
the deployer moves the pin, restarts the unit under a `service` lease, and
runs the health check. A pin owned by a deployment is moved **only** by the
deployer, never by `fleet-pin-advance`, and never by a session. It moves only
to a commit on a `main` the protection broker vouches for.

**5. A failed health check rolls back automatically, in `dev` only.** The
deployer returns the pin to the **last SHA it verified**, restarts, re-checks,
and pages. If the rollback also fails, it stops and pages. It does not retry.
This departs deliberately from `lib/deploy.sh`'s "nothing rolls back
automatically". A `dev` service is running, unattended, a merge a human
already approved, and the last verified SHA is the only target, so no
choice is left to make. A rollback in any other environment is an operator's
action.

**6. Every deploy is on the ledger.** `deploy-started` and `deploy-verified`
(SPEC's closed set) are emitted with actor `svc:fleet-deploy`, the service,
the environment and the SHA. A rollback is a `deploy-started` for the older
SHA.

**7. `stage` and `prod` are reached only by promotion**, never by a merge.
Promotion is its own gated action, as `operator/portunus` `docs/DESIGN.md` §5
describes, and is decided in a later ADR when a cloud provider exists.

## Consequences

- A new `bin/fleet-deploy` runs from launchd on the host that holds the pin,
  and each deployed unit is enrolled in `config/services.conf`.
- A manifest whose dependencies changed between two SHAs, such as the
  requirements file, is refused and paged rather than installed. How
  dependencies are prepared is a provider parameter, decided when a service
  needs it.
- Each service's hand-written deploy README reduces to its manifest, plus
  whatever the provider cannot do yet.
- Hosts other than the deployer's own are reached over the existing reuse-only
  ssh ControlMaster, like `fleet-svc`, and are added one at a time.
