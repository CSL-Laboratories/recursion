# run

Harness, observers and independent oracles for the capability-composition conformance suite, added
per lane. The independent oracle must observe the actual candidate outputs (bytes, records, effects)
and must not trust candidate flags. The structural gate is
[`../check_suite.py`](../check_suite.py). The R-PC lane is implemented and rerun-verified at
`050936e` (`check_rpc_fixtures.py`, `check_rpc_invariants.py`) but is a **scaffold** pending
strengthening (see `../REVIEWS.md`): `check_rpc_observed.py` adds the harness-owned observation
boundary (`observer.py`) and strict record schema (`record_schema.py`) for F04/F06/F14/F16. R-UI/R-MOB
are pending.
