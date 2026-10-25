# AT1 route adapter (CSL-537)

In-process contracts for the first Route Adapter slice. No live provider
calls. The frozen M1 `RouteCapabilityDescriptor@1` schema is unchanged.
`RouteSurfaceDescriptor` is the AT1 admission row.

## What this slice covers

- **D-RP2-2.** One `SessionOwner` in this process. Default
  `FakeRouteDriver()` instances share `process_session_owner()`.
  Independent `(tenant, route, session)` triples run concurrently. The
  same triple is serialized. The contention policy is `busy-reject`. A
  second begin while the triple is held raises `Busy`. Generation
  numbers and `local-N` operation ids are process-local, and each
  operation carries the issuing owner's id so a colliding `local-N`
  from another owner cannot cancel or observe that hold. A stale
  `complete` is discarded and does not release a newer owner. `cancel`
  reaches the active owner directly. `DISTRIBUTED_FENCING` is false.
  Enqueue is not implemented; it is not the AT1 default.
- **D-RP2-3.** Public `CredentialRef` (tenant, provider, name).
  `LocalCredentialResolver.authorize` returns an `InvocationHandle`
  and does not return the secret. The stub is replaceable: `FakeRouteDriver`
  takes any resolver with `authorize`. Custody checks refuse another
  tenant. The stub does not read or write process-global environment
  variables. Adapter snapshots and errors carry the public reference
  only. `start` rejects, before authorize or acquire, when the session
  tenant, the caller tenant, and `CredentialRef.tenant_id` are not the
  same. `send` accepts a message and does not write that body into the
  public transcript or snapshot. Delivery is `runtime-visible-stub`
  (material stays inside the resolver object). Broker-owned egress is a
  later implementation of the same `authorize` shape.
- **D-RP2-4.** `grok_rows()` is three rows: `grok-build`, `grok-bot`,
  `grok-api`. Bot inbound session control is `unsupported`.
  Control-MCP Bot-to-Recursion caller-only is **not** admitted
  (`CONTROL_MCP_CALLER_ONLY_ADMITTED` is false). A descriptor cannot
  say `caller-only` until that constant is changed after validation.
- **D-RP2-5.** `admit_experimental_driver` sets Recursion's opt-in
  flag only. `vendor_experimental_api` stays false unless
  `require_vendor_experimental_api` is called with a required method name.

## Deferred

- **D-RP2-1.** Codex App Server `start → send → observe → cancel` is
  **skipped**. `CODEX_APP_SERVER_PROOF["status"]` is `skipped` and
  `live_calls` is false. The proof is gated on Lucas confirming
  existing authorized access. This package does not buy entitlements,
  borrow tokens, or place that call. If the proof is unavailable, the
  next live driver to design is API-keyed Claude (mocks or
  owner-authorized API fixtures only). Grok Build is third. The fake
  driver's start/send/observe/cancel sequence is fixture coverage, not
  live-driver acceptance.
- **D-RP2-6 / F-5.** The counsel gate stays **closed**
  (`F5_COUNSEL_GATE`). Subscription-token collection and unattended
  subscription fan-out are out of AT1 (`SUBSCRIPTION_FANOUT_IN_AT1` is
  false). That is a scope exclusion, not a legal finding of permission
  or prohibition.

Also deferred: distributed fencing, broker-owned egress, Vault,
service-account versus BYOK policy, production driver ranking, and any
live Claude or Grok call.
