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
- Status: **adopted** 2026-10-15. Lucas's owner decision on PR #189 authorizes the
  protected-policy change, retires `loop/work/<task-id>`, and keeps the force-push,
  merge, and deployment limits. Applied as D-040.
- Owner: Lucas.
- Issue: [CSL-76](https://linear.app/csl-laboratories/issue/CSL-76).

### Motivation

Historical, as of the proposal on 2026-10-15. The operating docs already named review
branches `csl-<N>-<slug>`, with an optional GitHub username prefix, while invariant 1
of `CONTROL_PLANE.md` still told agents to commit to `loop/work/<task-id>`. That file
outranked the operating docs, so the change needed an Owner authoring session recorded
in `state/DECISIONS.md`. D-040 applied that session. Invariant 1 no longer names
`loop/work/<task-id>`.

### Proposed replacement

Keep invariant 1's force-push, merge, and deployment limits. Name the review branch
`csl-<N>-<slug>`, optionally prefixed with a GitHub username. pr-lint matches
`CSL-[0-9]+` case-sensitively, so a lowercase branch name alone does not pass. The
`CSL-N` key in the PR title satisfies the check.

### Non-goals

- No edit to `ANCHOR.md`.
- No edit to `.github/workflows/**`. Workflow, pr-lint, script, and Claude/Devin
  findings stay listed on the CSL-76 PR.

---

## P-003 — Protected-doc edits needed by the Recursion spine build (WP16)

- Date: 2026-10-26
- Status: **proposed**. Confers no authorization. For Lucas's Oct 29 protected-doc session.
- Owner: Lucas.
- Issue: [CSL-495](https://linear.app/csl-laboratories/issue/CSL-495).

### Motivation

WP16 adds a loopback, token-authenticated Control API that runs only fake drivers
(recursion stack off R#302, `scripts/v3/control_api/`). No edit to `ANCHOR.md`,
`AGENTS.md` or `CONTROL_PLANE.md` was made. A search on 2026-10-26 found no line in
those three files that mentions a daemon, listener, loopback server or Control API, so
nothing conflicts and nothing authorizes it either. The items below are what the slice
needs recorded.

### Proposed edits (text for Lucas to author or reject)

1. `AGENTS.md` hard rule 8: add one sentence that a listener bound to a loopback
   address, authenticated per request, serving only fake drivers, is not a live
   provider, host or spend action, and that any non-loopback bind or live adapter needs
   a fresh admission. The code enforces this (`RemoteBindRefused`, `LIVE_ADAPTERS_ENABLED`).
2. `AGENTS.md` "Bounded issue execution": name `scripts/v3/control_api/rerun_frozen_sha.sh`
   as an acceptable way to produce the closeout record of the full 40-hex commit tested,
   the commands and their results. It does not replace independent verification.
3. `ANCHOR.md` in-scope list: say whether the Control API surface (CSL-498) is in scope.
   Carried, not new: the Step 4 decision 10 items (ANCHOR L44 and L46, `AGENTS.md` L62,
   `CONTROL_PLANE.md` L12-13).
4. RP0-v2 (WP7, not a protected file): record Lucas's 2026-10-26 confirmation that a thin
   direct-CLI fallback may exist and must be labeled as not satisfying X2, and that
   workload IDs stay neutral instead of narrowing the public-scope regex.

### Non-goals

- No change to `state/DECISIONS.md` and no new decision entry.
- No change to CSL-237 (stays Nov 12, Lucas's confirmation).

