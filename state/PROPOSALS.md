# Proposals — protected-path and control-plane changes awaiting Owner action

Per `AGENTS.md`, changes to protected paths or the control plane are **proposed** here, never
performed as part of a product task. A proposal is not authorization.

---

## P-001 — Repository topology: plugins outside the core

- Date: 2026-10-02
- Status: **proposed**; principle accepted (`state/DECISIONS.md` D-035); **no repository split is
  authorized.**
- Owner: Lucas.

### Motivation

Keep the core repository lean; enable a community plugin ecosystem; and match the existing design:
the plugin portability contract (R-A), opt-in install (D-019), porting-as-hypothesis (D-033) and
the "scales with your workflow" thesis (D-034) already assume plugins are **separate artifacts
referenced by digest**, not vendored source.

### Principle (accepted)

- Plugins are **external artifacts**; first-party plugins ship in editions by digest/lockfile.
- Keep the core product in **this single repository** during R&D.
- Split only when the plugin/capability contract, installer and registry/signing exist and the
  kernel boundary is frozen.

### Target shape (long-term, not now)

| Repo | Role |
|---|---|
| `recursion` (this repo) | core/brain: kernel, authority/continuity, contracts, CLI, backend, first-party client(s), specs |
| `recursion-plugins/*` | first-party and community plugins |
| `recursion-registry` / installer | distribution, digests, signatures, provenance, lockfile |
| `recursion-gui`, `recursion-mobile` | only if they grow enough to justify extraction |

### Not now / non-goals

- No repository rename or split; no edit to the `CONTROL_PLANE.md`/`ANCHOR.md` single-product framing.
- No extraction while Session A/Stage B verification and packet `d` are mid-flight (it would
  invalidate their pinned source identities).

### Prerequisites before any split

1. Plugin/capability contract frozen (R-A) + installer (G-09) + registry/signing/provenance.
2. Shared command/query and projection contract stable (R-L / R-UI / packet `d`).
3. A frozen kernel boundary (after Stage B verification and the kernel-freeze decision).
4. An explicit control-plane **authoring session** amending `CONTROL_PLANE.md`/`ANCHOR.md`
   (single product), recorded in `state/DECISIONS.md`.
5. Cross-repo CI: a versioned contract, conformance suite and a cross-repo index (to avoid
   recreating the v1/v2 drift).

### Monorepo → polyrepo migration checklist

- [ ] Freeze the plugin manifest + capability-graph contract; publish it as a versioned spec.
- [ ] Build the installer + registry (digest/signature/provenance) and the lockfile.
- [ ] Move plugins out: one repo per plugin (or an org for first-party), each with conformance CI
      against the frozen contract.
- [ ] Reference plugins by digest; remove vendored plugin source from core; keep editions as
      manifests.
- [ ] Add cross-repo version pinning, a compatibility matrix and a cross-repo index.
- [ ] Only then consider extracting GUI/mobile.
- [ ] Amend `CONTROL_PLANE.md`/`ANCHOR.md` and record the topology change in `state/DECISIONS.md`.

### Governance

Any actual split requires Lucas's explicit approval recorded via an authoring session. This file is
a proposal and confers no authorization. Because the repository is still in R&D and is still
growing, the default is to **stay monorepo** until the prerequisites above hold.

---

## P-002 — Replace the legacy branch name in `CONTROL_PLANE.md`

- Date: 2026-10-15
- Status: **proposed**. Not authorized. CSL-76 does not edit this protected path.
- Owner: Lucas.
- Issue: [CSL-76](https://linear.app/csl-laboratories/issue/CSL-76).

### Motivation

Current operating docs (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, the PR template,
`docs/v3/START-HERE.md`, and `docs/v3/RESEARCH-SCOPE.md`) name review branches
`csl-<N>-<slug>`, with an optional GitHub username prefix. Invariant 1 of
`CONTROL_PLANE.md` still tells agents to commit to `loop/work/<task-id>`. That file
outranks the operating docs on conflict, and changing it needs an Owner authoring
session recorded in `state/DECISIONS.md`.

### Proposed replacement

Keep invariant 1's force-push, merge, and deployment limits. Name the review branch
`csl-<N>-<slug>`, optionally prefixed with a GitHub username. pr-lint matches
`CSL-[0-9]+` case-sensitively, so a lowercase branch name alone does not pass. The
`CSL-N` key in the PR title satisfies the check.

### Non-goals

- No edit to `CONTROL_PLANE.md`, `ANCHOR.md`, or `state/DECISIONS.md` in CSL-76.
- No edit to `.github/workflows/**`. Workflow, pr-lint, script, and Claude/Devin
  findings are listed on the CSL-76 PR for Lucas.
