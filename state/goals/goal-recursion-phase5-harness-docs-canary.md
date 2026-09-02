schema_version: 1
id: goal-recursion-phase5-harness-docs-canary
project_id: recursion
status: active
outcome: Prove Phase-5 harness end-to-end on Recursion itself via a one-line docs note under allowed_paths.
acceptance_suite: test -f docs/PHASE5-HARNESS-READY.md
falsifier: Touches scripts/, state/, deploy/, credentials, or anything outside the allowed docs path.
budget: subscription Cursor only; one supervised worker; review branch only; no automerge

Supervised multi-project readiness canary after absorb of #83+#84.
