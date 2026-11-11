# ConnectionBreaker contract (frozen)

Spec: PRODUCT.md d84cdd26 (CSLR-D-012), section 4.2 ("Two breakers"). Source:
`plugin_security/breaker.py`, frozen as it stands at #328 head `4f99503d`. A downstream
operator plugin builds against this surface, so it does not change: no renames, no
reordered arguments, no removed refusal codes and no changed return values. Additions
are allowed: new methods, new refusal codes, and new parameters that are keyword-only or
have a default. `tests/test_breaker_contract.py` fails on any change to the frozen part.

## Install and import

No pyproject. Consumers put `core/` on `PYTHONPATH`. The breaker itself needs only the
standard library; the rest of `plugin_security` needs `cryptography` (recipe in
`README.md`).

```python
from recursion_core.plugin_security import ConnectionBreaker, Refused, create_breaker_store, TYPED_LIMITS
```

## Surface

| Call | Returns | Refusals (`Refused.code`) |
|---|---|---|
| `create_breaker_store(path)` | `None`. Installer step: creates the store (0600, WAL) | `FileExistsError` if `path` exists (it never replaces a store) |
| `ConnectionBreaker(store_path, *, clock=time.monotonic, probe_lease_seconds=300.0)` | the breaker | `breaker_store_missing`, `breaker_store_invalid` |
| `bind_run(run_id, connection, plugin_id, provider, model_version)` | `None`. Same binding again is a no-op | `invalid_run_id`, `invalid_connection`, `invalid_plugin_id`, `invalid_provider`, `invalid_model_version`, `run_connection_mismatch`, `run_binding_mismatch` |
| `check(run_id, connection, plugin_id)` | `{"provider": ..., "model_version": ...}` | `run_unknown`, `run_not_owned`, `run_connection_mismatch`, `run_faulted`, `connection_tripped` |
| `report(connection, event_type)` | `True` when it tripped the connection; `False` for the core's own `core_timeout`, `core_deadline`, `caller_cancelled` | `invalid_connection`, `untyped_breaker_event` |
| `fault_run(run_id)` | `None` | `invalid_run_id` |
| `begin_probe(connection)` | lease id (`str`), one per tripped connection | `invalid_connection`, `connection_not_tripped`, `probe_lease_held` |
| `reset_after_probe(connection, lease, probe_ok)` | `True` only for the live lease and `probe_ok is True`; the trip is then cleared | `invalid_connection` |
| `tripped(connection)` | `bool` | none |
| `close()` | `None` | none |

`TYPED_LIMITS` = `plan_limit`, `rate_limit`, `usage_window_exhausted`: the only events that trip.
Ids match `[A-Za-z0-9_.:@/-]{1,128}`.

## Behaviour that is part of the contract

- Bindings, faults and trips are on disk and survive a restart. Probe leases are in memory: after
  a restart a tripped connection needs a new probe.
- A wrong lease returns `False` and leaves the held lease in place. A correct lease is spent by
  `reset_after_probe` whatever `probe_ok` is. An expired lease clears nothing.
- The store is created only by `create_breaker_store`; a missing store refuses, it is not created.
- Same-uid limit (B17): code in the same process or uid can rewrite the store. This is not a
  containment boundary.

## How the daemon uses it

`daemon/breaker.py` opens the store at `$XDG_STATE_HOME/recursion/plugin-security/breaker.sqlite3`
(creating it on first start, see S34 decision 5), binds each run (owner `host`), reports typed
connection limits and faults runs whose route result is an error. It calls only the surface above.
