# Candidate: Recursion research-driven issue contract

**Purpose:** preserve product direction while allowing developers and agents to claim and execute issues in parallel.
**Status:** Revision 2 candidate under CSL-258, following CSL-204. It is non-normative until an authorized human accepts its exact digest.

This revision aligns decision-maker wording with CONTROL_PLANE and D-041 only. [Revision 1](ISSUE-CONTRACT.md) remains byte-preserved historical evidence; its Lucas-only decision-maker clauses are superseded by the higher-priority CONTROL_PLANE. Receipt-channel requirements, admission caps and lifecycle sequencing are not migrated by this revision. Neither authoring approval nor a structural checker pass accepts this candidate.

## Core rule

Every issue is a bounded claim against a named parent outcome. It may leave implementation choices to its claimant, but it may not invent product intent, authority, public contracts, evidence standards or release claims.

An issue is not development-ready because it has a plausible task description. It is ready when another developer can determine what outcome it advances, what decisions constrain it, what evidence it inherits, what it may change, what would prove it wrong, and how an independent verifier can determine completion.

## Research proportionality

Every issue performs a research check. Not every issue commissions a new Deep Research run.

| Issue condition | Required action |
| --- | --- |
| Current public fact, API, standard, version, license or competitor capability | Use Parallel Search/Fetch; use Context7 for current library/API details when available. Pin source and access/version date. |
| Consequential product, architecture, authority, privacy, spending, compatibility or irreversible choice | Run a bounded Parallel Deep Research brief before recommending a direction. Compare credible options, alternatives, failure modes, falsifiers and smallest experiments. |
| Existing accepted research directly covers the question | Cite the pinned research, verify its assumptions against the current parent contract, and search only for material deltas. Do not rerun research for ceremony. |
| Mechanical implementation under a frozen contract | Inherit the parent evidence packet and check dependency/library deltas. New Deep Research is unnecessary unless implementation exposes a consequential unknown. |
| Claim requiring private repository or runtime evidence | Use local inspection and deterministic experiments. Do not send private source, transcripts, credentials or infrastructure details to public research. |
| Multiple independent questions | Freeze one shared parent brief, split bounded research children, and reserve a synthesis issue. Do not let children choose incompatible premises. |

Research must preserve source-documented facts, locally observed evidence, inference and unknowns as separate classes. Model agreement is not verification.

### Research authorization and continuation

An issue's accepted research plan authorizes its initial bounded evidence pass. It must state the question, source class, expected quota/cost class, stop condition, and artifact to produce. Agents may execute and synthesize that approved pass without step-by-step permission.

A founder acting within the authority below must approve continuing or expanding the lane when the initial pass reaches its stop condition, when a new Deep Research run or materially broader search would consume additional Parallel quota, or when the proposed next question changes the product direction being investigated. The continuation packet includes evidence already obtained, remaining uncertainty, expected quota class, options, recommendation, and the smallest next pass. Research continuation within an approved budget does not raise that budget; additional spending remains reserved to Lucas. Repeated queries, model curiosity, or an unfinished recommendation are not implicit authorization.

The authoritative approval receipt is a comment by the approving human on the matching Linear issue. It identifies the approver, exact research pass or recommendation digest, approved scope and quota class, date, conditions, and next stop trigger. Agents may draft the comment for review, but may not post it on the human's behalf. Labels and issue status are routing signals only; moving an issue from `needs-decision` or changing its status does not grant approval.

## Human decision sovereignty

Consequential decisions are made by an identified human, never inferred from agent output, model consensus, a merged prototype or an issue status change.

- **Agents may:** collect evidence, expose contradictions, compare options, propose contracts, implement bounded experiments and recommend a choice.
- **Agents may not:** declare their recommendation accepted, manufacture approval language, select a product/architecture boundary, silently treat implementation as precedent, or unblock dependent work.
- **Human decision-maker:** the founder or individual developer authorized for the exact decision scope by CONTROL_PLANE, including its Lucas-reserved and all-founder release boundaries below. Assignment to an agent, bot or model is not delegation of decision authority.
- **Bounded engineering discretion:** an issue may explicitly delegate reversible implementation details within an accepted contract. That discretion does not include product intent, public semantics, authority, privacy, spending, credentials, compatibility, release scope or irreversible architecture.

[CONTROL_PLANE.md](../../../CONTROL_PLANE.md#roles) and D-041 define the authority boundary. Any founder may decide product and implementation questions, accept research-lane outcomes, admit features within a lane, and approve research continuation within its approved budget. All three founders make the final release decision; founders merge after required review and checks. Lucas retains spending and new services, sensitive company credentials (including company API-key generation), company-server deployments, and the final tie-break. Another founder's approval cannot grant those reserved powers or raise a budget.

Each developer owns their individual live actions, accounts, API keys and tests. An agent may execute a developer-approved live action only on that developer's own accounts/keys, with the route, account and spend ceiling recorded, and only after the R2a permission-profile fix has landed. Individual approval does not override Lucas's company-resource or spending approvals, authorize live capital, or grant an agent merge permission. Destructive actions outside that individual scope still require Lucas's explicit approval. This candidate grants no specific live action or acceptance of another contract.

### Symbiotic execution model

Human sovereignty is a boundary, not a requirement for humans to direct every step. The default is agent initiative inside an accepted envelope and human judgment at consequential thresholds.

- **Bounded autonomy:** agents and developers may independently choose reversible implementation details, local refactors, test strategy, research tactics, prototype structure, failure repair, and linked follow-up issues when the parent outcome, accepted contracts, allowed effects, budgets, and acceptance gate remain unchanged.
- **Collaborative judgment:** when several reversible choices could materially affect maintainability or integration, the agent produces evidence and a recommendation for the active claimant. Work may continue on non-conflicting experiments while that choice is discussed.
- **Human gate:** explicit human confirmation is required for product decisions, approval of recommendations into the product contract, research continuation beyond the accepted issue budget, or choices that change public semantics, authority, privacy, credentials, spending, compatibility commitments, release scope, destructive external state, or an architecture that is costly to reverse.
- **Narrow blocking:** a pending human decision blocks only the affected claim and dependants. Unrelated research, tests, prototypes, and work under already accepted contracts continue.
- **Escalation packet:** agents do not merely stop. They reduce the question to exact options, evidence, recommendation, falsifiers, affected scope, and the smallest decision needed.

Routine engineering choices are recorded in the issue or PR with their evidence and remain reviewable; they do not require a human decision receipt. Research execution within an approved pass is routine, but research continuation is not. If a routine choice crosses a listed threshold, its discretion class changes before the consequential effect occurs.

### Required decision receipt

Every accepted consequential decision must have a durable human-authored or human-confirmed receipt containing:

1. decision identifier and parent outcome;
2. human decision-maker and the authority/delegation basis;
3. exact proposal and contract revision/digest reviewed;
4. selected option and materially rejected alternatives;
5. concise rationale tied to the evidence packet;
6. unresolved risks, unknowns and imposed conditions;
7. decision scope and downstream issues authorized to inherit it;
8. expiry, revisit trigger or explicit statement that none is set;
9. explicit approval language and approval date.

An agent may draft this packet, but only the human's explicit comment on the matching Linear issue converts it into a decision. The receipt pins the comment identity so a status change cannot be mistaken for approval. Reactions, silence, meeting attendance, prototype success, branch creation, PR approval and merge do not substitute for the receipt unless the governing contract explicitly defines that exact action as approval.

### Decision gate behavior

- Before a decision: label only the affected scope `needs-decision`; research, tests, bounded prototypes, and unrelated implementation continue inside accepted boundaries.
- At the gate: present the evidence packet and exact choices to the human; do not ask for approval of an unspecified future implementation.
- After approval: pin the receipt identity in every dependent issue. Children inherit it without repeatedly asking the same question.
- On material evidence or scope change: invalidate inheritance, return dependants to blocked, and request a revised human receipt.
- Automation may check that a valid receipt exists and matches the pinned digest. Automation may not create, approve or reinterpret the receipt.

## Required issue fields

1. **Parent outcome:** goal, parent issue and accepted contract this work advances.
2. **Current state:** verified repository/Linear baseline and overlap search.
3. **Intended outcome:** observable capability or decision produced.
4. **Non-goals:** adjacent work and claims explicitly excluded.
5. **Inherited decisions:** owner decisions and contracts that may not be reopened silently.
6. **Research basis:** pinned repository evidence, Parallel/Context7 sources and unresolved unknowns.
7. **Research budget:** approved question, quota/cost class, stop condition, and continuation trigger.
8. **Dependencies:** issues, artifacts and decisions required before start or freeze.
9. **Authority boundary:** permitted changes/effects and actions requiring owner approval.
10. **Discretion class and decision triggers:** bounded-autonomy, collaborative-judgment, or human-gated work, plus findings that change the class.
11. **Falsifiers:** observable results that reject the proposed approach or invalidate the task.
12. **Acceptance gate:** deterministic or machine-checkable checks on the exact candidate.
13. **Independent verification:** who or what reruns the candidate, preferably with a different model family.
14. **Evidence output:** artifacts, raw results, limitations and provenance retained for dependants.
15. **Follow-up rule:** new discoveries become deduplicated linked issues, not drive-by scope expansion.

## Definition of Ready

An issue may enter development only when:

- the parent contract and required dependencies are accepted or explicitly version-pinned;
- overlap has been checked across the CSL team, project and linked PRs;
- consequential decisions required for this exact scope are closed, or named unknowns are within the allowed experiment boundary;
- allowed paths/effects, non-goals, product ceilings and testable acceptance are explicit;
- fixtures and falsifiers exist before implementation where the behavior is load-bearing;
- one claimant is accountable while active;
- integration and independent-verification capacity are reserved.

If two materially different implementations would satisfy the written issue but create incompatible product behavior, the issue is not ready.

## Lifecycle gates

1. **Intake:** deduplicate, identify the parent outcome, classify evidence and expose contradictions.
2. **Research:** search current facts; use Deep Research only at the consequential decision boundary.
3. **Classify discretion:** proceed autonomously for bounded choices, collaborate with the claimant for material reversible choices, and enter a human gate only at a consequential trigger.
4. **Decision when triggered:** present options and falsifiers to the human authorized for that scope under CONTROL_PLANE and the authority boundary above; agents cannot supply that decision.
5. **Human receipt when triggered:** record explicit human approval against the exact proposal/contract digest; otherwise only affected dependants remain blocked.
6. **Contract freeze:** pin schemas, semantics, fixtures, host profiles and artifact identities needed by parallel children.
7. **Develop:** implement only the claimed boundary; record discoveries as linked issues.
8. **Test:** run focused checks, falsifier cases and exact-candidate acceptance; distinguish fixture, local qualification and release evidence.
9. **Iterate:** repair failed acceptance. Return to research/decision if evidence changes scope or invalidates the contract.
10. **Verify:** an independent verifier reruns the exact artifact and reconstructs the result from retained evidence.
11. **Review:** bounded candidate enters Devin review, Claude fixing, current-head Blacksmith CI, then Codex mediation/gate under repository rules.
12. **Close:** retain the accepted result, limitations and dependency links; open deduplicated follow-ups for unfinished work.

No new push inherits acceptance from an older head. Human approval and merge remain required.

## Parallel issue pattern

Parallel work is appropriate when children consume the same frozen interface and do not write the same authority surface.

- **Parent contract issue:** owns outcome, shared brief, invariant set, schemas and integration checkpoint.
- **Research children:** investigate independent discriminators using the same evidence taxonomy.
- **Prototype children:** implement matched experimental arms, not premature product forks.
- **Synthesis issue:** reconciles raw results, contradictions and unknowns without averaging away hard failures.
- **Owner-decision issue:** records the selected boundary and rejected alternatives.
- **Development children:** implement non-overlapping accepted slices.
- **Integration issue:** proves composed behavior and blocks release claims until closure.

Parallelism stops at unstable shared contracts. Research may fan out before freeze; production implementations may fan out only after their common interface is pinned.

## Issue body template

```markdown
## Parent outcome

## Current verified state and overlap check

## Intended outcome

## Non-goals

## Inherited decisions and contracts

## Research basis
- Repository evidence:
- Parallel Search / Deep Research:
- Context7:
- Local evidence:
- Unknowns:

## Dependencies and integration checkpoint

## Research budget and continuation
- Approved question and source class:
- Expected quota/cost class:
- Stop condition and evidence artifact:
- Continuation trigger and human approval comment:

## Authority boundary and owner-decision triggers

## Discretion class
- bounded_autonomy / collaborative_judgment / human_gated:
- Findings that change this class:

## Required human decision receipt(s)
- Decision ID:
- Human decision-maker:
- Delegated scope (if applicable):
- Proposal/contract digest:
- Selected/rejected options:
- Rationale and conditions:
- Approval evidence/date:
- Revisit trigger:

## Proposed work boundary

## Falsifiers

## Acceptance gate

## Independent verification

## Required evidence output

## Follow-up and deduplication rule
```

## Drift indicators

Stop and return to the parent/owner decision when:

- a child changes public semantics, authority, persistence, compatibility or release scope;
- implementation selects a previously unselected language, store, framework, provider, sandbox or protocol;
- a benchmark changes fixtures, durability, host occupancy or accounting boundaries between candidates;
- a passing synthetic fixture is being presented as live qualification or product acceptance;
- missing usage, evidence or support is converted to zero or pass;
- downstream issues disagree on identity, lifecycle, refusal, capability or configuration semantics;
- an agent recommendation, prototype, merge or stale receipt is being treated as a current human decision;
- generated work grows faster than independent review and integration capacity.

## Completion gate for this contract

Before applying this workflow to the backlog, validate it against R1, the engine/runtime comparison, OS specialization, one provider integration, one interface issue and one privacy/security issue. A second reviewer must be able to classify each issue's research requirement and readiness identically from the written rules. Ambiguity becomes a proposed clarification, not an implicit exception.
