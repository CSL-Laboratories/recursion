# quota: the dollar reservation and, later, the usage ledger

Lane QUOTA's package (F9). Decided: money is a reservation that stays at zero unless the user opts in with their
own pay-per-token key, under a cap the user sets (grill Q44, O59); unknown stays unknown (pillar P10); the meter
is the connection, not the engine (grill Q8).

## What is here (slice 1)

`reservation.admit(connection: str, route: Mapping, run_id: str) -> dict` is the admission at the breaker's
admission. The route service calls it before any adapter runs. Lane DRIVER-CC keeps `routes/service.py` and
applies the call there through a guarded import: with the module ABSENT nothing changes. It answers a plain dict:

```text
{"allowed": true,  "code": null,                  "reservation_id": "rsv-<32 hex>" | null, "reason": "no_spend" | "within_cap", "metered": bool}
{"allowed": false, "code": "reservation_refused", "reservation_id": null,                  "reason": <refusal>,                 "metered": bool}
```

- **A refusal launches nothing.** On the wire it is a launch-path refusal like every other: `start_failed` with
  `start_error` {code: `reservation_refused`, detail: the reason}. It is not a direct code of `run.start`.
- **Admission is where the reservation is made.** There is no separate `reserve` call.
- **`reservation.release(run_id) -> list[str] | None`** ends a hold whose final usage is known, and marks a
  metered hold with no final usage, or with an unreconciled unknown effect, `unresolved`. An unresolved hold
  still counts. It returns the ids this call recorded. `[]` means none was open, or the hold was already
  unresolved, so a second release writes nothing. `None` means the store could not record it; the hold then
  stays and still counts.
- **Composition** builds `reservation.Reservations(db, spend_policy, *, reads=None)` and calls
  `reservation.install(reservations)` once. A second call raises `RuntimeError`; anything but a `Reservations`
  raises `TypeError`. Before it does, a route that can spend is refused (`quota_not_composed`), and one that cannot
  is admitted with no reservation id and no row.
- **`reads`** answers two calls inside admission's transaction (`reservation.NoReads` until the ledger exists):
  - `spent(tx, connection) -> Spend`: anything that is not a `Spend` counts as unknown;
  - `paid_fallback_active(tx, connection) -> bool | None`: True or False is a typed reading; None is no reading,
    and the connection's last typed reading stands; anything else, or an exception, refuses.

| Reason | When |
|---|---|
| `no_spend` | Allowed. The route cannot spend money: a subscription that bills nothing. A zero hold. |
| `within_cap` | Allowed. An API-key route; KEYS says the user opted in; the cap is above 0; the connection's spend is known and below the cap; the headroom left after other runs' holds is above 0. The hold reserves that headroom. |
| `no_opt_in` | KEYS' policy says no opt-in, or KEYS keeps no key by the route's connection id. |
| `cap_zero` | Opted in, but the cap is 0 or absent (absent is 0). |
| `cap_reached` | The spend is known, or a lower bound, at or past the cap. |
| `cap_held` | Other runs on the same connection hold the headroom left. |
| `spend_unknown` | The spend is unknown, or only a lower bound below the cap: nothing shows the cap is not reached. |
| `consent_unsupported` | The route can spend money through a funding source that has no consent interface yet: a passthrough (a cloud or gateway bills it), a subscription billing its apiKeyHelper (`billing_ack`), or a subscription on its plan's paid fallback. KEYS' policy is a stored key's and is never applied to another kind of connection that shares its id. |
| `paid_fallback_unknown` | A subscription's meter read failed, or did not answer True, False or None. |
| `policy_unavailable`, `policy_invalid` | KEYS failed or is not composed; or its answer is not `{metered_spend_allowed: bool, cap_usd: number >= 0}`. |
| `store_unavailable` | The reservation could not be recorded. Nothing is admitted, a free route included. |
| `route_invalid` | The call is malformed: the connection is not the route's, the run id is not 1 to 64 of `[A-Za-z0-9_-]`, or the engine, model or connection id is empty. |
| `quota_not_composed` | No `Reservations` is installed, and the route can spend money. |

**What can spend money:**
- an API-key route;
- a passthrough route;
- a subscription route the user told to bill its apiKeyHelper (`billing_ack: api_key_helper` with
  `api_key_helper: present`);
- a subscription that the meters show drawing on the plan's own paid fallback (usage credits, overage).

Only the first can be admitted today. A fallback route is admitted on its own, so a switch to a paid route at a
limit meets the same rules.

**One current reservation per run.** A retried start is checked again.
- It reuses the run's hold when that still fits: a free hold for a free attempt, or a paid hold no larger than
  the headroom now left.
- Otherwise the hold is released (`superseded`) and a new one made, in the same transaction.
- On another connection the earlier hold is released (`superseded`).
- A refused retry releases the run's hold (`refused_on_retry`).
- A raised cap opens new headroom that another run can take.

## Where it keeps things

- `tables.py` declares the lane's tables once through the event store's transaction helper (lane EVENTS').
- The table `quota_reservation` is append-only: one row per admission (`held` or `refused`), one marker when a
  restart sweep keeps a metered hold with no final usage (`provisional`) or when release leaves it `unresolved`,
  and one `released` row when the hold ends (`run_ended`, `superseded`, `refused_on_retry`, or `settled`).
- Money is an integer number of micro-USD, also kept as that integer's exact decimal text. What admission knew
  of the spend is kept with its quality and provenance; an unknown spend is null, never 0.
- `quota_settle` is one row per reservation id, status `settled` or `unresolved`. A second settle leaves that row.
  `ledger.Ledger(db).read(reservation_id)` reads that row back. A new `Ledger` on the same store reads the
  same row, and a read writes nothing. An unresolved amount is null, never 0. A known zero stays 0.
- `quota_usage` is the run's reported cost (null when unknown). `quota_effect` records `outcome_unknown` until
  `reconcile_effect`. `quota_session_spend` is the daemon's own session book: a later cumulative total replaces
  the earlier one, and the engine's budget flag is stored and not counted as spend.
- A subscription's typed meter reading is kept on its admission row (`paid_fallback`: `active` or `inactive`).

KEYS' policy is read before the transaction, for API-key routes only. The spend, the holds and the last meter
reading are read, and the rows written, in one transaction. Nothing here calls the registry, the daemon or an
engine.

## Not here yet

- The usage-event feed, the session-total span rules, the meters, and `paid_fallback_active` read from a ledger.
  Until a reader is passed in, every spend is unknown, so every route that can spend is refused (`spend_unknown`),
  except a session whose daemon book already has a figure.
- A consent interface for funding sources other than a stored key (passthrough, helper billing, plan overage).
- The part of the start path that writes `budget {cap_usd}` into the run record (`quota/hooks.py`).
- `budget.show` and `route.explain` (slice 3).
- Replaying the event log into the settle rows after a restart. `Ledger` reads the rows that were written;
  catching the book up from the log needs the event-store reader, which is not in this slice.
