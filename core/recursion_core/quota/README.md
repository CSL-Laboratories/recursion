# quota: the dollar reservation and, later, the usage ledger

Lane QUOTA's package (F9). Decided: money is a reservation that stays at zero unless the user opts in with their
own pay-per-token key, under a cap the user sets (grill Q44, O59); unknown stays unknown (pillar P10); the meter
is the connection, not the engine (grill Q8).

## What is here (slice 1)

`reservation.admit(connection, route, run_id)` is the admission at the breaker's admission. The route service
calls it before any adapter runs; lane DRIVER-CC keeps `routes/service.py` and applies the call there through a
guarded import (module absent: nothing changes). It answers a plain dict:

```text
{"allowed": true,  "code": null,                  "reservation_id": "rsv-<32 hex>", "reason": "no_spend" | "within_cap", "metered": bool}
{"allowed": false, "code": "reservation_refused", "reservation_id": null,           "reason": <refusal>,                 "metered": bool}
```

A refusal launches nothing. On the wire it is a launch-path refusal like every other: `start_failed` with
`start_error` {code: `reservation_refused`, detail: the reason}. It is not a direct code of `run.start`.
`reservation.release(run_id)` ends the run's reservation when the run is over.

| Reason | When |
|---|---|
| `no_spend` | The route cannot spend money: a subscription that bills nothing. Admitted with a zero hold. |
| `within_cap` | The route can spend; the user opted in; the cap is above 0; the connection's spend is known and below the cap; no other run holds the rest. The hold reserves the whole headroom left. |
| `no_opt_in` | KEYS' policy says no opt-in, or KEYS keeps no key by the connection's id. |
| `cap_zero` | Opted in, but the cap is 0 or absent (absent is 0). |
| `cap_reached` | The spend is known, or a lower bound, at or past the cap. |
| `cap_held` | Other runs on the same connection hold the headroom left. |
| `spend_unknown` | The spend is unknown, or only a lower bound below the cap: nothing shows the cap is not reached. |
| `policy_unavailable`, `policy_invalid` | KEYS failed or is not composed; or its answer is not `{metered_spend_allowed: bool, cap_usd: number >= 0}`. |
| `store_unavailable` | The reservation could not be recorded. Nothing is admitted, a free route included. |
| `route_invalid` | The call is malformed (the connection is not the route's, a bad run id). |
| `quota_not_composed` | Before composition installs a `Reservations`, a route that can spend is refused; one that cannot is admitted with no reservation id. |

What can spend money: an API-key route; a passthrough route; a subscription route the user told to bill its
apiKeyHelper (`billing_ack: api_key_helper` with `api_key_helper: present`); a subscription that the meters show
drawing on the plan's own paid fallback (usage credits, overage). A fallback route is admitted on its own, so a
switch to a paid route at a limit meets the same rules.

One reservation per run: a retried start on the same connection is checked again and reuses the run's held
reservation; on another connection the earlier hold is released (`superseded`).

## Where it keeps things

`tables.py` declares the lane's tables once through the event store's transaction helper (lane EVENTS'). The
table `quota_reservation` is append-only: one row per admission (`held` or `refused`) and one per release
(`released`, with `run_ended`, `superseded` or `refused_on_retry`). Money is decimal text; what admission knew of
the spend is kept with its quality and provenance, and an unknown spend is null, never 0.

KEYS' policy is read before the transaction. The spend and the holds are read, and the row written, in one
transaction. Nothing here calls the registry, the daemon or an engine.

## Not here yet

- The ledger (slice 2): `spent` and `paid_fallback_active` read from it. Until then every spend is unknown, so
  every route that can spend is refused (`spend_unknown`).
- The part of the start path that writes `budget {cap_usd}` into the run record (slice 2, `quota/hooks.py`).
- `budget.show` and `route.explain` (slice 3).
- A sweep that releases the holds of runs that ended while the daemon was down.
