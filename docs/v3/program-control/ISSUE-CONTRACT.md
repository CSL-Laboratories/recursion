# Candidate: Recursion research-driven issue contract

**Purpose:** preserve product direction while allowing developers and agents to claim and execute issues in parallel.  
**Status:** CSL-204 review candidate. It is non-normative until a human accepts its exact digest.

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

## Human decision sovereignty

Consequential decisions are made by an identified human, never inferred from agent output, model consensus, a merged prototype or an issue status change.

- **Agents may:** collect evidence, expose contradictions, compare options, propose contracts, implement bounded experiments and recommend a choice.
- **Agents may not:** declare their recommendation accepted, manufacture approval language, select a product/architecture boundary, silently treat implementation as precedent, or unblock dependent work.
- **Human decision-maker:** Lucas, or a human approver Lucas explicitly delegates for the named decision scope. Assignment to an agent, bot or model is not delegation of decision authority.
- **Bounded engineering discretion:** an issue may explicitly delegate reversible implementation details within an accepted contract. That discretion does not include product intent, public semantics, authority, privacy, spending, credentials, compatibility, release scope or irreversible architecture.

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

An agent may draft this packet, but only the human's explicit confirmation converts it into a decision. Reactions, silence, meeting attendance, prototype success, branch creation, PR approval and merge do not substitute for the receipt unless the governing contract explicitly defines that exact action as approval.

### Decision gate behavior

- Before a decision: label the issue `needs-decision`; research and bounded prototypes may continue only within their approved non-production boundary.
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
7. **Dependencies:** issues, artifacts and decisions required before start or freeze.
8. **Authority boundary:** permitted changes/effects and actions requiring owner approval.
9. **Decision triggers:** findings that must return to Lucas rather than be resolved by an agent.
10. **Falsifiers:** observable results that reject the proposed approach or invalidate the task.
11. **Acceptance gate:** deterministic or machine-checkable checks on the exact candidate.
12. **Independent verification:** who or what reruns the candidate, preferably with a different model family.
13. **Evidence output:** artifacts, raw results, limitations and provenance retained for dependants.
14. **Follow-up rule:** new discoveries become deduplicated linked issues, not drive-by scope expansion.

## Definition of Ready

An issue may enter development only when:

- the parent contract and required dependencies are accepted or explicitly version-pinned;
- overlap has been checked across the CSL team, project and linked PRs;
- consequential research and owner decisions are closed, or named unknowns are within the allowed experiment boundary;
- allowed paths/effects, non-goals, product ceilings and testable acceptance are explicit;
- fixtures and falsifiers exist before implementation where the behavior is load-bearing;
- one claimant is accountable while active;
- integration and independent-verification capacity are reserved.

If two materially different implementations would satisfy the written issue but create incompatible product behavior, the issue is not ready.

## Lifecycle gates

1. **Intake:** deduplicate, identify the parent outcome, classify evidence and expose contradictions.
2. **Research:** search current facts; use Deep Research only at the consequential decision boundary.
3. **Decision:** present options and falsifiers; Lucas retains product and architecture authority.
4. **Human receipt:** record explicit human approval against the exact proposal/contract digest; otherwise dependants remain blocked.
5. **Contract freeze:** pin schemas, semantics, fixtures, host profiles and artifact identities needed by parallel children.
6. **Develop:** implement only the claimed boundary; record discoveries as linked issues.
7. **Test:** run focused checks, falsifier cases and exact-candidate acceptance; distinguish fixture, local qualification and release evidence.
8. **Iterate:** repair failed acceptance. Return to research/decision if evidence changes scope or invalidates the contract.
9. **Verify:** an independent verifier reruns the exact artifact and reconstructs the result from retained evidence.
10. **Review:** bounded candidate enters Devin review, Claude fixing, current-head Blacksmith CI, then Codex mediation/gate under repository rules.
11. **Close:** retain the accepted result, limitations and dependency links; open deduplicated follow-ups for unfinished work.

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

## Authority boundary and owner-decision triggers

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
