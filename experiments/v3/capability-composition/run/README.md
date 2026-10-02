# run

Harness, observers and independent oracles for the capability-composition conformance suite, added
per lane. The independent oracle must observe the actual candidate outputs (bytes, records, effects)
and must not trust candidate flags. The structural gate is
[`../check_suite.py`](../check_suite.py). The R-PC lane is implemented and independently accepted at
`050936e` (`check_rpc_fixtures.py`, `check_rpc_invariants.py`); R-UI/R-MOB are pending.
