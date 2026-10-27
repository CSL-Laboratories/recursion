# O2 WorkPacket amendment (A6)

**Version:** `o2-workpacket-amendment v0.1-draft`
**Status:** DRAFT under envelope G. Not a rule-H acceptance of either profile.
**Authority for the split:** CSL-206 comment `a8988987` (attributed relay of Lucas's confirmation, 2026-10-16). That comment accepts no digest of this file.
**Blocker record:** [LANGUAGE-AGNOSTIC-ARCHITECTURE.md](architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md) §7, at base `7862cab9fbcaf450048a3b827ba3c5f888285b1b`.
**Seam:** `S-WORKPACKET` (`freeze-blocking`, `open`) in [O2-MATRICES.md](O2-MATRICES.md).

Comment `a8988987` requires four things:

1. Version the correction of the hard-coded `CORR-M1A` correlation id versus the wire profile's GoalSpec id, and of WorkPacket effects taken from GoalSpec rather than TaskNode.
2. Preserve historical fixture bytes and receipts. Pin historical outcomes separately from new normative expectations.
3. Never regenerate expected outputs from the candidate. Current Python output is not the normative oracle.
4. Use an independent verifier from another model family.

Revisit trigger recorded there: neutral-adapter work finds any further normative discrepancy. This amendment does not add a fifth discrepancy and does not close that trigger.

---

## 1. What was read

Both loci are in `scripts/v3/planning/replay/kernel.py` at `7862cab9fbcaf450048a3b827ba3c5f888285b1b` (sha256 `9327669e4ae8e2a8b97918c16cf91ec2b96797be78af989dc26939192a345338`, 35938 bytes). This change does not edit that file.

| Locus | Observation | Class |
|---|---|---|
| `_domain_record`, line 288 | `correlation_id` is the literal `CORR-M1A` on every domain record this helper seals, including a derived work packet. | source-documented |
| `derive_work_packet`, line 327 | `effect_keys` is `copy.deepcopy(goal["payload"]["allowed_effect_keys"])`. | source-documented |

The wire profile those lines disagree with is `docs/v3/M1A-WIRE-PROFILE.md` at the same commit (sha256 `8041689e110ccdafb10225181abce52bddda7ac1ea198c0118cc1d8de69f11e0`, 43532 bytes), section "Exact WorkPacket derivation":

- correlation id is the GoalSpec id;
- the payload copies the TaskNode effect fields (with the goal fence, the pause epoch, and the other TaskNode authority fields named there);
- the caller never supplies the work-packet record.

[LANGUAGE-AGNOSTIC-ARCHITECTURE.md](architecture/LANGUAGE-AGNOSTIC-ARCHITECTURE.md) §7 states the consequence: a task that narrows effects can yield a broader set if the constructor copies the GoalSpec list. Re-sealing digests without a versioned amendment does not fix that. This file is that amendment's draft, not the freeze.

---

## 2. Two profiles

The profiles do not overwrite each other. A historical byte string stays on the historical profile. A normative expectation exists only on the normative profile. Comparing them by replacing one digest in place is a failed preservation (`O2F-P10` must pass; doing the replacement is not a pass).

### 2.1 Historical profile `historical-m1a-work-packet-1`

This profile describes bytes the current helper actually seals. It is evidence of what the M1A Python reference did. It is not the normative contract.

| Field | Historical expectation |
|---|---|
| `schema_version` on the sealed record | `m1a-work-packet-1` (unchanged helper) |
| `correlation_id` | literal `CORR-M1A` |
| `effect_keys` | the GoalSpec `allowed_effect_keys` value, deep-copied |
| Oracle | the source text at the pin above, plus any fixture bytes already committed before this amendment |

Rules:

- Do not edit historical fixture files to match §2.2.
- Do not recompute their digests and call the new digest the old receipt.
- A receipt that names `CORR-M1A` remains a receipt of this profile even after a normative profile exists.
- Passing this profile does not mean the narrowing bug is accepted. It means the old bytes are still the old bytes.

### 2.2 Normative profile `normative-m1a-work-packet-2-draft`

Proposed schema version: `m1a-work-packet-2-draft`. The suffix `draft` is intentional. Rule H has not accepted a version id. A later receipt may rename it. Until then, checkers and fixtures must not drop the suffix and must not write this version onto historical records.

| Field | Normative expectation |
|---|---|
| `correlation_id` | the GoalSpec id of the packet's `goal_ref` |
| `effect_keys` | the TaskNode `effect_keys` of the packet's `task_ref`, not the GoalSpec `allowed_effect_keys` |
| Who builds the record | the kernel, not the caller (same rule as the M1A wire profile) |
| Oracle | this table. Not the return value of `derive_work_packet`. |

Worked narrowing, used by `O2F-P06` and `O2F-A01`:

- GoalSpec `allowed_effect_keys` = `[send, draft]`
- TaskNode `effect_keys` = `[draft]`
- Normative packet `effect_keys` = `[draft]`
- A packet that instead carries `[send, draft]` is the historical copy evaluated against the normative profile. That case is `O2E-007`. It is not "close enough".

The historical profile still expects `[send, draft]` for that same pair of inputs. Both expectations are true, on different profiles. A single expected file that tries to hold both is a mixed oracle and fails the split.

### 2.3 What this amendment does not claim

- It does not say the Python function has been changed. It has not.
- It does not generate a golden JSON file.
- It does not choose a product language in which the normative profile will be implemented.
- It does not accept `m1a-work-packet-2-draft` as a public wire version. Public version bytes stay with CSL-238 / recursion#231 until H says otherwise (`S-WIRE-BYTES`).
- It does not search for further drift. The A6 revisit trigger stays open.

---

## 3. Regenerating expectations is a failure

`O2E-019` / `O2F-A13`: any procedure that runs the candidate, including today's `kernel.py`, and stores that output as the normative expected packet, is a failed gate. The same ban applies to "update the fixture from the last green run".

The historical profile may cite observed bytes that were produced in the past and then frozen. Freezing them is a human or a reviewed fixture commit under a later child. It is not a license for the checker to call the function again and overwrite the pin. After freeze, those bytes are inputs. The candidate is not.

The independent verifier (contract §8) rebuilds the normative expectation from §2.2 and diffs it against the candidate. It uses another model family. It does not ask the candidate whether the candidate passed.

---

## 4. Hand-check

| A6 term | Where it is satisfied in this draft |
|---|---|
| Version both discrepancies | §2.1 and §2.2 |
| Preserve historical bytes and receipts | §2.1 rules; `O2F-P10` |
| Separate normative expectations | §2.2 table, not a rewrite of §2.1 |
| Do not regenerate from the candidate | §3, `O2E-019` |
| Independent verifier, other model family | [O2-CONTRACT.md](O2-CONTRACT.md) §8 |

The seam stays `open` because no rule-H comment binds this file's digest.
