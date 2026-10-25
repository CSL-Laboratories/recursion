---
title: Recursion spec-acceptance rule (founder GitHub PR approval)
status: proposed (2026-10-25); blocked on Lucas authoring session
issue: CSL-524
twin: CSL-523
base: 954ddc607f6b8736a09ebf5aed872fce8355ce3e
---

# Proposed D-044 — founder GitHub PR approval as Recursion spec acceptance

**This file is a proposal.** It is not acceptance, not an authoring session, and
not permission to edit protected paths. `CONTROL_PLANE.md` rule 6: protected
paths change only in an explicitly founder-approved authoring session recorded
by Lucas himself. An agent relay does not count.

Lucas (2026-10-25 12:13am PT): "Yes absolutely" to drafting Recursion's
acceptance follow-up, matching CSL-523. Separate from recursion#295.

## Rule (Lucas, 2026-10-24 11:58pm PT, his words)

> Samir and Dan should be able to change and modify the product spec with their
> agents as recommended and as research is synthesized

> Spec changes by Samir or Dan are accepted once another founder approves the PR.

## Binding

| Field | Value |
|---|---|
| Allowlist (founders) | Lucas (`NotSkittlz`), Samir (`samir-shah-ahmed`), Dan/Chaandan (`CDanTheMan`) |
| Approver | must be a founder **and** must not be the PR author |
| Author | GitHub `user.login` of the PR; if an agent posted through a founder account, the attributed `Agent (…, for <developer>)` principal |
| Head | full 40-hex SHA that is the PR head at the moment of acceptance |
| Files | SHA-256 of each accepted path, recorded in the approving review or a receipt the review names |
| Lucas-authored spec PRs | Lucas's existing path stays valid (his own unprefixed Linear or GitHub statement on the exact candidate) |
| Not a receipt | merge, labels, status, reactions, silence, agent comments, tests, CI, implementer's PASS, non-founder review, author self-approval, stale-head approval, digest mismatch |

Do not open a Linear-comment receipt path for Samir or Dan. Agents never merge.
Lucas's reserved decisions (spend, company credentials, company-server deploy,
tie-break) are unchanged. Protected-path / merge-rule amendments still need a
recorded authoring session.

## Why this file exists instead of editing the targets

`CONTROL_PLANE.md` Protected paths: a task that touches them without the matching
authoring-session approval fails its gate. Historical pattern (D-041, D-043):
Lucas posts the approval himself, then the review branch applies the files.

Until that comment exists on CSL-524, this PR must not edit
`CONTROL_PLANE.md`, `ANCHOR.md`, or `state/DECISIONS.md`. `AGENTS.md` and
`CLAUDE.md` are not on the CONTROL_PLANE protected list, but applying them
without the control-plane text would contradict live governance, so they stay
in this proposal too.

## Current-file digests at `954ddc607f6b8736a09ebf5aed872fce8355ce3e`

These are the live bytes this proposal would amend. A later apply-step must
re-hash the then-current files and bind the accepting review to **that** head.

| Path | SHA-256 |
|---|---|
| `CONTROL_PLANE.md` | `73ec2f799280e2167461e7f3de6172e6fe54456ea2e7955d7832bcf03dc8ab45` |
| `ANCHOR.md` | `0975e9151c1bafa3bd67d66fed061078b1eb75e6b808c65a7f4bfe851fc8b0e5` |
| `state/DECISIONS.md` | `1a8c092085cbe1ecf5d69fc72dbde349686d7648562d7f62e2214ec41c6e1002` |
| `AGENTS.md` | `310e6743e479ce30c945f25fb7129728b31eaccde40ae07a37a13a765e485eab` |
| `CLAUDE.md` | `310e6743e479ce30c945f25fb7129728b31eaccde40ae07a37a13a765e485eab` (byte-identical to `AGENTS.md` on this base) |
| `docs/v3/PLANNING.md` | `b83aff4256e4bd5b44951347c27b87312f9fa59f53897d88fb850be61aefc488` |
| `docs/v3/AUTONOMOUS-PLANNING.md` | `f894da5bc3e4e898a16c61e859f5464c614b3ea2b9a4bfb32781c5792d9cfa41` |
| `docs/v3/DECISION-REGISTER.md` | `3344cbd8aa603753d9f71f596a7ab773dba5201cec7ee41e9671e3331d08221f` |
| `docs/v3/ACCEPTANCE.md` | `1235beeb5bdecc6caadd07b777df22f58d4574ce70a2c1bfe2e3bab6ed7bc0a9` |

## Exact sentence Lucas must post on CSL-524

Post from your own account, **unprefixed**. An agent-prefixed or
developer-approved relay does not count for this contract. Copy this entire
block as the comment body:

```
I approve a recursion protected-path authoring session for CSL-524 (review branch cursor/csl-524-spec-acceptance-1752; files CONTROL_PLANE.md, ANCHOR.md, state/DECISIONS.md, AGENTS.md, CLAUDE.md, docs/v3/PLANNING.md, docs/v3/AUTONOMOUS-PLANNING.md, docs/v3/DECISION-REGISTER.md, docs/v3/ACCEPTANCE.md). Spec changes by Samir or Dan (or their attributed agents) are accepted once another founder approves the PR, bound to that head SHA and exact file digests; Lucas's existing acceptance path stays valid. Nothing takes effect until the authoring PR merges. I admit CSL-524.
```

## Proposed insertions (apply only after that comment)

GoalSpec (runtime object) is **not** the product spec. This rule accepts
`docs/v3` amendments. GoalSpec digest acceptance stays Owner-authenticated;
CSL's first-user Owner remains Lucas until a GoalSpec names another founder.
Item 9 of the M1A list is split accordingly. Merge/deploy of the initial
private deployment stays Lucas's reserved seam.

### 1. `CONTROL_PLANE.md` — after "Founders merge review branches…" (Roles / Founders)

Insert a new subsection immediately after the paragraph that ends
"Approval does not apply repository settings or bypass existing review
requirements.":

```
### Product-spec acceptance

Lucas (2026-10-24 11:58pm PT): Samir and Dan may change and modify the product
spec with their agents as recommended and as research is synthesized. Spec
changes by Samir or Dan are accepted once another founder approves the PR.

Founders for this receipt: Lucas (`NotSkittlz`), Samir (`samir-shah-ahmed`),
Dan/Chaandan (`CDanTheMan`). The approver must be a founder and must not be the
PR author (GitHub author login, or the attributed `for <developer>` principal
when an agent authored through a founder account). The author's own approval
does not count.

The accepting GitHub review is `APPROVED` on the exact head SHA (40-hex) that
is the PR head at the moment of acceptance, and it names a SHA-256 digest map
of every accepted path. A review on a stale SHA, a digest mismatch, a
non-founder review, merge, labels, CI, an implementer's PASS, silence, a
reaction, or an agent comment is not acceptance.

Lucas-authored spec PRs keep Lucas's existing acceptance path (his own
unprefixed Linear or GitHub statement on the exact candidate). Another
founder's GitHub approval of a Lucas-authored spec PR is not required and does
not replace Lucas's path. Agents never merge.

Protected-path and merge-rule amendments still require the separately recorded
authoring session. This receipt does not change Lucas's reserved decisions.
```

Also record D-044 in the "Changing this document" closing list, next to D-041
and D-043, once that entry exists.

### 2. `ANCHOR.md` — new short section after Success criteria

Do **not** edit the "terminal first, GUI later" sentence (no authoring session
for GUI wording; that supersession lives in the dated 2026-10-24 amendment).

```
## Product-spec acceptance

`docs/v3` accepted text changes only by a dated amendment that is accepted.
Spec changes by Samir or Dan (or their attributed agents) are accepted when a
founder other than the author approves the PR on the exact head SHA with a
file-digest map (`CONTROL_PLANE.md` product-spec acceptance). Lucas-authored
amendments keep Lucas's existing acceptance path. Merge is not acceptance.
```

### 3. `state/DECISIONS.md` — append D-044 (do not edit earlier entries)

```
## D-044 - Founder GitHub PR approval accepts Samir/Dan spec changes
Date: 2026-10-25 (decision) - Status: ACCEPTED founder decision; repository
amendment pending review and merge

**Source record:** Lucas (2026-10-24 11:58pm PT), his words: "Samir and Dan
should be able to change and modify the product spec with their agents as
recommended and as research is synthesized" and "Spec changes by Samir or Dan
are accepted once another founder approves the PR." Drafting this follow-up
was directed 2026-10-25 12:13am PT ("Yes absolutely"), matching CSL-523.
Authoring-session approval is the unprefixed comment Lucas posts on CSL-524.

**Authoring-session approval:** that CSL-524 comment. Lucas is a founder, and
`CONTROL_PLANE.md` lets any founder approve a protected-path amendment through
this process.

**Options considered:** A) keep product-spec acceptance Lucas-only, including
for Samir/Dan-authored PRs / B) count another founder's GitHub `APPROVED`
review of a Samir- or Dan-authored spec PR as acceptance, bound to head SHA
and file digests, keeping Lucas's path for Lucas-authored PRs.

**Chose:** B.

**Because:** the 11:58pm PT instruction is explicit; a GitHub review from the
approver's own account is already the named-reviewer receipt
(`CONTROL_PLANE.md` Protected paths). Binding SHA and digests prevents a
stale-head or swapped-file approval from counting.

**Consequence:** `CONTROL_PLANE.md` gains the Product-spec acceptance
subsection. `AGENTS.md`/`CLAUDE.md` record that merge is still not
acceptance, and that the bound GitHub approval is. GoalSpec digest
acceptance (runtime Owner object) is not this rule.

Unchanged: merge rules; agents never merge; Lucas's reserved authority; R2a;
accepted contracts; D-038; OR-038.

**Falsifier:** a non-founder approval, author self-approval, stale head, or
digest mismatch is treated as spec acceptance; or Lucas's path for
Lucas-authored spec PRs is removed.

**Review:** at a founder revisit of this decision, or the next monthly
decision-log review.
```

### 4. `AGENTS.md` and `CLAUDE.md` (keep them in lockstep)

In "Bounded issue execution and shared evidence", after:

> A founder merge records its merge decision; it does not itself accept a
> product fragment or release an unrelated decision hold.

insert:

```
 Spec changes authored by Samir or Dan (or their attributed agents) are
 accepted when a founder other than the author submits a GitHub `APPROVED`
 review on the exact PR head SHA with a SHA-256 digest map of the accepted
 paths (`CONTROL_PLANE.md` product-spec acceptance; D-044). Lucas-authored
 spec PRs keep Lucas's existing acceptance path. Self-approval, a non-founder
 review, a stale head, a digest mismatch, CI, PASS, silence and labels are
 not acceptance.
```

Do not change hard rule 2 (agents never merge; no founder merges their own PR
without another founder's approval). That is merge policy, not spec
acceptance. The other-founder merge approval of a founder's own PR is still
not spec acceptance unless it is a Samir/Dan spec PR and the approving
founder is not the author, with SHA+digest binding.

### 5. `docs/v3/PLANNING.md` — Goal compilation vs product spec

Keep Owner-authenticated GoalSpec. After:

> Owner intent is retained exactly. Head may compile a complete draft, but
> only Lucas accepts or amends the exact GoalSpec digest.

append:

```
 Amendment proposed 2026-10-25 (acceptance is a human decision by Lucas,
 CSL-524): **product-spec** (`docs/v3`) amendments by Samir or Dan are
 accepted by another founder's GitHub `APPROVED` review on the exact head
 SHA with file digests (`CONTROL_PLANE.md` product-spec acceptance). That
 rule is not GoalSpec digest acceptance. GoalSpec remains Owner-authenticated;
 CSL's first-user Owner remains Lucas until a GoalSpec names another founder.
```

### 6. `docs/v3/AUTONOMOUS-PLANNING.md`

After step 4 ("Lucas accepts the exact GoalSpec digest…") and the following
"Only Lucas may amend GoalSpec intent…" paragraph, append the same
product-spec vs GoalSpec distinction as PLANNING.md.

APQ-01 / APQ-02 "Lucas seam" cells stay as GoalSpec/Owner ratification items.
Add one footnote on those two rows: product-spec acceptance is D-044, not
these seams.

### 7. `docs/v3/DECISION-REGISTER.md` item 9

Current:

> 9. Ratify or amend the M1A proposal that only Lucas accepts/amends a
>    GoalSpec digest, Head owns ARM lifecycle/conflicts, and each ARM
>    exclusively owns its plan/DAG proposals while the deterministic kernel
>    owns admission.

Proposed replacement of that numbered item (the other eight items unchanged):

```
9. Ratify or amend the M1A proposal that the authenticated Owner
   accepts/amends a GoalSpec digest, Head owns ARM lifecycle/conflicts, and
   each ARM exclusively owns its plan/DAG proposals while the deterministic
   kernel owns admission. Product-spec (`docs/v3`) acceptance is not this
   item: spec changes by Samir or Dan are accepted by another founder's
   GitHub review (CSL-524 / proposed D-044). CSL's first-user Owner remains
   Lucas until a GoalSpec names another founder.
```

### 8. `docs/v3/ACCEPTANCE.md` — Release decision

Do **not** change "Lucas accepts the review branch and performs the protected
merge/deployment seams" (company-server deploy remains reserved). After that
bullet list, append:

```
Amendment proposed 2026-10-25 (CSL-524): merge of a review branch is still
not product-spec acceptance. Spec changes authored by Samir or Dan (or their
attributed agents) are accepted when a founder other than the author
approves the PR on the exact head SHA with file digests. Lucas-authored spec
PRs keep Lucas's existing acceptance path.
```

## Non-goals

- Do not accept or merge recursion#295.
- Do not reopen D-038, OR-038, OR-017, or language/runtime.
- Do not edit `.github/workflows/**`.
- Do not add a `program.json` checker in this issue (CSL-523's companion
  checker is out of Recursion scope unless Lucas asks after authoring).
- Do not treat this proposal file's publication as D-044 taking effect.

## Gate for this drafting PR

```
git diff origin/master -- CONTROL_PLANE.md ANCHOR.md state/DECISIONS.md AGENTS.md CLAUDE.md
# must be empty
test -f docs/v3/proposals/ACCEPTANCE-RULE-2026-10-25.md
python -X utf8 -B tools/check_public_scope.py
```

## After Lucas posts the authoring sentence

Apply the insertions above on this branch or a successor, re-hash the
accepted paths, request `samir-shah-ahmed` and `CDanTheMan`, and wait for
Devin then required CI. Do not merge.
