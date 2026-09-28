---
title: Recursion research metric schema
type: procedure
status: proposed
last-verified: 2026-09-28
related: [../RESEARCH-PROCEDURE.md, PLAN-20260928.md]
---

# Metric schema

Every research packet reports against this shared, measurable schema so packets can be synthesized.
There is **no single scalar score** (OR-017/OR-024): synthesis is **Pareto** over the vector, and
the owner selects a named profile (FAST / BALANCED / DEEP / MAX). Every metric has a unit, a stated
measurement method, and an explicit `unknown` state — missing telemetry is never zero.

## Objective vector

| Dimension | Metrics | Unit | Measurement |
|---|---|---|---|
| Quality | task success; tests passed; verifier score; regressions; human acceptance | rate / count | deterministic gates + independent verifier |
| Price | uncached input tokens; cached input; output tokens; subscription units; dollars | count / currency | provider receipts; unknown when unavailable |
| Speed | TTFT; tokens/sec; wall-clock; p50/p95/p99 input + frame latency | ms / tok/s | instrumented timers |
| System performance | CPU time; peak RSS; disk I/O; network bytes; sandbox startup; VRAM | ms / MB / bytes | OS counters |
| Context/cache efficiency | peak & average resident tokens; retrieved/evicted tokens; compactions; prefix-cache hit ratio | count / % | stream ledger |
| Configurability/portability | endpoints/engines supported; config portability; migration in/out cost | count / minutes | onboarding fixtures |
| Reliability/continuity | checkpoint/resume success; replay success; escalation frequency; human interventions | rate / count | fault fixtures |
| Onboarding | time-to-first-successful `/goal`; subscriptions/keys reused without re-entry; import success rate | minutes / % / rate | onboarding fixture |

## Rules

- Report every metric as `value(unit) | method | evidence-class`, or `unknown | blocker`.
- A feature must beat a simpler baseline on ≥1 dimension without a regression on a floor
  (authority, safety, privacy, correctness, recoverability).
- Ablations are the default experiment: baseline, then one added component at a time.
