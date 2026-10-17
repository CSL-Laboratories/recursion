# Decision log

Every decision gets a falsifier — the specific thing that, if observed, means this was wrong.
Without it, review is hindsight.

**Review cadence:** first of the month. Pull decisions ≥60 days old, check whether the
falsifier fired, mark `HELD` / `FAILED` / `UNCLEAR`. Do not edit the original entry.

---

## D-017 — Recursion v3 is greenfield; v1/v2 are retired, not migrated
Date: 2026-09-28 · Status: OPEN

**Options considered:** A) migrate v2 aggregate-by-aggregate into v3 (the original
`CURRENT-AND-MIGRATION` / OR-019 strangler) / B) keep both versions side by side / C) treat v3 as
a brand-new harness that inherits nothing from v1/v2 and retire the old control plane.

**Chose:** C.

**Because:** v3 is a from-scratch provider-neutral agent harness (its own client, execution
kernel, context/compaction engine and node fabric). The v1/v2 Hermes-based control plane
contributed no dependency to it, and version coexistence caused the drift that blocked
contributors. The complete prior tree is preserved at tag `pre-v3-clean-cut-20260928`.

**Consequence:** the pre-v3 decision log (D-001 through D-016) and the v2 migration framing in
`docs/v3/CURRENT-AND-MIGRATION.md` are historical lineage only and authorize no current work.
`CONTROL_PLANE.md` and `ANCHOR.md` were rewritten for a single-product repository under Lucas
alone, with coding agents as bounded executors and no standing executive.

**Falsifier:** if v3 turns out to require v2 state, schema or a migration path to function, this
was wrong.

**Review:** 2026-12-01

---

## D-018 — Thesis: Recursion is a native harness *and* a unifying wrapper; no ecosystem lock-in
Date: 2026-10-02 · Status: ACCEPTED

**Options considered:** A) keep the 2026-09-07 "not another … provider router" framing / B)
reposition Recursion as both a first-class native harness and a first-class wrapper over the
wider harness ecosystem, whose purpose is to remove lock-in and cut quota/usage burn, shipped as
a community, open-source product.

**Chose:** B.

**Because:** users should be able to use the models and subscriptions they already prefer across
many harnesses; much of that surface is reachable by adapter/porting rather than greenfield work;
and a community/open-source posture is the growth vector.

**Consequence:** `PRODUCT.md` and `ANCHOR.md` are reframed; the "provider router" non-goal is
reworded to "not a naive router"; the compatibility inventory expands with plugin-supported
harnesses. The claim that vendors deliberately unoptimize quota is recorded as a **hypothesis
requiring a falsifiable experiment**, never as fact. Release or publication of any
credential-reliant route stays counsel-gated (G-32).

**Falsifier:** honest measurement shows Recursion cannot reduce quota/usage burden, or cannot
integrate routes without violating provider terms.

**Review:** 2026-12-01

## D-019 — Release model: build comprehensively, ship lean, install opt-in (Dot Option B rejected)
Date: 2026-10-02 · Status: ACCEPTED

**Options considered:** A) lean default with all accepted capabilities available through
supported components/profiles / B) a smaller initial release with revised launch gates and named
deferrals (the Dot's recommendation).

**Chose:** A. The Dot's Option B is rejected as the release model.

**Because:** a 2-developer project needs the full capability and plugin surface at first release
to be adopted; a large fraction of that surface is port/optimize work ported from existing plugin
ecosystems, not new invention; and deferrals shrink the product without meaningfully shrinking
the engineering.

**Consequence:** the comprehensive contract in OR-021…OR-025 stands; every capability is built and
ships as an opt-in plugin/profile, not default. The Dot's seven proposed deferrals (seven-name
launch gate, remote nodes, coordination topologies, universal plugin adaptation, six-mode
updates, GUI, adaptive compaction/research) become *built, opt-in, not default* unless separately
removed.

**Falsifier:** delivery evidence shows the comprehensive surface cannot reach a usable first
release, or that porting leverage is far lower than assumed.

**Review:** 2026-12-01

## D-020 — Availability contract: automatic node takeover required; core is single-node (closes G-06)
Date: 2026-10-02 · Status: ACCEPTED

**Options considered:** for canonical-state availability after coordinator loss: A) fail-closed,
writer may be temporarily unavailable / B) another enrolled node must take over writing
automatically; and for packaging: core-only vs plugin.

**Chose:** B for availability (user-toggleable, required for long-running autonomous work), with
the shipping **core single-node**. The kernel owns the monotonic fence/epoch and a store/
availability seam; the replicated store + coordinator election ship as a first-party plugin in
the `remote`/`full` editions. Default install compiles in no consensus.

**Because:** auto-takeover can only be a *safe* plugin if fencing is core; otherwise two writers
can commit. This preserves one-writer correctness while giving zero-downtime failover when the
plugin is installed.

**Consequence:** closes G-06. Adds a store/availability seam to the architecture; names
availability backends and node transports as capability plugins; distinguishes multi-node
*execution* (first release, OR-025) from multi-node *canonical-writer failover* (plugin).

**Falsifier:** a plugin cannot implement takeover against the core fence contract, or the seam
forces consensus into the core.

**Review:** 2026-12-01

## D-021 — Delegation over credential intermediation

Date: 2026-10-02 · Status: ACCEPTED

**Chose:** Recursion must not hold, copy or relay provider credentials it was not explicitly
granted; where possible it delegates a request to a user-authenticated local tool/harness rather
than intermediating login. Driving the user's already-authenticated CLI is the preferred route
over capturing OAuth tokens.

**Because:** third-party credential intermediation is the specific thing provider terms restrict;
delegation to an installed authenticated tool is a materially stronger legal and trust position and
is the cross-harness behavior the product is built for.

**Consequence:** an architecture principle, applied per adapter. Still not legal advice:
per-route terms and automation limits remain counsel-gated (G-32 / D-027).

**Falsifier:** a required route only works via credential intermediation and cannot be delegated.

**Review:** 2026-12-01

## D-022 — One pause/stop vocabulary across config, formal model and broker (G-07)

Date: 2026-10-02 · Status: ACCEPTED

**Chose:** adopt three Stop modes — **Aggressive / Full / Lazy** — plus `checkpoint`, `interrupt`
and `drain`, as the single cross-surface vocabulary in configuration, the formal model and the
broker. A source interrupt, socket close or killed PID is not external rollback.

**Because:** three surfaces currently disagree, and pause/stop is the substrate for revocation,
disconnect and admission.

**Consequence:** needs to be reflected in FU-03's model and the broker contract; G-07 closed.

**Falsifier:** the vocabulary cannot be expressed consistently across all three surfaces.

**Review:** 2026-12-01

## D-023 — First qualified upstream profile

Date: 2026-10-02 · Status: ACCEPTED

**Chose:** qualify **Codex App Server** end-to-end first (strongest structured interface:
steering, correlated approvals/questions, resume, compaction, review). All other harnesses ship as
plugins.

**Because:** a first qualified profile forces the adapter contract to be real; Codex exposes the
richest structured surface to qualify against.

**Consequence:** FU-01/FU-05 route work targets Codex first; a second, deliberately different
adapter is prototyped as an architecture stress test, not a launch promise.

**Falsifier:** Codex's stable/experimental split or account terms prevent a trustworthy qualified
profile.

**Review:** 2026-12-01

## D-024 — Frontier claim: productive continuity

Date: 2026-10-02 · Status: ACCEPTED

**Chose:** the headline measurable differentiator is **productive continuity** — a currently
authorized task survives a harness change, compaction, a source fork or a lost machine while
preserving authority, evidence and honest accounting. The six demonstration journeys from the
2026-10-02 strategy are adopted as release acceptance scenarios.

**Because:** multi-agent dashboards, persistence, plugins, worktrees and attention inboxes already
exist elsewhere; only the combined continuity contract is a candidate advance, and it must beat
strong baselines in verified outcomes and human effort.

**Consequence:** evaluation focuses on continuity endpoints, not feature counts.

**Falsifier:** a strong baseline preserves these semantics with equal or less effort.

**Review:** 2026-12-01

## D-025 — Continuing autonomous research is an optional bounded plugin

Date: 2026-10-02 · Status: ACCEPTED

**Chose:** 24/7 autonomous research is an optional, off-by-default, budgeted plugin with explicit
sources, cadence, stopping conditions and promotion authority. It is not a standing executive and
cannot self-activate or self-approve.

**Because:** matches ANCHOR's "no standing autonomous executive" and keeps the lean base idle-free.

**Consequence:** G-27/G-33 style research work stays optional; lean profiles do no idle inference.

**Falsifier:** it cannot be bounded, or it reproduces a standing executive by another name.

**Review:** 2026-12-01

## D-026 — Recursion's own license and public posture

Date: 2026-10-02 · Status: ACCEPTED (license file still to be added)

**Chose:** release as a community, open-source project under **Apache-2.0** (patent grant +
adoption), with the plugin/capability contract published early to attract porters, and a
contribution/CLA posture to be finalized.

**Because:** the growth vector is community adoption and plugin porting; a permissive,
patent-granting license maximizes both.

**Consequence:** add a LICENSE file and a `plugin` contract doc; this is a distribution decision
that still requires counsel (D-027) before any public release claim.

**Falsifier:** counsel identifies an obligation incompatible with Apache-2.0 distribution.

**Review:** 2026-12-01

## D-027 — Counsel engagement authorized (packet only; no spend)

Date: 2026-10-02 · Status: ACCEPTED

**Chose:** authorize preparation of a counsel-ready packet — per-route terms/ToS, third-party
credential-intermediation limits, Recursion's own licensing, distribution and trademark handling.
Lucas engages counsel; no spending or legal commitment is made by an agent.

**Because:** G-32 / DS-33 make licensing a hard release gate, and the anti-lock-in goal is legally
sensitive the moment the repository is public.

**Consequence:** research packet R-C is commissioned; G-32 stays open until counsel returns.

**Falsifier:** n/a (a process decision).

**Review:** 2026-12-01

---

## D-028 — Platform floors (closes G-11)
Date: 2026-10-02 · Status: ACCEPTED

**Chose:**

- **Windows:** minimum **Windows 11 25H2 (build 26200)** for Home/Pro; additionally qualify
  **24H2 (build 26100) Enterprise**. **x64 primary**; Arm64 a separate later case.
- **Linux:** kernel **6.12 LTS** primary, **6.6 LTS** compatibility minimum; **cgroup v2
  required**; ext4.
- **WSL2:** development/test only, never production; WSL2-ext4 and WSL2 `/mnt/c` are distinct
  mandatory cases.
- **Storage:** canonical state on **local NTFS / local ext4 only**; **SMB/NFS out of scope** for
  the first release.
- **"Survives power loss":** application-consistent after an abrupt **VM reset** at the commit
  boundary, verified by replay reproducing identical canonical state. Physical platter
  interruption is out of scope.
- **Filesystems:** NTFS + ext4; ReFS/xfs/btrfs deferred.

**Because:** these are bounded, testable floors; 24H2 Home/Pro reaches end of updates on
2026-10-13; 6.12 LTS is the RHEL 10 / Debian 13 kernel and 6.6 LTS covers Ubuntu 24.04 and WSL.

**Consequence:** closes G-11. FU-04 pins these floors, maps every manifest case to an in-scope /
dev-test / out-of-scope / later disposition, and keeps every profile **Unqualified** until its
traces pass. Destructive traces still require a disposable, Owner-approved target.

**Falsifier:** a required mechanism fails at the stated floor, or replay diverges after an abrupt
VM reset.

**Review:** 2026-12-01

---

## D-029 — Operator-provisioned trusted launch boundary (strong credential non-intermediation)
Date: 2026-10-02 · Status: ACCEPTED (design research commissioned)

**Options:** A) drive user-authenticated CLIs with policy-level non-intermediation only / B)
invest in an operator-provisioned trusted launch boundary outside Recursion's readable domain so
the strong non-access claim holds.

**Chose:** B. Recursion is a trusted orchestrator the user leans on to define and operate their
own space.

**Because:** same-user processes can still read home, environment, keychain and process memory, so
"drive the CLI" alone yields only policy-level non-intermediation (R-B §6).

**Consequence:** a new architecture requirement and research packet R-E. If the boundary cannot be
met on a profile, label that profile "non-intermediation-by-policy only."

**Falsifier:** the boundary cannot be enforced on a supported platform profile.

**Review:** 2026-12-01

## D-030 — Delegation model: harness-native driving + headless fan-out
Date: 2026-10-02 · Status: ACCEPTED (subject to counsel)

**Chose:** Recursion delegates through the user's own harness/CLI and its native headless/exec
features (e.g. `codex exec`, `claude -p`, `cursor-agent`, ACP), owning the JSON stream of that
output; it does not stand in for the provider. High-volume fan-out is authorized through the
harness's own delegation features inside the user's subscription.

**Because:** the model requests occur through the user's harness and subscription; Recursion
supplies one customizable front and removes ecosystem lock-in.

**Consequence:** research packets R-F (delegation mechanics) and R-H (counsel framing). Treated as
within-ToS pending counsel; no credential custody.

**Falsifier:** counsel finds this indistinguishable from credential intermediation on a required
route, or a harness forbids third-party drivers.

**Review:** 2026-12-01

## D-031 — Product positioning: no vendor-sabotage claim
Date: 2026-10-02 · Status: ACCEPTED

**Chose:** market Recursion on its unrestrictive, customizable environment — one place to connect
keys/subscriptions, routing through any harness or its own, with privacy and safety — not on a
"vendors sabotage quota" claim, which resource measurement cannot establish (R-D).

**Consequence:** PRODUCT/ANCHOR wording updated in the reconciliation; savings claimed only when
measured per workload.

**Falsifier:** n/a (positioning).

**Review:** 2026-12-01

## D-032 — Parent goal is v3; sweep prompt templates
Date: 2026-10-02 · Status: ACCEPTED

**Chose:** create `state/goals/goal-recursion-v3-harness-orchestrator.md` as the active parent
goal; repoint all prompt front matter; sweep retired Hermes-control wording (D-017) from every
prompt template.

**Because:** the outgoing R-A…R-D packet carried stale authority ("Hermes is the executive control
plane", "Hermes constitution remains operative") and a v2 parent goal; the Dot had to correct it.

**Consequence:** prompt suites updated. Broader doc drift (e.g. `ARCHITECTURE.md`) is tracked
separately.

**Falsifier:** n/a (governance hygiene).

**Review:** 2026-12-01

## D-033 — Plugin import/port hypothesis (validate, do not assume)
Date: 2026-10-02 · Status: ACCEPTED (as a hypothesis to test)

**Chose:** treat "porting is cheap" as a hypothesis; commission research into importing/porting
existing plugins (notably DeepSeek Harness / Cordis) and cross-harness plugin reuse.

**Because:** R-A finds porting savings unmeasured and an acceptable-port count of zero possible.

**Consequence:** research packet R-G; the D-019 rationale stands but its porting-leverage
assumption is testable.

**Falsifier:** measured porting cost exceeds a native rewrite, or no ecosystem imports cleanly.

**Review:** 2026-12-01

---

## D-034 — Product thesis: "the harness that scales with your workflow"
Date: 2026-10-02 · Status: ACCEPTED

**Chose:** the product thesis and public tagline is **"Recursion — the harness that scales with your
workflow."** It is a capable baseline on its own (Pi-Agent-class) that grows *deterministically and
without lock-in* as the user adds subscriptions, BYOKs, plugins, configurations, nodes and modding,
composing the effective system prompt and capabilities to fit the workflow. All open source.

**Because:** it names the moat — deterministic capability→behavior composition plus backend
guarantees (authority, continuity, accounting, performance) plus true portability — rather than
table-stakes features (multi-provider routing, plugins, a GUI) that already exist elsewhere.

**Consequence:** the thesis is only true if the composition is reproducible, versioned, cache-aware
and **injection-safe**, and if "scales" is measurable (not "more is always better" — integration
tax is real). GUI and mobile are optional **client plugins** over a secure backend; remote/node
fabric is a **backend capability**, never contingent on a client. New research commissioned:
capability-driven prompt/context composition (extends R-Q/R-L/R-O/R-A/R-W).

**Falsifier:** if adding a plugin/node/configuration degrades quality, latency or authority at a
fixed budget, or the composed prompt is not reproducible, the thesis is wrong for that addition.

**Review:** 2026-12-01

---

## D-035 — Repository topology principle: plugins live outside the core
Date: 2026-10-02 · Status: ACCEPTED (principle); any repository split remains PROPOSED, not authorized

**Options:** A) monorepo with plugins vendored / B) plugins external to the core repo, referenced
by digest / C) split the core product into multiple repositories now.

**Chose:** B as the principle. Plugins are external artifacts; first-party plugins ship in editions
by digest/lockfile. **Keep the core product in this single repository during R&D.** Consider
extracting GUI/mobile only if they grow enough to justify it. **Do not split the core now.**

**Because:** it keeps the core lean and enables a community plugin ecosystem and portability
(R-A/D-019/D-033/D-034). Splitting now would harden an unfrozen kernel boundary, contradict the
single-product framing in `CONTROL_PLANE.md`/`ANCHOR.md`, and multiply version/release drift while
review throughput is the stated constraint (ANCHOR rationing rule). Recursion is still in R&D.

**Consequence:** the proposal, target shape and migration checklist are recorded in
`state/PROPOSALS.md` P-001. Any actual split requires an explicit control-plane authoring session
amending `CONTROL_PLANE.md`/`ANCHOR.md`, recorded here. No repository is renamed or split now.

**Falsifier:** the plugin contract cannot be versioned/verified across repositories, or the core
cannot stay lean with external plugins, or a split is needed to make forward progress.

**Review:** 2026-12-01.

---

## D-036 — Mobile offline retention: online-only private view (O-01)
Date: 2026-10-02 · Status: ACCEPTED

**Options:** A) no private offline redisplay (online-only private view) / B) explicit bounded
offline snapshot policy / C) no mobile offline capability at all.

**Chose:** A. Private views require a live, authenticated backend connection. Only minimal authorized
status metadata may persist; reconnect re-authorizes before any private hydration; there is no
automatic replay of queued actions.

**Because:** remote control already requires a live authenticated connection (R-MOB); the literal
"a cached view never serves after an unseen revocation" is unsatisfiable for a disconnected device;
and Recursion must not claim remote erasure or clone-proofness it cannot deliver. Keeping connection
quality in the transport/tunneling layer lets the content boundary stay simple.

**Consequence:** global **no offline protected redisplay** until a bounded offline policy (option B)
is separately adopted. R-UI/R-MOB fixtures use the online-only rows. Enterprise workflows that
require *private offline content* are explicitly out of scope under this decision and would need a
future bounded-snapshot decision fixing scope, encryption, expiry, audience, storage/backup and
accepting that an offline device cannot learn an unseen revocation.

**Falsifier:** a required enterprise workflow needs private offline content and cannot be served
securely online.

**Review:** 2026-12-01.

---

## D-037 — Conformance fixtures authorized; Python as disposable test harness only (O-02)
Date: 2026-10-02 · Status: ACCEPTED

**Chose:** authorize the provider-free conformance-fixture work (R-PC F01–F18, R-UI UI-01…UI-20 plus
the C×V soak, R-MOB MOB-F01–F24 plus the two-path simulation) to be implemented in an already
installed runtime (**Python**) functioning **only** as a disposable test harness. The product
implementation language remains unselected (FU-01).

**Because:** the fixtures require executing code, but selecting the product language from a research
recommendation is disallowed; Python is installed and adequate for deterministic, provider-free
harnesses.

**Consequence:** the standards in the conformance intakes apply (a valid positive control, an adverse
mutation, and an independent oracle that does not trust candidate flags). The Owner intends to move
to a more optimized language later; that intent is **not** a selection and does not bind FU-01.

**Falsifier:** the Python harness cannot express a required check, or a fixture can pass without an
independent oracle.

**Review:** 2026-12-01.

---

## D-038 — D-034/D-019 supersede older first-release GUI/mobile exclusions (O-03)
Date: 2026-10-02 · Status: ACCEPTED

**Chose:** confirm that D-034 and D-019 supersede any older "GUI/mobile later" first-release
exclusions. GUI and mobile are **built but opt-in**; nothing accepted is removed, and compatibility
across the whole product scope is a goal.

**Because:** R-UI flagged a release-timing conflict between older first-release prose and D-019's
comprehensive opt-in packaging; leaving it ambiguous re-opens at every UI/mobile task.

**Consequence:** future UI/mobile tasks treat GUI and mobile as in-scope optional clients over the
backend; no capability is deferred by the old exclusion wording.

**Falsifier:** n/a (governance clarity).

**Review:** 2026-12-01.

---

## D-039 — Accept the OS/language expansion as advisory; plan a language-agnostic architecture (O-04)
Date: 2026-10-02 · Status: ACCEPTED

**Chose:** accept the delivered iOS/Android advisory packet (intake
`docs/v3/research-lane/ios-android-20261002/`) as research evidence, and commission a **planned,
language-agnostic architecture** for the core and clients so that a later language change can
reconstruct the architecture with bounded cost (supports D-033/D-035).

**Because:** the Owner wants the architecture planned for the future and reconstructable across
languages; report completeness is not adoption of the recommended stacks.

**Consequence:** no language, toolkit, renderer, store, transport, auth or push provider is selected;
the Swift/SwiftUI and Kotlin/Compose recommendations stay advisory; the R-MOB `MOB-F01…F24` and the
shared `MP01…MP11` fixture namespaces reconcile into one suite. A language-agnostic architecture
planning deliverable is commissioned.

**Falsifier:** a future language change cannot be reconstructed from the plan without re-deriving
the architecture.

**Review:** 2026-12-01.

---

## D-040 — Review branches use `csl-<N>-<slug>`, not `loop/work`
Date: 2026-10-15 · Status: ACCEPTED

**Options considered:** A) keep `loop/work/<task-id>` in `CONTROL_PLANE.md` invariant 1 /
B) name review branches `csl-<N>-<slug>`, with an optional GitHub username prefix, and
satisfy pr-lint with `CSL-N` in the PR title.

**Chose:** B. Lucas's owner decision on PR #189 (2026-10-15) adopts P-002: review
branches are `csl-<N>-<slug>`, optionally prefixed by a GitHub username, and PR titles
keep uppercase `CSL-N`. Force-push, merge, and deployment limits stay as they were.

**Because:** `loop/` branch names belong to the retired recursion release. The current
harness keys work to Linear issues. pr-lint matches `CSL-[0-9]+` case-sensitively, so a
lowercase branch name alone does not pass. The `CSL-N` key in the PR title satisfies the
check.

**Consequence:** invariant 1 no longer names `loop/work/<task-id>`. The operating docs
already use `csl-<N>-<slug>`. Workflows were not edited in this decision.

**Falsifier:** pr-lint or branch protection requires a `loop/` prefix, or a lowercase
`csl-<N>-<slug>` branch plus a `CSL-N` title fails the Linear-key check.

**Review:** 2026-12-01.

---

## D-041 - Founder authority split and bounded governance authoring
Date: 2026-10-16 - Status: ACCEPTED authority direction; repository amendment pending review and merge

**Source records:** Lucas's 2026-10-16 22:55 UTC confirmation is recorded as an attributed
agent relay on [CSL-45](https://linear.app/csl-laboratories/issue/CSL-45/list-planned-features-and-research-items-for-recursion#comment-59f88559)
(comment `59f88559-8967-46a6-9be5-d3e9f3499e0a`, 22:57:52 UTC) and
[CSL-44](https://linear.app/csl-laboratories/issue/CSL-44#comment-bbe42521)
(comment `bbe42521-10fa-4ef0-b799-1c56fec9ddad`, 22:58:08 UTC). These are agent-authored
records of his confirmation, not personally typed founder comments. They bind month plan
rev 3.4, SHA256 `3071ef2592dbef82e63b3173c030d975a55b093c7e95cff1b499d70319b744c8`.

**Authoring-session approval:** In parent task `01a0fe67-c17a-7798-8cb4-a7c1a86710d7`, the
assistant asked: "The next governance step needs the explicit authoring session required
by your contract: may I use one writer on a new branch to amend CONTROL_PLANE.md to the
authority split you approved and append the matching entry to state/DECISIONS.md? This
would be a reviewable PR, with no repository permission/settings changes or merge."
Lucas replied: "Yes absolutely? You have my complete authorization. Let’s get everything
landed." The session is recorded on
[CSL-45](https://linear.app/csl-laboratories/issue/CSL-45#comment-1aca1256)
(comment `1aca1256-3e03-4eb2-848f-3a0b98f80d15`). The delegated scope explicitly retains
no merge by this writer; the reply is not used as an agent-merge exception.

**Options and choice:** Replace Lucas-only product/implementation approval with decisions
by any founder within the approved split; retain Lucas's reserved decisions and final
tie-break. Reject a blanket prohibition on agents executing individually approved live
actions, but require the recorded authorization and landed R2a gate below. This records
the confirmed choice, not an agent-selected alternative or a new implementation contract.

**Prior rationale preserved:** D-017 records the 2026-09-28 single-product reset: the former
Hermes-led portfolio control plane was retired because it supplied no v3 dependency and
version coexistence caused contributor-blocking drift. Its consequence placed the repository
under Lucas alone, with bounded coding agents and no standing executive. D-040 later changed
only branch naming and explicitly retained the then-current merge/deployment limits. Those
entries and their reasons remain unchanged. This amendment supersedes their Lucas-only
governance consequence where it conflicts with the newly confirmed authority split; it
does not reverse the single-product scope, retirement, or bounded-executor model.

**Confirmed authority and conditions:**
- Any founder (Lucas Guerra, Chaandan Saini, Samir) may decide product and
  implementation questions from research, research-lane outcomes, feature admission within
  a lane, or research continuation within its approved budget. A1b core language/runtime
  choice belongs to the founders; no language is selected. All three make the final release
  decision.
- Founders merge after required review and checks. Agents recommend and propose; they do
  not decide, merge, force-push, or fabricate founder decisions. Any founder may approve
  protected-path or merge-rule amendments through a recorded authoring session.
- Lucas reserves spending/new services, sensitive company credentials including company
  API-key generation, company-server deployments including Railway, and the final tie-break.
  Approved research continuation cannot raise its budget or delegate those reserved powers.
- Developers own their individual live actions, accounts, keys and tests. Agent execution
  requires the developer's recorded route/account/spend-ceiling approval, uses their own
  accounts/keys, and is blocked until the R2a permission-profile fix has landed. Company
  credentials/deployments still need Lucas's approval. Building is not operating permission;
  this amendment grants no specific live run, spending, credential use, deployment, quota
  reset or destructive action.
- Decision packets retain options, evidence classes, tradeoffs, recommendation, strongest
  countercase, reversibility, research cost and deadline. Consequential founder records retain
  the chosen option, rationale, conditions and revisit trigger.
- The companion project's paper-only restriction, nine invariants and pre-live gates remain
  unchanged, as recorded on CSL-44. No person-level
  approval unlocks live capital. Accepted CSL-212 bytes and receipts are untouched; a later
  amendment requires its separate versioned candidate, exact acceptance and reviewed promotion.

**Session boundary and consequence:** Codex Sentry is the sole writer on
`csl-45-governance-authoring`, based on #221 head
`41c50a6e09a3ceacbd9ba2cee6a16f0e9ffa4fa0`. Only `CONTROL_PLANE.md` and this appended entry
change. Historical decision bytes are preserved. Cross-family review and independent
exact-candidate verification precede draft publication. AGENTS/CLAUDE/CONTRIBUTING alignment
remains a coordinated follow-on; no settings, CODEOWNERS, rulesets, grants, credentials,
live actions, queue activation, other-owner branch writes, rebase, force push or merge occurs.
This is not Phase 1 completion, an unknown contract-digest acceptance or a release decision.

**Open items preserved:** A1b language selection, A2 CSL-42 policy, A4 economics, A5 GUI
toolkit, D1 enterprise fabric/provider terms, D2b account import, D3 Kalshi path, E2
co-developer Cursor capacity, P2 CI envelope/cadence, P3/P4 budget setters and claimants,
B5 queue activation, the proposed Oct 27 admission cut, and bake-off fallback remain open.
R2a/B6 still require their bounded contracts and applicable gates; this entry does not
authorize their implementation or claim either has landed.

**Falsifier / revisit trigger:** a founder decision is reversed or disputed more than once
in a week, or a spend, credential or deployment boundary is crossed without authorization.
Record the event and seek a founder decision; no automatic policy expansion follows.

**Review:** at the recorded revisit trigger or the next monthly decision-log review.
