# The credits window

A Fable review that the spent subscription limit would hold can instead run at once on
API credits, but only while the owner has opened a **window**, and only within two caps
(plan 0146, [ADR-0023](adr/0023-a-fable-review-may-bill-api-credits-inside-an-owner-window.md)).
`bin/fleet-credits` is the only writer of the window and its ledger.

## The order

1. Every review tries the subscription first. The **first attempt on a PR is never on
   credits**: only a review that already stopped (a `review_attempt` row, outcome `quota`
   or `budget`) can be retried on credits. A gate that is stale or wrong therefore cannot
   send work to credits while quota is left.
2. Credits only while the `model:fable` gate is **closed**. With the gate open, an open
   window costs nothing: `decide` answers `subscription`. A model with no usage bucket
   (rev2bot's `claude-opus-5-5`) always gets `subscription`.
3. Inside a window, within `min(per-PR remaining, month remaining)`, at least $1.00.

## The owner's commands

```
fleet-credits on --days N [--note TEXT]   N is 1-7; needs a terminal
fleet-credits off                          closes the window now; always exits 0
fleet-credits status [--json]
fleet-credits release ID                   needs a terminal; frees an expired reservation
```

- `on` refuses unless stdin is a terminal. That is a cooperative guard, not a security
  boundary: launchd, mopsus, the watcher and an agent harness run on pipes. There is no
  flag or variable to skip it. A second `on` moves the window's end, never past seven
  days after the window **first opened**; to go longer, `off` and then a new `on`.
  `on` also warns when the deployed `dispatch-review.py` does not accept `--bill credits`.
- `off` never asks and never fails ("no window was open" is success). It stops **new**
  credits spend. A review already running keeps going until it finishes or reaches its
  own `--max-budget-usd`; `off` prints how many reservations are still open and their
  total. The only way to stop in-flight spend at once is to **revoke the key in the
  Console**.
- There is no renew. Nothing opens a window but `on`; no config key, event or probe does,
  and nothing extends one at expiry. A window nobody remembers opening is how a shared
  budget disappears.

## How to stop credits spend

1. `fleet-credits off` (new spend stops).
2. `fleet-config set credits_per_pr_usd 0` or `credits_monthly_usd 0` (`decide` holds,
   for longer than a window).
3. Revoke the key in the Anthropic Console (in-flight spend stops too).

## The caps

`fleet-config` keys, built-in 50 and 100: `credits_per_pr_usd` (0-200) and
`credits_monthly_usd` (any non-negative number). The per-PR cap keeps a ceiling because
it becomes one review's `budget_usd`, so a typo of `5000` must refuse. The month has
none, because the prepaid Console balance with auto-reload off is what actually bounds
it. Both are per **UTC calendar month** (the Console's period), so a
PR still open next month starts from $0.

## `decide`

`fleet-credits decide --repo R --pr N --model M --json` prints one object:

```json
{"bill": "subscription" | "credits" | "hold",
 "budget_usd": <number or null>,
 "reason": "<string>",
 "window_ends": "<ISO-8601 UTC, e.g. 2026-10-18T09:00:00Z>" | null}
```

- `budget_usd` is a number in (0, 200] for `credits` and `null` otherwise. `window_ends`
  is set for `credits` and for a cap hold; `null` for `subscription` and for a hold with
  no window.
- `hold` reasons: `no window`, `window ended`, `per-PR cap`, `monthly cap`.
- Exit 0 for any answer it could make; exit 1 when it cannot read the database or the
  gate. A caller treats 1 as `hold`.
- `decide` **reserves** the budget in the transaction that read the remainders, so two
  concurrent callers can never be granted the same money. `decide --peek ...` computes the
  same answer and reserves and emits nothing; mopsus uses only `--peek`. The dispatcher's
  own `decide` for its slot, immediately before it spawns, is the only call that reserves.
  One run holds one reservation per credits slot.
- It uses `$FLEET_DB` (else `<fleet dir>/fleet.db`).

## `record`

```
fleet-credits record [--dry-run] --repo R --pr N --model M --usd X --session S
```

Appends the reported spend and settles the **newest** open reservation for that repo, PR
and model (an older one belongs to a crashed run). The spend takes the reservation's
window, even if the window has since closed; with no matching reservation it is still
written, with `window_id` NULL, which means "no window was open". Prints the row as JSON.
`--dry-run` validates and prints the row and writes, emits and pages nothing. The caps
are enforced by the run's `--max-budget-usd`; `record` stores the reviewer's figure and
never an estimate.

A reservation not settled within 4 hours is **booked at its full amount** (a
`credit_spend` row with session `expired:<reservation id>`), evented as
`credits-reservation-expired` and paged. It stays counted. Nothing automatic frees it:
after checking the Console, the owner runs `release ID`.

## Tables, keys, events

Tables (all with `created_at`/`updated_at`): `credit_window`, `credit_reservation`,
`credit_spend`, `credit_notice`, `credit_release` (and, for intakebot jobs, `credit_grant`,
`credit_job_reservation`, `credit_job_spend`). Events and their `detail` keys:

| event | detail |
|---|---|
| `credits-window-opened` | `window_id`, `ends_at`, `days` (again on an extension) |
| `credits-window-closed` | `window_id`, `by` (`owner`\|`expiry`) |
| `credits-spent` | `window_id`, `repo`, `pr`, `model`, `usd`, `pr_total`, `month_total` |
| `credits-cap-reached` | `window_id`, `cap` (`pr`\|`month`), `repo`, `pr`, `total`, `limit` |
| `credits-reservation-expired` | `window_id`, `reservation_id`, `repo`, `pr`, `model`, `usd` |
| `credits-refused` | `repo`, `pr`, `reason` (once per repo, PR and reason per window) |

Pages: a window opening (or extending), a window closing, each cap reached, each expired
reservation. An ended window is noticed by the first `decide`/`status`/`on`/`record`
after `ends_at`; mopsus runs `status` every cycle while a window is open.

## mopsus

In `review_retry_sweep`, for a held session review whose stop was a usage limit or a
`budget` stop (never a no-verdict, and not a plan-kind review, whose retry is a resume
request that carries no billing), mopsus runs `decide --peek`:

- `credits`: if the deployed `dispatch-review.py --bill __probe__ --help` exits 2 with
  `invalid choice` on stderr (a parser without `--bill` prints help and exits 0), start the
  retry under the existing lock now, with no probe, as
  `dispatch-review.py <repo> <pr> --post --bill credits`. It counts against
  `review_retry_limit` like any retry, and `review-requeued` records `billing: credits`.
  If the dispatcher does not accept the flag, mopsus treats the row as `hold` and emits
  `credits-refused` with reason `dispatcher`.
- `subscription` or `hold`: what happens today (probe, then retry on the subscription).

A `budget` stop (a credits run stopped by `--max-budget-usd`, talos plan 0029) closes no
gate. Retry counts and probe cadence for non-credits retries are unchanged (ADR-0016).

The key itself is never read here: the reviewer's dispatcher in operator/talos fetches it.

## Intakebot jobs

A intakebot job (plan 0166, [ADR-0023](adr/0023-a-fable-review-may-bill-api-credits-inside-an-owner-window.md)
§6) is a second subject beside a review. The intakebot worker calls:

```
fleet-credits decide-job [--peek] --job J --principal P --meter credits|subscription --model M --json
fleet-credits record-job [--dry-run] --job J --principal P --model M --usd X --session S
```

`decide-job` prints the `decide` object with one more key:

```json
{"bill": "credits" | "subscription" | "hold",
 "budget_usd": <number or null>,
 "reason": "<string>",
 "window_ends": "<ISO-8601 UTC>" | null,
 "grant": true | false}
```

- `--meter credits` (a principal other than the owner; intakebot passes the meter, eunomia
  does not know who the owner is): `credits` when P holds a live grant and
  `min(per-job cap, grant remaining, intakebot month remaining, overall month remaining)` is at
  least $1.00; that minimum is `budget_usd`, `grant` is `true`, `window_ends` is `null`.
  Otherwise `hold` with `grant` false and `reason` one of `no grant`, `grant cap`,
  `intakebot monthly cap`, `monthly cap`, `per-job cap`. The answer is **never** `subscription`,
  and the gate and window are **not consulted**: a grant is standing (ADR-0023 §6), and a
  stale or open gate must not route a partner's job onto the owner's subscription.
- `--meter subscription` (the owner's own job): `decide`'s rule. A model with no bucket or
  an open gate answers `subscription`; a closed gate needs an open window (`no window`,
  `window ended`), and the budget is `min(per-job cap, intakebot month remaining, overall month
  remaining)`, at least $1.00. Grants are not consulted. `grant` is `false`.
- Without `--peek`, a `credits` answer writes a `credit_job_reservation` row in the
  transaction that read the remainders. A reservation unsettled after 4 hours is booked at
  its full amount (a `credit_job_spend` row, session `expired:<reservation id>`) and paged;
  `release --job SPEND_ID` (needs a terminal) frees it after the Console says nothing was
  spent. Exit 1 when the database cannot be read; treat that as `hold`.
- `record-job` writes one `credit_job_spend` row, settles the newest open reservation for
  that job, principal and model, emits `credits-job-spent`, and fires the cap-reached
  notice for the grant (`grant:<principal>`), the intakebot month (`intakebot-month`) and the overall
  month (`month`). A grant has no window, so its notices de-duplicate per UTC month. With no
  reservation the row is still written.

**One month, one total.** `credits_monthly_usd` is review spend + review reservations + job
spend + job reservations, so a review and a job can never be granted the same money.

Grants (owner only, each needs a terminal; no flag skips that):

```
fleet-credits grant PRINCIPAL --monthly-usd N [--note TEXT]   N is 0 to credits_intakebot_monthly_usd
fleet-credits revoke PRINCIPAL                                 stops new spend; always succeeds
```

`PRINCIPAL` is intakebot's opaque id (`^[A-Za-z0-9._:-]{1,128}$`). A second `grant` replaces the
cap (and revives a revoked grant). `status [--json]` has a `intakebot` section: live grants with
this month's spend and reservations, the intakebot month total, and open job reservations.

Keys: `credits_intakebot_job_usd` (0-200, built-in 5) and `credits_intakebot_monthly_usd` (no
ceiling, built-in 25; the overall month bounds it). Events: `credits-grant-given` (`principal`, `monthly_usd`), `credits-grant-revoked`
(`principal`), `credits-job-spent` (`job`, `principal`, `model`, `usd`, `billing`:
`grant`\|`window`). `credits-cap-reached` for intakebot carries `cap`
(`grant`\|`intakebot-month`\|`month`), `principal`, `job`, `total`, `limit`.

Kill switches:

1. `fleet-credits off`: the review window and the owner's own intakebot jobs. It does **not**
   touch grants, which need no window.
2. `fleet-config set credits_intakebot_monthly_usd 0`: every intakebot `decide-job` answers `hold`,
   granted or not.
3. `fleet-credits revoke PRINCIPAL`: one principal.

A run already going finishes within its own `--max-budget-usd`; only revoking the key in the
Console stops it at once.

## Owner steps (nothing in the code can do these)

Intakebot (plan 0166): (a) check the Console workspace limit on the key at
`kv/revbot/anthropic`: intakebot spend now draws from it too, so that limit is the
backstop if the ledger is wrong; (b) once the intakebot worker's credits plan is built, run
`fleet-credits grant <principal> --monthly-usd N` for the first partner.

Reviews (plan 0146):

1. In the Anthropic Console, find the workspace that holds the key at
   `kv/revbot/anthropic` (`~/bin/fetch-anthropic-key.sh`, operator/hosttools).
   Credits are organisation-wide, so that once-unfunded key now spends the $200. Give that
   workspace a **monthly spend limit of $100**. That limit is Anthropic's and holds even
   if this ledger is wrong.
2. Confirm the key in keyvault is still valid, or replace it with
   `seed-secret.sh revbot/anthropic`.
3. Once talos plan 0029 is merged and the wrapper reinstalled, run
   `fleet-credits on --days 1` while Fable is closed to watch one review bill credits.
