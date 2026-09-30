---
title: DS-24 — Credentials, enrollment, egress and secret lifecycle
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-24 — Credentials, enrollment, egress and secret lifecycle

Run as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide the exact credential broker, auth-route separation, egress/classification enforcement, node enrollment and secret-lifecycle contract so an admitted route never widens authority, leaks a secret into model-visible output or a tracked file, or reconstitutes canonical state from a provider session. It is blocking because `E-FHAR2-104`/`105` and `SECURITY.md` list these boundaries as threat-tested requirements, and because `OR-019` forbids any external account, session or harness from owning or recovering canonical identity.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This lane must not enroll a node, read or write a secret store, contact a provider, or change a permission profile; `recursiond` is a hypothesis, not an implementation authorization. Do not infer universal OAuth permission from a technical interface, and do not decide distribution, billing or entitlement policy — entitlement, distribution and terms-of-service are separate facts that require pinned sources and legal review, not model opinion. Handle no real secret values anywhere in the packet.

## Inputs to read

- `CONTROL_PLANE.md`
- `ANCHOR.md`
- `AGENTS.md`
- `docs/v3/START-HERE.md`
- `docs/v3/SECURITY.md`
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/EXECUTION-FABRIC.md`
- `docs/v3/ROUTE-CAPABILITIES.md`
- `docs/v3/ACCEPTANCE.md`
- `docs/v3/STATE.md`
- `docs/v3/CONSTITUTIONAL-AMENDMENT-PROPOSAL.md`
- `docs/v3/DECISION-REGISTER.md`
- `docs/v3/foundation-research/compatibility.json`
- `docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md`

## Research questions

### Auth-route separation, entitlement and no token intermediation

1. How must API-key/BYOK, SDK OAuth, native-binary login and subscription-backed routes be represented as four separate route classes, and for each class what exactly distinguishes authentication from entitlement, from distribution permission, and from provider terms-of-service compliance?
2. Which documented provider terms restrict third-party login or token intermediation, and what is the exact source-pinned boundary between "user-owned native authentication of an unmodified binary" and "an application logging a user into a provider on their behalf"?
3. What is the negative fixture that proves Recursion never intermediates a token — for example storing, proxying, refreshing or re-signing one provider's credential to act as another principal — and how is that refusal observable in an audit receipt?
4. How do billing and quota attribution stay distinct from route identity across subscription, metered API and BYOK routes, so no route silently falls back to a metered account or debits a subscription for a helper call?
5. What must be verified before a public support claim about a route: intended distribution channel, permitted account type, observed authentication behavior, and any jurisdiction- or account-specific restriction?

### Credential broker and secret lifecycle

6. What is the exact contract for `recursiond` as sole credential broker: which secret locations are permitted (provider-native auth homes, an OS secret store, or a configured vault), and which locations are forbidden (Git, SQLite, prompts, worktrees, telemetry, context artifacts, plugin manifests, tracked files)?
7. How does a worker receive an opaque account/credential reference plus a temporary environment or home overlay exposing only the selected route, with the minimum lifetime and filesystem visibility, and no enumeration of unrelated accounts?
8. How is the overlay torn down deterministically after use, including on crash or kill, so a descendant process cannot retain access to a now-revoked credential reference?
9. What redaction and scanning pipeline guarantees that logs, evidence, prompts, tool output, error strings and telemetry never contain credential values before durable storage or publication, and what is the negative test where a secret-shaped token is emitted and must be caught?
10. How are credential references rotated without breaking content identity, what is the permitted behavior of a temporary overlay when the underlying secret is rotated or revoked mid-run, and how are credential soft-delete and cryptographic key destruction represented as proof states rather than booleans?

### Classification, egress and outbound-byte capture

11. How does every admitted project declare a data classification and egress policy with `private` and deny-by-default as the base, and how does the egress broker validate destination, resolved provider route, project classification, provider data-use policy, GoalSpec/network grant, secret-exposure scan, artifact classification and owner authorization together?
12. For contributor-tier or training-eligible routes, what exact route-specific acknowledgement is required, how does the egress broker capture and classify the exact outbound bytes before launch so prompt scanning is defense in depth rather than the boundary, and what negative fixture proves project configuration and prompt content cannot widen eligibility?
13. How are proxy, `HTTP_PROXY`/`HTTPS_PROXY`, PAC files, DNS resolution, DNS-over-HTTPS pinning and SNI handled so an approved destination cannot be silently redirected (DNS rebinding, route-rewriting proxy), and how are private cache/evidence artifacts kept project-scoped so digests, sizes and timing never become equality or membership oracles across trust domains?

### Node identity, authorization and revocation

14. How are the installation trust root, everyday operator credentials, and controller/executor/observer/verifier roles kept separate, and how does device enrollment prove possession plus explicit owner approval?
15. How are parent-goal envelopes and work packets signed, scoped, expiring and replay-protected (issuer, subject, audience, delegation depth, capability verbs, scope, limits, expiry, revocation epoch and fencing token), and what is the negative trace where a risk-based step-up is skipped or replayed?
16. How does offline revocation prevent new admission immediately while letting a disconnected node finish only its last-valid bounded operation and spool signed evidence, how is that evidence revalidated against GoalSpec version, fencing token, lease and PAUSE/revocation state after reconnect, and how is split brain resolved when two nodes believe they are executive?
17. What is the exact zero-model enrollment preflight, what trust-relevant inputs does it cache against (runtime hash, config hash, trust bundle, protocol versions, certificate state, permission snapshot), and how does it complete in seconds without invoking a model?

### Preflight, conformance, safety policy and threat tests

18. How is the full conformance suite mandatory for official v3 release evidence but optional for an individual owner's ordinary install, and how does a failure quarantine only the affected remote capability instead of disabling local Recursion?
19. How are provider refusals recorded and handled for continuity, what distinguishes an advisory "poor fit" recommendation the owner may ignore from a hard local boundary (credentials, privacy, egress, budgets, node identity, publication) enforced independently of model behavior, and under what exact conditions may a refusal trigger an explicitly configured alternate route without becoming evasion of a provider or legal safeguard?
20. For each threat in the required list — stolen node credential, replayed work packet, malicious project configuration widening egress or permissions, prompt/tool-output exfiltration, plugin dependency confusion or update compromise, symlink/reparse/path traversal across worktrees, stale route/GoalSpec after revocation, provider response forging evidence or completion, split-brain executive leases, disconnected node publishing stale work, public event metadata revealing private membership, and owner-hostile defaults preventing local recovery — what is the concrete fault injection and falsifier?

## Deliverable

A source-pinned report containing: a claim table (claim, source, version/date, classification, confidence, expiry trigger) with entitlement, distribution and terms-of-service kept as separate columns; a four-route authentication matrix with unknown cells explicitly `unknown`; a credential-broker contract naming permitted and forbidden secret locations; an overlay/opaque-reference lifecycle with teardown and rotation rules; a redaction-and-scan specification with negative tests; an egress/classification decision procedure with outbound-byte capture; a proxy/DNS threat analysis; a node identity/enrollment/step-up/revocation contract with signed-envelope fields; a zero-model preflight specification and cache keys; a release-versus-install conformance rule; a provider-refusal and authorized-alternate policy; and a threat-to-test matrix. Include a recommendation tied to a named competing option with a decisive falsifier plus open Owner questions (installation maximum, step-up cadence, delegated credential administration).

## Non-goals

- Do not enroll a node, contact a provider, read or write a secret, or change a permission profile.
- Do not decide distribution, billing, entitlement or legal policy.
- Do not implement `recursiond` or select a secret-store or vault product.
- Do not reopen Owner decisions or promote a research recommendation into a decision.
- Do not claim token intermediation or third-party OAuth is permitted without pinned source evidence.
- Do not weaken hard local boundaries (credentials, privacy, egress, budgets, node identity, publication).
- Do not place any real or synthetic secret value in the packet or any tracked file.
- Do not include private project content, transcripts, local paths, credentials or account identifiers.
- Do not present the contributor-route acknowledgement as a universal or default-enabled behavior.

## Completion gate

- Entitlement, distribution and terms-of-service are reported as separate source-pinned facts, never merged.
- The four auth routes each state a supported and a refused pattern, with a no-intermediation negative test.
- Permitted and forbidden secret locations are enumerated, and the packet contains no secret values.
- Redaction/scanning is specified with at least one negative token-emission test.
- Egress is deny-by-default with outbound-byte capture for contributor routes and a config-widening falsifier.
- Proxy and DNS redirection have explicit negative traces.
- Enrollment, role separation, step-up, offline revocation and disconnect spooling have signed-envelope fields and fault tests.
- The preflight is zero-model and cached against named trust-relevant inputs.
- The threat-to-test matrix covers every required threat with a concrete fault injection and falsifier.
- No protected path is edited, no runtime/provider/auth/cron/PAUSE/spending effect occurs, and open Owner questions are listed separately.
