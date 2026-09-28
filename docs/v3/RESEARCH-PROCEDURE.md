---
title: Recursion v3 research procedure
type: procedure
status: proposed
last-verified: 2026-09-28
related: [SCIENTIFIC-RESEARCH.md, RESEARCH-AGENDA.md, RESEARCH-SCOPE.md, CLAIM-LEDGER.md, SOURCE-REGISTER.md, research-lane/PLAN-20260928.md]
---

# Recursion v3 research procedure

How an idea (from the Owner, the Astra report, the Gemini harness conversation, or an agent)
becomes **verified evidence** that can change the spec. This builds on
[SCIENTIFIC-RESEARCH](SCIENTIFIC-RESEARCH.md); it does not replace it.

## Precedence

1. The Owner's current explicit decision (including the 2026-09-28 identity/`/goal`/ACP answers).
2. Accepted spec direction (`DECISION-REGISTER.md`, `PRODUCT.md`).
3. A pinned external source (code, docs, paper) — a *source*, never authority.
4. An agent's inference or a model consensus — weakest; never a proof.

A third-party recommendation never overrides an Owner answer, and a paper never proves local fit.

## Evidence classes (never conflated)

| Class | Meaning | Example |
|---|---|---|
| `source-documented` | a specific upstream revision does X | a pinned commit/file |
| `locally-observed` | a run in Recursion's own environment measured X | a gate output |
| `inferred` | a reasoned conclusion from sources | a comparison |
| `unmeasured` / `unknown` | not established, with a named blocker | no counter available |

A claim is only as strong as its weakest cited class. "No result" is a valid, recorded outcome.

## A Research Packet

One idea → one packet, under `docs/v3/research-lane/<lane>/`, with a runnable check where possible:

- **SPEC.md** — question; which spec decision it informs; priority; sources (pinned URL/commit);
  method; evidence-class target; **falsifier** ("if observed, this was wrong"); deliverable;
  external receipt path; independent verifier.
- **check.py** (when deterministic and provider-free) — takes `--candidate FULL_SHA`, prints a
  single pass/fail JSON line; fails closed on stale/forged/incomplete evidence.
- **Receipt** — `research-attestations/<lane>/<candidate>/` in the Git common directory.

## Lifecycle

1. **Propose** — Owner or agent writes the packet. No gate → it is not admitted (rule: no gate, no work).
2. **Admit** — the packet names its sources and budget; a provider/host/spend need is Owner-gated.
3. **Execute** — run the gate; never treat a worker's self-report as acceptance.
4. **Verify** — an **independent** verifier (different session/family) reruns the exact candidate.
5. **Synthesize** — see below.

## Gates

- Prefer **deterministic, provider-free** gates (a script that reads pinned artifacts).
- A gate must fail closed on: skipped checks, forged/stale evidence, changed candidate, incomplete domain.
- Where no deterministic gate is possible yet, the packet must still produce an explicit
  `unknown` with a named blocker — never prose that reads like a result.

## Synthesis

- Each packet emits `CLAIM-LEDGER.md` rows carrying: claim, status, source, reviewer, limits,
  applicability, and the falsifier. Dissent and counterexamples are preserved, not smoothed.
- Accepted direction updates `DECISION-REGISTER.md`; product impact updates `PRODUCT.md`.
- New external sources are pinned into `SOURCE-REGISTER.md` (exact commit/URL + retrieval date).
- **Preserve negative results** with the reason: a ruled-out approach without its "why" will be
  rediscovered.

## Priority

Astra's P0/P1/P2 ordering is the default, **but the Owner's answers override**. The 2026-09-28
identity, `/goal`, ACP, session, model-plane and auth changes are P0 for spec work.

## Anti-drift rules

- One canonical target (v3); v1/v2 are retired and never a research input.
- Generated output faster than it is verified is a liability, not progress — generate less.
- A packet that needs a live provider, spend or a host change is Owner-gated and named as such.
