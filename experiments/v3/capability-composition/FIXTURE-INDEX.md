---
title: Capability-composition fixture index (reconciled)
status: registry
last-updated: 2026-10-02
---
# Capability-composition fixture index

Reconciled registry of every fixture across the three lanes. Status values: `specified` (defined in
the source report), `partial` (fixture + independent oracle + detected mutant + gate exist, but the
advertised full predicate set is not yet asserted — see `REVIEWS.md`), `implemented`, `verified`
(independently reviewed), `deferred` (specified but not provider-free behavioral evidence). Neither
`partial` nor `implemented` is full conformance evidence or Owner acceptance.

## R-PC — composition conformance (F01–F18)

Source: `docs/v3/research-lane/capability-composition-20261002d/rpc/report.md` §9 (provider-free
acceptance). Corpus: the synthetic benchmark corpus (immutable input).

| ID | Fixture | Status |
|---|---|---|
| F01 | Frozen reproduction — 23-byte literal specimen; reversed source enumeration reproduces the same bytes | partial |
| F02 | Generated-summary identity — outputs A and B are distinct; replaying A executes no regeneration | partial |
| F03 | Mandatory closure under pressure — required members survive overflow handling | partial |
| F04 | Disabled capability zero contribution — zero bytes/registrations/selector queries | partial |
| F05 | Immediate revocation with coalesced preferences — revocation not erased by coalescing | partial |
| F06 | Authority laundering — imported content cannot authorize a protected action | partial |
| F07 | Procedure precedence — declared precedence/override, never load order | partial |
| F08 | Evidence conflict — both sources preserved; no synthesized decision | partial |
| F09 | Coherent read cut incl. ABA — cut unavailable rather than guessed | partial |
| F10 | One governor + stale parent — compare-and-install rejects stale parent | partial |
| F11 | Prefix continuity + required forfeiture — correctness beats reuse | partial |
| F12 | Span/role coverage incl. Unicode byte mutation and omitted wrapper | partial |
| F13 | Route/schema drift — re-admit disclosure; no cache-continuity shortcut | partial |
| F14 | Checkpoint resume — historical grants never reactivate | partial |
| F15 | Graph errors before load — cycle/ambiguity fails before body loading | partial |
| F16 | Observer/oracle sensitivity — independent oracle detects planted faults | partial |
| F17 | N-increment accounting — 5 increments × 4 arms × 6 tasks = 120 scripted cells | deferred |
| F18 | Scaling adjudication sensitivity — scripted-only disposition labels | deferred |

Gate: `run/check_rpc_fixtures.py` (F01, F11, F12) and `run/check_rpc_invariants.py` (F02–F10,
F13–F16). F17/F18 are `deferred`: scripted accounting/analysis, not provider-free behavioral
evidence.

**Verification:** rerun **ACCEPT** at `050936e` (GLM-5.3-Flash; result in `results/`). **Full
F01–F16 specification fidelity is ISSUES FOUND / NEEDS EVIDENCE** (static review Q01–Q10) — hence the
`partial` status; see `REVIEWS.md`. Strengthening the tested code requires re-verification. R-UI/R-MOB
remain `specified`.

## R-UI — UI projection conformance (UI-01…UI-20)

Source: `docs/v3/research-lane/capability-composition-20261002d/rui/report.md` (candidate-2).

| ID | Fixture | Status |
|---|---|---|
| UI-01 | Determinism — 100 baseline runs plus 24 permutations (124 outputs) byte-identical | specified |
| UI-02 | Locale/profile precision — equal profile ⇒ equal tree; lexical ID order preserved | specified |
| UI-03 | Unknown/version — typed omission/blocker; no synonym/default version | specified |
| UI-04 | Downgrade bypass — every ingress refuses missing mandatory semantics | specified |
| UI-05 | Collisions — candidate-2 supplier-qualified keys; duplicate keys refuse; occurrence indices | specified |
| UI-06 | Disabled inertness — exact `DisabledActivityTrace` zeros | specified |
| UI-07 | Revocation online — backend fence then observed invalidation | specified |
| UI-08 | Revocation offline — no private hydration/replay on reconnect | specified |
| UI-09 | Direct access/spoof — independent sinks confirm zero forbidden disclosure | specified |
| UI-10 | Terminal/URI injection — ESC/OSC52/DCS/malformed corpus; parser recovery | specified |
| UI-11 | Ten-control parity across clients at one cut | specified |
| UI-12 | Three-Stop parity across clients | specified |
| UI-13 | Lease refusal matrix | specified |
| UI-14 | Replay/install — frozen RESET/GAP/replay and snapshot-install semantics | specified |
| UI-15 | Command identity — dedup, no new effect | specified |
| UI-16 | Accessibility fallback — semantic screening separate from AT conformance | specified |
| UI-17 | Zero extra work on passive views | specified |
| UI-18 | C×V soak — 25 cells (C,V ∈ {0,1,5,10,20}) × idle/high-output | specified |
| UI-19 | Bounded parser/materializer limits | specified |
| UI-20 | Shared provenance round-trip; no new base field/epoch/loss store | specified |

## R-MOB — mobile conformance (MOB-F01…MOB-F24)

Source: `docs/v3/research-lane/capability-composition-20261002d/rmob/report.md` Q23.

| ID | Fixture | Status |
|---|---|---|
| MOB-F01 | Enrollment binding | specified |
| MOB-F02 | Clone boundary | specified |
| MOB-F03 | Phishing/relay/downgrade refusal | specified |
| MOB-F04 | Scope / connected-only | specified |
| MOB-F05 | Input-lease race | specified |
| MOB-F06 | Ten-control parity | specified |
| MOB-F07 | Stop uncertainty | specified |
| MOB-F08 | Lost online ack / receipt reconciliation | specified |
| MOB-F09 | Offline honesty (last-synchronized) | specified |
| MOB-F10 | Cache cross-scope refusal | specified |
| MOB-F11 | Offline revocation impossibility (honesty) | specified |
| MOB-F12 | Expiry / restore | specified |
| MOB-F13 | Storage / backup | specified |
| MOB-F14 | GAP / replay-gap continuation | specified |
| MOB-F15 | Notification no-authority | specified |
| MOB-F16 | Notification limits / privacy | specified |
| MOB-F17 | Detach / reattach | specified |
| MOB-F18 | N-device / N-view soak | specified |
| MOB-F19 | Saturation / fairness | specified |
| MOB-F20 | Source / contract integrity | specified |
| MOB-F21 | Disclosure ordering | specified |
| MOB-F22 | Resource conservation (S+O+H+P ≤ L) | specified |
| MOB-F23 | Accessible model journeys | specified |
| MOB-F24 | Counsel readiness (questions, not a legal pass) | specified |

## Shared client matrix (MP01…MP11)

Source: `docs/v3/research-lane/ios-android-20261002/{ios,android}/report.md`.

| ID | Fixture | Status |
|---|---|---|
| MP01 | Same-cut control parity | specified |
| MP02 | Back/gesture detach | specified |
| MP03 | Connectivity and ambiguous receipt | specified |
| MP04 | Accessible controls and Stops | specified |
| MP05 | Stream flood and control reserve | specified |
| MP06 | Lifecycle and restoration | specified |
| MP07 | Enrollment and backup | specified |
| MP08 | Notifications and deep links | specified |
| MP09 | Adaptive input | specified |
| MP10 | Energy and background | specified |
| MP11 | Contribution failure | specified |

## Reconciliation note — `MOB-Fxx` vs `MPxx`

The two namespaces are **not yet proven to be 1:1**. `MP01…MP11` is the cross-platform *shared
semantic scenario matrix*; `MOB-F01…F24` is the R-MOB *mobile fixture family*. Candidate
correspondences are recorded below as `inferred` and must be confirmed against the reports and the
independent oracle before either set is treated as a superset. Unmapped fixtures stay `unknown`.

| MPxx | Candidate MOB-Fxx correspondence | Evidence class |
|---|---|---|
| MP02 detach | MOB-F17 detach/reattach | inferred |
| MP03 ambiguous receipt | MOB-F08 lost online ack | inferred |
| MP04 accessible controls | MOB-F23 accessible journeys | inferred |
| MP05 stream flood | MOB-F19 saturation/fairness | inferred |
| MP06 lifecycle/restore | MOB-F12 expiry/restore, MOB-F13 storage/backup | inferred |
| MP07 enrollment/backup | MOB-F01 enrollment, MOB-F13 storage/backup | inferred |
| MP08 notifications/links | MOB-F15 no-authority, MOB-F16 limits/privacy | inferred |
| MP11 contribution failure | (R-UI UI-03/UI-05 territory) | inferred |
| MP01, MP09, MP10 | no confirmed mapping yet | unknown |
