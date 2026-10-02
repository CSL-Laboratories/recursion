# Capability-composition conformance verification result

- Verifier model + version:
- Session / operator:
- Date (YYYY-MM-DD):
- Candidate repository + commit SHA:
- Fresh checkout? (yes/no):
- Provenance (source/candidate hashes from results/*.json):
- Disposition vs failures (results/*.json `disposition` / `failures`):

## Commands run (raw outputs inline or attached)

```text
# paste literal output
```

## Per-check disposition

| Check | PASS / FAIL / NEEDS-EVIDENCE | Note |
|---|---|---|
| `check_suite.py` passes (structure, ids, sources) | | |
| `check_rpc_fixtures.py` passes (F01/F11/F12) | | |
| `check_rpc_invariants.py` passes (F02–F10, F13–F16) | | |
| Oracle/expectations do not import or derive from the candidate | | |
| Every planted mutant is detected | | |
| Fidelity to report §9 (literal specimen / predicates) | | |
| F17/F18 marked deferred, not counted as passes | | |
| Non-proofs preserved (no over-claims) | | |

## Residual unknowns

## Reviewer identity and decision

- Decision: ACCEPT / REJECT / NEEDS EVIDENCE
- Rationale:
