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

## P-003 — Founder GitHub PR approval as Recursion spec acceptance

- Date: 2026-10-25
- Status: **authoring session applied on this branch; decision record is CSLR-D-001 in
  csl-research; nothing takes effect until this PR merges**. Lucas posted the
  authorizing unprefixed sentence from his own account on
  [CSL-524](https://linear.app/csl-laboratories/issue/CSL-524) as comment
  `c8efc4d7-9742-43db-8289-dd8b3bbe9cc4` (2026-10-26 6:55pm PT). It replaces the
  file list in comment `3bf10093-062c-43bc-a9c3-f21bb8f6ba73` and names branch
  `cursor/csl-524-spec-acceptance-1752`. `state/DECISIONS.md` is out of scope and
  stays byte-identical to master. Lucas (2026-10-26 2:50pm PT, relayed by Grok Bot
  Liaison): "Move the R#296 decision record to csl-research with a new decision
  ID." The record is CSLR-D-001 (`decisions/CSLR-D-001.md`,
  [csl-research#59](https://github.com/CSL-Laboratories/csl-research/pull/59)). Not merged.
- Owner: Lucas.
- Issue: [CSL-524](https://linear.app/csl-laboratories/issue/CSL-524) (child of CSL-494;
  related CSL-493, CSL-523). Twin of CSL-523 in the companion repo.
- Full proposed diffs: moved to csl-research. Lucas (2026-10-26 3:37pm PT, relayed
  by Grok Bot Liaison): move the whole proposal to csl-research; recursion keeps only
  a link. Vault copy:
  [`contracts/recursion/amendments/ACCEPTANCE-RULE-2026-10-25.md`](https://github.com/CSL-Laboratories/csl-research/blob/2cbc2fabe9d797798af2f56f2549dd8fd07877e7/contracts/recursion/amendments/ACCEPTANCE-RULE-2026-10-25.md)
  ([csl-research#59](https://github.com/CSL-Laboratories/csl-research/pull/59), CSLR-D-001),
  SHA-256 `2bf5e8473639ae3707b812fd8a68017c1f38480d00789878137b30a57eb81298`.
  Provenance: byte-for-byte copy of `docs/v3/proposals/ACCEPTANCE-RULE-2026-10-25.md`
  at recursion commit `2d829ed74786f97ea5d6fc5eae38fcb31db32fa6`, removed from this
  repository after that commit.

### Motivation

Lucas (2026-10-24 11:58pm PT): Samir and Dan may change the product spec with their
agents; spec changes by Samir or Dan are accepted once another founder approves the
PR, bound to that head SHA and exact file digests. Lucas's path stays valid.

`CONTROL_PLANE.md` rule 6 forbids editing protected paths without that authoring
session. Comment `c8efc4d7-9742-43db-8289-dd8b3bbe9cc4` is that session. The vault
text is applied on this branch and takes effect only when this PR merges.

### Proposed replacement

The vault copy linked above is applied on this branch to `CONTROL_PLANE.md`, `ANCHOR.md`,
`AGENTS.md`, `CLAUDE.md`, `docs/v3/PLANNING.md`, `docs/v3/AUTONOMOUS-PLANNING.md`,
`docs/v3/DECISION-REGISTER.md` item 9, and `docs/v3/ACCEPTANCE.md`. The decision record
stays CSLR-D-001 in csl-research ([csl-research#59](https://github.com/CSL-Laboratories/csl-research/pull/59)).
`state/DECISIONS.md` is not appended. Nothing in the applied text takes effect until this
PR merges.

### Non-goals

- No protected-path edit beyond Lucas's CSL-524 authoring comment
  `c8efc4d7-9742-43db-8289-dd8b3bbe9cc4`. `state/DECISIONS.md` stays untouched.
- No merge. No acceptance of recursion#295. No change to Lucas's reserved
  decisions or to agent-never-merge.
- No other-project identifiers in tracked files.
- No entry in `state/DECISIONS.md` (frozen through D-044). The decision record
  is CSLR-D-001 in csl-research ([csl-research#59](https://github.com/CSL-Laboratories/csl-research/pull/59)).
