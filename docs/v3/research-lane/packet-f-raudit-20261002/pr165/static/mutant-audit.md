# PR165 mutant reachability and discriminator audit

Candidate: c08c93fb475ca770d56dd1e1996d8e6e94c33f81, user-authorized unmerged PR165 head (latest instruction, 2026-10-02 21:45:57 UTC). Retrieved 2026-10-02.
**Every entry is STATIC ONLY / NOT RUN.** Reachable discriminator means a call and a differing checked value/event can be traced in the source; it does not mean the call was executed or a runtime rejection was observed.

## Inventory

rpc_mutants.py has **32 top-level function declarations**, **28 unique names**, **22 unique names referenced** in the two relevant gates, **six unreferenced unique names**, and **four earlier declarations shadowed by later definitions**. _ForgedRight is a helper class, not a separately declared mutant function.

Eighteen unique invariant mutants have visible checked-value discrepancies. Two observed mutants (disabled_reads and restore_reexecute) have visible event discrepancies. forged_authority has no differing asserted field from the intended negative control; worker_silent is tested with observation already disabled, so its independent sensitivity is not established.

## Gate and expectation source anchors

- [run/check_rpc_invariants.py L42–54](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_invariants.py#L42-L54): subset matcher and case evaluation
- [run/check_rpc_invariants.py L57–140](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_invariants.py#L57-L140): all invariant candidate/mutant call sites
- [run/rpc_expected.py L16–59](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_expected.py#L16-L59): all literal expected subsets
- [run/check_rpc_observed.py L43–163](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_observed.py#L43-L163): all observed call sites and assertions

The call-site shorthand in the inventory refers to those pinned files and exact lines. A later same-name definition overrides an earlier definition before run() is invoked; a textual reference to that name does not cover both bodies.

## Complete rpc_mutants inventory

| Mutant declaration | Gate call site | Asserted discriminator or absence | Static status |
|---|---|---|---|
| [m_summary_equal_seed](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L9-L10) | invariants L59–60 | F02_identity: distinct=False vs True | Reachable discriminator |
| [m_summary_concatenation](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L13-L16) | invariants L63–64 | F02_collision: a|b + | + c and a + | + b|c both a|b|c; distinct=False vs True | Reachable discriminator |
| [m_replay_regenerates](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L19-L20) | invariants L61 | F02_replay: model_calls=1 vs 0 (wrong bytes are not an asserted expected field) | Reachable discriminator |
| [m_closure_auto_budget (earlier definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L23-L24) | Name later rebound at L123 | No gate can reach this earlier body through X.m_closure_auto_budget after module load | SHADOWED |
| [m_disabled_leaks](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L27-L30) | None | No asserted field in current gates | UNREFERENCED |
| [m_revoke_delayed](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L33-L34) | invariants L85–86 | F05_revoked: outcome=allow vs refuse | Reachable discriminator |
| [m_trust_claim](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L37-L40) | None | No asserted field in current gates | UNREFERENCED |
| [m_precedence_last_loaded](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L43-L44) | None | New _v2 name is a separate function; calling it does not cover this one | UNREFERENCED |
| [m_evidence_synth](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L47-L48) | invariants L102 | F08: disagreement=False vs True; decision=x vs None. Lost refs are not independently asserted | Reachable discriminator |
| [m_cut_accept_latest (earlier definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L51-L52) | Name later rebound at L135 | No call to this earlier three-argument body | SHADOWED |
| [m_successor_last_completion (earlier definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L55-L56) | Name later rebound at L139 | No call to this earlier two-argument body | SHADOWED |
| [m_route_name_only (earlier definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L59-L60) | Name later rebound at L166 | No call to this earlier three-argument body | SHADOWED |
| [m_restore_replay](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L63-L64) | None | New m_restore_reexecute is a separate function | UNREFERENCED |
| [m_graph_import_to_discover](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L67-L68) | invariants L129–136 | F15 four rejection cases: reason=None vs each required reason, imported=True vs False | Reachable discriminator |
| [m_silence_pass](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L71-L76) | None | Old boolean classification mutant has no current call | UNREFERENCED |
| [m_disabled_reads](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L85-L88) | observed L60–63 | Records CATALOG_READ >0 while disabled candidate requires zero; outcome still says disabled | Reachable event discriminator |
| [m_forged_authority](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L91-L93) | observed L96–98 | Returns completed but return is discarded. Harness denies forged proof; no admitted effect matches good negative case | REFERENCED; NO DIFFERING ASSERTED FIELD |
| [m_quiet_no_effect](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L96-L97) | None | Returns completed without useful effect; never submitted to a gate control | UNREFERENCED |
| [m_restore_reexecute](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L100-L109) | observed L126–130 | REEXECUTE count>0 vs candidate's zero; REAPPROVE/FUNDS_RESET are also emitted but mutant check only examines REEXECUTE | Reachable event discriminator |
| [m_worker_silent](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L112-L113) | observed L161–163 | HOLD_UNKNOWN checked with observer already disabled; candidate under disabled observer also yields HOLD_UNKNOWN | REFERENCED; CONFOUNDED SENSITIVITY |
| [m_closure_silent_drop](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L117-L120) | invariants L77–78 | F03_valid: dropped=[] vs [O2:v1,O3:v1]; loss_record=[] is outside expected subset | Reachable discriminator |
| [m_closure_auto_budget (effective definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L123-L125) | invariants L80–81 | F03_unfittable: outcome=ok/dispatched=True vs mandatory_context_unfittable/False | Reachable discriminator |
| [m_cut_values_only](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L128-L132) | invariants L108–109 | F09_aba_unstable: outcome=ok vs composition_cut_unavailable; aba_detected=False vs True | Reachable discriminator |
| [m_cut_accept_latest (effective definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L135-L136) | invariants L107 | F09_mixed: outcome=ok vs composition_cut_unavailable | Reachable discriminator |
| [m_successor_last_completion (effective definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L139-L145) | invariants L115 | F10_late: accepted=B vs A; corrupted epoch/steps not in case subset | Reachable discriminator |
| [m_successor_duplicate_adopt](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L148-L154) | invariants L116–117 | F10_duplicate: accepted=B vs A | Reachable discriminator |
| [m_successor_directive_ignored](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L157-L163) | invariants L118–119 | F10_directive: accepted=B vs A | Reachable discriminator |
| [m_route_name_only (effective definition)](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L166-L170) | invariants L124–125 | F13_hold_unknown: outcome=ok vs hold; hidden_visibility=known vs unknown | Reachable discriminator |
| [m_graph_selected_presence_only](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L173-L182) | invariants L138–139 | F15_invalid_selection: reason=None vs invalid_provider_selection | Reachable discriminator |
| [m_precedence_last_loaded_v2](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L185-L186) | invariants L94–95 | F07_override: winner=skillB vs skillA | Reachable discriminator |
| [m_precedence_accept_any_override](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L189-L192) | invariants L96–97 | F07_override_unauthorized: outcome=ok vs hold | Reachable discriminator |
| [m_revoke_erased_by_coalesce](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/rpc_mutants.py#L195-L198) | invariants L88–89 | F05_pending: revoked=False vs True; revoke_fence/history not expected fields | Reachable discriminator |

## Literal fixture mutants

| Definition | Invocation and checked predicate | Static status |
|---|---|---|
| [render_by_enumeration](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/reference_model.py#L65-L70) | [run/check_rpc_fixtures.py L61–64](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_fixtures.py#L61-L64): base output equals exact expected; reversal differs | Reachable byte discriminator |
| [render_empty_spans](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/reference_model.py#L73-L75) | [run/check_rpc_fixtures.py L73–75](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_fixtures.py#L73-L75): keeps bytes/ranges, candidate span validator returns refuse | Reachable candidate-validator discriminator |
| [render_invalid_coordinate](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/reference_model.py#L78-L83) | [run/check_rpc_fixtures.py L76–77](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_fixtures.py#L76-L77): final end is len+1; candidate validator reason=invalid_coordinate | Reachable candidate-validator discriminator |
| [render_gap_spans](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/reference_model.py#L86-L88) | [run/check_rpc_fixtures.py L78–79](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_fixtures.py#L78-L79): last span missing; candidate validator reason=uncovered_output_byte | Reachable candidate-validator discriminator |
| [expected_ranges_by_codepoint](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/oracle.py#L31-L40) | [run/check_rpc_fixtures.py L102–107](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_fixtures.py#L102-L107): note ends at 24 vs candidate's 25 | Reachable byte-unit discriminator; declared in oracle rather than reference_model |

The three span mutants are checked by code in the candidate module itself. Exact base spans have a literal independent assertion, but this does not turn candidate.verify_spans into an independent oracle. F12 spans are not checked against an independent ownership map.

## F06 decisive distinction

Good negative candidate path:
- expected outcome is refused;
- one effect attempt is denied;
- no admitted effect or useful completion.

Forged-authority mutant path:
- mutant returns completed;
- its return is ignored;
- the harness rejects the forged proof;
- the gate only requires no admitted effect, exactly as for the good candidate.

Therefore the label “forged-authority mutant detected” is not backed by a mutant-versus-expected discrepancy on an asserted field. It demonstrates that the harness refused the proof. The same shared checker should validate both result and independently recorded effects if false completion is the planted fault. [run/check_rpc_observed.py L76–98](https://github.com/NotSkittlz/recursion/blob/c08c93fb475ca770d56dd1e1996d8e6e94c33f81/experiments/v3/capability-composition/run/check_rpc_observed.py#L76-L98)

The authorized positive control bypasses candidate.admit_import entirely. An always-refusing admit_import that still generates a denied sink attempt survives this fixture's checks. That counterexample is source reasoning, not a measured run.

## Single-fault quality

The claim that each mutant violates exactly one predicate should be interpreted cautiously. For example, replay_regenerates changes both bytes and model_calls; evidence_synth drops refs, changes disagreement and synthesizes a decision; restore_reexecute emits three forbidden operations. A detected multi-fault mutant does not prove detection of each fault independently. The strengthened successor mutants likewise set multiple output fields incorrectly, although accepted=B alone provides a checked discriminator.

**Recommendation:** derive the active inventory mechanically, require unique declaration IDs, call every active mutant through the same observation/checker route as its positive candidate and record the exact failing field. Use one-fault mutations when claiming sensitivity to each independent predicate.\
**Rival:** these are intentionally smoke-test bundles and obsolete definitions are retained for context. If so, label the retired definitions explicitly and narrow “every planted mutant.”\
**Falsifier:** any active mutant lacks a call or passes the checker; a bundled fault's supposedly tested sub-fault survives when isolated.\
**Non-proof:** complete coverage of this finite inventory is not exhaustive mutation adequacy, model quality, natural-workload error rate or security proof.
