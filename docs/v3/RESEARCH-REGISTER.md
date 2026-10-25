---
title: Recursion v3 research and open questions (living register)
status: amendment proposed (2026-10-24); acceptance is a human decision by Lucas
last-updated: 2026-10-25
authority: living index; does not accept product text, admit work, clear a hold, or authorize spend
---

# Recursion v3 research and open questions

**Living register.** Lucas (2026-10-24 11:57pm PT): the Recursion spec must be dynamic,
changing with the issues and the research that is published.

This file is the living index. It is **not** accepted product text. [PRODUCT.md](PRODUCT.md)
accepted wording (including the OR-038 paragraph) changes only under the update rule
below. [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md) is the current dated proposal
(Lucas-authored; Lucas accepts). `CONTROL_PLANE.md`, `state/DECISIONS.md` and `AGENTS.md`
are not edited from this register.

Umbrella: [CSL-493](https://linear.app/csl-laboratories/issue/CSL-493) (CSL frontier map:
Nov 11 parents). Rows are Recursion parents [CSL-494](https://linear.app/csl-laboratories/issue/CSL-494)…[CSL-510](https://linear.app/csl-laboratories/issue/CSL-510) plus shared [CSL-520](https://linear.app/csl-laboratories/issue/CSL-520)…[CSL-522](https://linear.app/csl-laboratories/issue/CSL-522). Parents outside that Recursion set are out of this register.

## Update rule

1. **Publishing a csl-research record updates this register** in a **docs-only PR**.
   Path: `CSL-Laboratories/csl-research` `submissions/<record_id>/` (full 40-hex commit
   when known). Fill the `csl-research` cell and set status to `findings-published` unless
   a dated amendment is already `proposed` or `accepted` for that parent.
2. **PRODUCT.md accepted text** changes only by a dated spec amendment that is accepted.
   Lucas (2026-10-24 11:58pm PT, his words): "Samir and Dan should be able to change and
   modify the product spec with their agents as recommended and as research is
   synthesized" and "Spec changes by Samir or Dan are accepted once another founder
   approves the PR."
   - Founders: Lucas, Samir (`samir-shah-ahmed`), Dan/Chaandan (`CDanTheMan`).
   - Spec PRs authored by **Samir or Dan** (or their agents, attributed
     `Agent (…, for Samir)` / `Agent (…, for Chaandan)` / `for Dan`) are **accepted
     when a founder other than the author approves the PR**. That GitHub approval
     counts as acceptance. The author's own approval does not count. Agents still
     never merge.
   - Spec PRs authored by **Lucas** (or his agents, attributed) are accepted when
     **Lucas accepts** (this RP0 amendment, recursion#295, is that case).
   - A register update, a Linear comment, CI, an implementer's PASS, or a merge
     without the matching founder approval does not rewrite accepted product wording.
3. **Linear parent progress** (new F-n, a named decision receipt, a superseded question)
   is reflected on the next docs-only register PR. Do not wait for a product-language or
   architecture decision to keep the index current.
4. **Statuses** (exactly these four; no synonyms):

   | Status | Meaning |
   |---|---|
   | `open` | Parent is admitted; no published vault record and no dated amendment in flight |
   | `findings-published` | A csl-research `submissions/<record_id>/` exists; product text not yet proposed |
   | `proposed` | A dated spec amendment PR is open |
   | `accepted` | Accepted under rule 2. Silence is not acceptance. Self-approval is not acceptance |

5. This register does not select a language, store, framework, default relay, or spend
   route. F-n rows remain research questions until a founder decides under rule 2. F-2
   is answered in the 2026-10-24 proposal; it is `proposed` until this Lucas-authored
   amendment is accepted.

**Vault this session:** [CSL-Laboratories/csl-research](https://github.com/CSL-Laboratories/csl-research) returned GitHub API 404 (2026-10-25). Every `csl-research` cell is `none` until a readable `submissions/<record_id>/` is cited. Do not invent record IDs.

## Register

Fixed columns: parent, Linear, F-n, csl-research, status.

| Parent | Linear | F-n | csl-research | Status |
|---|---|---|---|---|
| CSL-494 | [RP0: North star + spec amendments](https://linear.app/csl-laboratories/issue/CSL-494) | F-1, F-2, F-3, F-15 | none | proposed |
| CSL-495 | [RP1: P6 Surfaces (daemon, Tauri GUI, app, CLI/TUI, mobile/PWA)](https://linear.app/csl-laboratories/issue/CSL-495) | F-2, F-12 | none | open |
| CSL-496 | [RP2: P1 Call and pilot other subscriptions](https://linear.app/csl-laboratories/issue/CSL-496) | F-5, F-6 | none | open |
| CSL-497 | [RP3: P2 Cross-chat (Task Capsule, inbox, Projects)](https://linear.app/csl-laboratories/issue/CSL-497) | F-8 | none | open |
| CSL-498 | [RP4: P3 Remote control (Control API, pairing, E2E relay)](https://linear.app/csl-laboratories/issue/CSL-498) | F-9, F-11 | none | open |
| CSL-499 | [RP5: P4 Enterprise](https://linear.app/csl-laboratories/issue/CSL-499) | F-4 | none | open |
| CSL-500 | [RP6: P5 Node orchestration](https://linear.app/csl-laboratories/issue/CSL-500) | F-10 | none | open |
| CSL-501 | [RP7: P7 BYOK and preference sync](https://linear.app/csl-laboratories/issue/CSL-501) | F-6, F-13 | none | open |
| CSL-502 | [RP8: P10 Quota and cost ledger](https://linear.app/csl-laboratories/issue/CSL-502) | — | none | open |
| CSL-503 | [RP9: P8 Agent-installable and agent-operable](https://linear.app/csl-laboratories/issue/CSL-503) | F-7, F-14, F-15 | none | open |
| CSL-504 | [RP10: P9 Verification and review](https://linear.app/csl-laboratories/issue/CSL-504) | — | none | open |
| CSL-505 | [RP11: P11–P12 Memory, learning, plugin/skill portability](https://linear.app/csl-laboratories/issue/CSL-505) | — | none | open |
| CSL-506 | [RP12: P13 Automation and events](https://linear.app/csl-laboratories/issue/CSL-506) | — | none | open |
| CSL-507 | [RP13: P14 Security and sandboxing](https://linear.app/csl-laboratories/issue/CSL-507) | F-5 | none | open |
| CSL-508 | [RP14: P15 Observability and replay](https://linear.app/csl-laboratories/issue/CSL-508) | — | none | open |
| CSL-509 | [RP15: P16 Research mode](https://linear.app/csl-laboratories/issue/CSL-509) | — | none | open |
| CSL-510 | [RP16: Evaluation and head-to-heads](https://linear.app/csl-laboratories/issue/CSL-510) | — | none | open |
| CSL-520 | [SP1: Private collaboration and closed-testing](https://linear.app/csl-laboratories/issue/CSL-520) | — | none | open |
| CSL-521 | [SP2: Lab OS (claims, merge machine, spec pipeline)](https://linear.app/csl-laboratories/issue/CSL-521) | — | none | open |
| CSL-522 | [SP3: Public demo and published results, Nov 11](https://linear.app/csl-laboratories/issue/CSL-522) | — | none | open |

`—` in F-n means no F-question from [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md) §4 is owned by that parent yet. New F-n keys are appended there and referenced here on the next docs-only PR.

CSL-494 is `proposed` because recursion#295 is the dated 2026-10-24 amendment
(Lucas-authored, unaccepted). F-2 is already answered by D-038; this amendment's
Tauri/daemon/standalone/"goat" additions are not `accepted` until Lucas accepts
(rule 2, Lucas-authored case).

## F-n index

Question text lives in [AMENDMENT-2026-10-24.md](AMENDMENT-2026-10-24.md) §4. This table only maps ownership.

| F-n | Owner parent(s) | Notes |
|---|---|---|
| F-1 | CSL-494 | Which Octopus; beat both until named |
| F-2 | CSL-494, CSL-495 | Answered by D-038 (2026-10-02). This amendment adds Tauri/daemon/standalone/"goat" beyond D-038 |
| F-3 | CSL-494 | Product language / runtime (OR-017) |
| F-4 | CSL-499 | What "enterprise" means by Nov 11 |
| F-5 | CSL-496, CSL-507 | Anthropic "ordinary, individual usage" for unattended `claude -p` |
| F-6 | CSL-496, CSL-501 | Devin/Cursor keys vs D-021; OS-keyring reference |
| F-7 | CSL-503 | Grok Bot inbound/driver API |
| F-8 | CSL-497 | Smallest Task Capsule |
| F-9 | CSL-498 | Default remote relay |
| F-10 | CSL-500 | Durable execution vs in-house replay kernel |
| F-11 | CSL-498 | Remote steering: vendor remotes vs always via Recursion |
| F-12 | CSL-495 | Native mobile vs PWA by Nov 11 |
| F-13 | CSL-501 | Preference sync backend |
| F-14 | CSL-503 | Agent-operator step-up; no agent self-approval |
| F-15 | CSL-494, CSL-503 | Anthropic's and OpenAI's own agent/bot products (working labels: Claude Bard, Codex Dots); exact products and control surfaces are RESEARCH. Do not assert product names |
