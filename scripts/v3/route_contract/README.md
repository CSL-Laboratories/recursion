# Route Contract v0 (draft, CSL-496)

Domain-neutral, provider-free draft. Not accepted, not normative, and not
live-capable. It stacks on the AT1 fake driver in `scripts/v3/route_adapter/`
(CSL-537) and does not modify it.

## What it is

- `scripts/v3/route_contract/schema.json` (generated; kept next to the
  package so `schemas/` stays a protected path) projects the
  frozen M1B shapes `RouteCapabilityDescriptor@1`, `RouteRequirement@1`,
  `SandboxDescriptor@1`, `ProcessRequirement@1`, `ProcessCapabilityDescriptor@1`
  and `ProcessTerminationObservation@1` byte for byte from
  `schemas/control-plane/v3/execution/schema.json`. It adds five draft records:
  - `RouteExecutionRequest@0`: zero-tools profile, prompt on stdin (digest only
    in the record), `CredentialRef` with no secret field, bounded output
    contract (JSON, one repair attempt, raw output never retained).
  - `RouteExecutionResult@0`: closed status and error tokens, usage state,
    the injected health event, and a `ProcessTerminationObservation` on timeout.
  - `RouteHealthEvent@0`, `RouteRefusal@0`, `AdapterSupport@0`.
- `ReferenceAdapter` is the conformance subject. It runs a request through a
  transport and the AT1 `FakeRouteDriver` (session ownership, custody, Busy).
- `live_adapter(engine_id)` always returns an adapter whose `support()` is
  `unsupported` / `awaiting-csl-460`. `LIVE_ADAPTERS_ENABLED` is a constant and
  `SubprocessTransport` refuses every known provider executable.

## Oracle

Argv, refusals and health outcomes are compared with golden vectors captured
from an existing production runner (commit and paths in
`scripts/tests/v3/route_contract/oracle_vectors.json`, `_provenance`). The
four oracle behaviours are zero-tools, secret refusal, health injection and
process-tree kill. Where this contract is stricter than the oracle it is listed
in `STRICTER_THAN_ORACLE` in the test (today: reordered Claude flags).

## Not covered (unverified or deferred)

- No live provider call. Every live adapter is `unsupported` until CSL-460.
- Tree kill is asserted on POSIX only. The Windows `taskkill` branch has no test
  in this PR. `tree_status` covers the child's process group; a descendant that
  leaves the group is not observable and is not claimed.
- Allow-listed tools, MCP servers and resumed sessions are outside v0.
- No distributed fencing (inherited from AT1: `DISTRIBUTED_FENCING` is false).
- Whether `scripts/v3/route_contract/**` and the new schema directory belong on
  the X1 keep-path list is Lucas's ruling (decision 3). Nothing here edits
  `scripts/v3/route_adapter/**` or its tests.

Regenerate the schema: `python3 -X utf8 -B -m scripts.v3.route_contract.schema --write`.
Run the suite: `python3 -X utf8 -B scripts/tests/v3/route_contract/conformance.test.py`.
