---
title: DS-14 — Scientific research ARM and scholarly evidence
status: proposed-inert
last-updated: 2026-09-29
parent-goal: goal-recursion-v2-execution-fabric
invocation: only after Lucas explicitly invokes this file in a new session
---

# DS-14 — Scientific research ARM and scholarly evidence

Run this as GPT-6 Astra with Deep Research and repository-capable tools, or another route Lucas explicitly approves. This file is inert until Lucas invokes it; reading or creating it launches nothing and authorizes no provider call, write, or effect.

## Purpose

Decide whether Recursion can find papers and standards, reconcile conflicting evidence, design reproducible experiments and produce cited architecture claims within privacy, copyright and budget limits. This blocks the research ARM design and the acceptance hard floors for scientific evidence, false citations, retractions and reproducibility. It is blocking because adapter eligibility, claim-source typing, adversarial discrimination and the lean plugin boundary are still open.

## Authority and boundaries

Read `CONTROL_PLANE.md`, `ANCHOR.md`, `AGENTS.md` and `docs/v3/START-HERE.md` completely before planning, then only the inputs below. Preserve the current Hermes constitution; write proposed amendments, never edit it. Public Deep Research receives only public or synthetic briefs — never unpublished v3 drafts verbatim, private project content, transcripts, local paths, credentials, or account/infrastructure identifiers. No runtime, provider, auth, config, cron, PAUSE, spending, deployment, publication or merge is authorized. This prompt must not change provider, model or authentication configuration. Private source is never sent to a public or contributor route, and no identifier, benchmark or citation may be invented.

## Inputs to read

- `docs/v3/RESEARCH-AGENDA.md` (lane R16)
- `docs/v3/SCIENTIFIC-RESEARCH.md`
- `docs/v3/PLUGINS-AND-EDITIONS.md`
- `docs/v3/TELEMETRY.md`
- `docs/v3/R1-R17-EVIDENCE.md`
- `docs/v3/foundation-research/synthesis.md`

## Research questions

1. Which official scholarly metadata and search APIs (for example Crossref, OpenAlex, Semantic Scholar, arXiv, DataCite, Europe PMC or PubMed, and standards bodies) expose persistent identifiers, versions, relations, licenses, editorial states and graph edges at current documented versions?
2. For each candidate adapter, what coverage, pagination, rate limit, authentication, terms, provenance and privacy does official documentation state, so admission can be decided without a live trial?
3. How are corrections, retractions, withdrawals, supersessions and preprint-to-journal version changes detected and forced back through claim adjudication?
4. Which full-text licensing and access models permit storage, quoting, embedding or publication, and how are paywalled sources represented without breaching access limits?
5. How should `SourceRecord` and `ResearchClaim` carry provenance, typed source edges, applicability tuples, confidence, history and expiry without becoming an overgeneralized schema?
6. Which adversarial fixtures (fabricated identifier, near-match metadata, outdated version, paywall, contradictory study, inapplicable population or workload, citation ring, prompt-injected metadata, hostile table layout, duplicate preprint records) discriminate a robust pipeline from a plausible one?
7. How does the system return `unknown` or heterogeneity instead of a winner by citation count when populations, methods, interventions, endpoints or workloads differ?
8. What do PDF, table and figure extraction preserve, and how are extracted values treated as observations rather than established facts?
9. How are experiments preregistered, timestamped and reproducible, including corpus manifest, persistent-identifier and version set, raw-response hashes, environment and seed, exclusions with reasons and negative results?
10. How does a verifier reproduce retrieval and analysis when sources are paywalled or mutable?
11. What is the lean plugin boundary: which schemas stay in the kernel, and which search providers, PDF parsers, citation-graph tools and notebook runtimes remain lazy plugins with a proven no-network disabled path?
12. Which scientific domains need specialized statistical or methodological reviewers, and how is that routing recorded?
13. How are search cost, rate limits, model quota and diminishing returns bounded, and how are private project names, prompts and sensitive negative findings excluded from exports?
14. How do papers support a mechanism while local structural and empirical evidence decide Recursion fit, and how is that three-level claim stated explicitly?

## Deliverable

- A source-pinned claim table with classification, confidence and expiry, using typed source edges such as supports, contradicts, qualifies, updates and inapplicable.
- A comparison of scholarly adapters and the generic `SourceRecord` and `ResearchClaim` schema, with naming of candidates that remain unadopted.
- An adversarial fixture suite covering false citation, outdated version, paywall, contradiction, inapplicable population and hostile extraction.
- A preregistered, reproducible experiment packet and a lean plugin-boundary proposal.
- Options with tradeoffs and a recommendation tied to a competing option and a decisive falsifier, plus open Owner questions.

## Non-goals

- Fabricating identifiers, metadata, benchmarks, citations or experimental results.
- Treating model consensus, citation count, a search snippet or one paper as proof of local fitness.
- Storing or publishing full text without an applicable license.
- Sending private project content or credentials to a public or contributor route.
- Adopting a specific adapter, marketplace, notebook runtime or hosted service.
- Running paid searches, private-canary work or runtime implementation.
- Reopening accepted Owner decisions or promoting a candidate into a default.

## Completion gate

- Every adapter claim is pinned to official documentation with version and retrieval date.
- Every research claim carries typed source edges, applicability and confidence, or is marked unknown.
- Adversarial fixtures include false-citation, retraction, version, paywall, contradiction and inapplicable-population cases.
- No fabricated identifier or benchmark appears anywhere in the report.
- Local-fit conclusions require local structural or empirical evidence, not papers alone.
- Every recommendation names a competing option and a falsifier.
- Privacy, copyright and secret scans are clean and no protected path is edited.
- Open Owner questions are listed and no decision is promoted.
