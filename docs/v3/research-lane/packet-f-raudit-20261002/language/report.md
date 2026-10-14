# R-AUDIT: R-LANG-informed architecture correction

Research date: 2026-10-02 UTC. Status: source research and proposed replacement text; no target execution. Project snapshot: `9f4122b50dc8117b185b2cd52244a10df3eb4285`. This report uses public primary documentation and the private Recursion contracts subsequently authorized by the user. No language, store, framework, transport, plugin host or vendor is selected.

## Findings that change the outline

- **Source-documented:** the newly retrieved R-LANG intake is explicitly advisory, records no winner, and says all proposed target experiments are unexecuted. It recommends retaining Rust, Go and separate CPython GIL/free-threaded reference arms, with C#/.NET the first expanded challenger. An advisory comparison is not a selected deployment or execution authorization. [P1, P2]
- **Source-documented:** M1B is a strict *application profile* around JCS, not unrestricted RFC 8785 input. It rejects floats, negative zero, duplicate keys and malformed encodings before schema validation; it distinguishes missing from null and bounds integers. Its digest representation is `sha256:` plus 64 lowercase hexadecimal characters. [P3]
- **Inferred:** ordinary serializer options do not establish that profile. The cross-language danger is information loss before validation: duplicate collapse, absence/null collapse, number coercion and invalid-Unicode replacement. A configuration failure is not evidence that the whole language cannot express an invariant. Public mechanisms and counterexamples are detailed below. [S3–S24]
- **Locally observed, static:** the intake calls the reconstruction inventory “15 neutral units,” but the source report's section 7 table contains **14 named rows**, lines 209–222. The intake's parenthetical list also contains 14. This is an inventory defect to correct, not a reason to invent an extra unit. [P1, P2]
- **Source-documented:** P1 is a language-neutral plugin-contract proposal; process/IPC, Wasm component, native FFI and remote protocol are alternatives. The retrieved intake selects none. RC-01 through RC-09 are proposed reconstruction criteria, not passing receipts. The FU-03 integrated specification is candidate work with explicit reconciliation and independent-review prerequisites. [P1, P2, P4]

### Evidence convention and citation resolution

**D / source-documented** means a retrievable specification, API/source file, or pinned project artifact says the fact. A documented historical test verdict remains a reported result, not a new observation. **L / locally observed** is static inspection of retrieved material. **I / inferred** covers reasoning, candidate design and proposed tests. **U / unknown** covers absent accepted decisions, pins, observations or receipts. `unsupported` is reserved for an identifier/claim lacking supporting provenance, not used as a substitute for unknown execution results.

Every citation ID resolves to an exact URL, retrieval date **2026-10-02**, document section/version and claim scope in `source-ledger.md`. Moving public documentation is explicitly marked; it is not a frozen project toolchain. All hypothetical observations below are **NOT RUN**. Source-documented API availability does not mean installation, qualification or adoption.

## 1. Checklist item (i): record the outcome and supersessions accurately

**D [P1, P2, P5]:** D-037 authorizes Python only as a disposable fixture harness, while the product language remains unselected. D-039 commissions reconstructable, language-agnostic architecture and does not select technologies. The new R-LANG intake calls its result research evidence, not a decision. It does not supersede D-037 or D-039. D-038's explicit supersession concerns older first-release GUI/mobile exclusions, not a language choice.

**U:** an accepted later language winner, newly frozen executable toolchain matrix, selected host, complete integrated gate pass and accepted reconstruction cost ceiling are not established by these sources. “No supersession established in this inspected snapshot” is the supported boundary; it is not a claim that no future/private decision can exist.

**Proposed replacement text (I, proposal C1):**

> R-LANG has returned advisory comparison and reconstruction requirements; it selects no language or runtime. D-037 remains disposable-harness authorization and D-039 remains the language-agnostic reconstruction mandate. The advisory roster retains Rust, Go and both CPython reference arms; expansion priorities are research choices, not product commitments. Record any later outcome with its exact decision, source revision, effective scope and explicit supersessions. Until then, toolchain qualification and host selection remain open.

**Competing option:** keep only a concise no-selection statement and a link to the decision ledger, omitting the advisory roster from normative architecture. That reduces stale duplication. Treating the shortlist as selected is a rejected shortcut. **Decisive falsifier for C1:** a later accepted decision selects a language/supersedes this boundary, or the duplicated roster drifts from its source and misdirects an admitted implementation. **Non-proof:** correct status labeling does not establish feasibility, performance or readiness to build the product.

### Independent critique of the intake count

**L [P1, P2]:** the actual 14 rows are authority/topology; state/transitions; invariants/abstraction; four-function API; schemas/wire; canonical bytes/digest domains; refusal alphabet; concurrency/fencing; persistence/recovery; effects/adapters; plugin/edition interface; context/composition; release/build identity; evidence package.

**Proposed amendment (I, part of C1):** replace the unsupported count of 15 with “the 14 named neutral units in R-LANG section 7, plus an unresolved-seam register,” unless a separately identified omitted unit is added through review. **Competing option:** retain 15 and identify the missing row explicitly. **Falsifier:** the pinned table or a later accepted revision contains a fifteenth distinct row. **Non-proof:** an accurate count says nothing about completeness of those units.

## 2. Checklist item (ii): separate neutral semantics from language mechanisms

### 2.1 Three byte contracts, not one ambiguous phrase

**D [S1]:** RFC 8785 specifies compact UTF-8 output, recursively sorted raw property names using unsigned UTF-16 code units, unchanged array order and Unicode content, ECMAScript-compatible binary64 rendering, and errors for duplicates, invalid Unicode and nonfinite numbers. Appendix B supplies number samples. The RFC is Informational.

**D [S2]:** verified technical erratum 7920 recommends erroring on negative-zero input; this is distinct from serialization's zero result. The retrieved RFC Editor search record provides the text and status; direct page fetch failed. Its `SHOULD` is not a universal `MUST`.

**D [P3]:** M1B independently mandates rejection of negative zero and all floats before schema validation. Its maximum decode depth is 64; canonical object size is 262,144 UTF-8 bytes. `Count` is 0–2,147,483,647 and `Microunits` 0–9,007,199,254,740,991; booleans are not integers. Exact fields, nullability and declared set-array ordering are part of admission.

**L [P8]:** the retrieved M1B execution schema checker connects `json.loads` to `_reject_float` and to an integer hook that specifically rejects `-0`. **I:** this supports decimal/exponent-token rejection expectations (`1.0`, `1e0`) for that checker path; these were not executed here. The prose should retain an explicit lexical rule, because a schema's mathematical integer predicate alone is weaker.

**D [P4]:** candidate FU-03 `RCV1` is a separately epoch-bound test interchange with deterministic CBOR and digest-domain proposals. It is not an adopted broker or store format and must not retrofit frozen M1 contracts.

**I:** therefore an M1B `-0` or `1.0` input should never be admitted merely because a generic canonicalizer can convert it to `0` or `1`. Likewise, a generic supplementary-Unicode property-name test is useful for a reusable JCS module but does not make those names legal fields of a closed M1B schema. The architecture must name which entry point and profile each vector exercises. [P3, P4]

### 2.2 Nonselecting language/configuration feasibility matrix

Rows cover the advisory comparison families in P1. They describe public mechanisms and risks, not a measured ranking. Each **I** implication is covered by proposal C2 and its rival/falsifier/non-proof below.

| Family and documented behavior | Architecture consequence and decisive specimen to preserve |
|---|---|
| **Rust / serde_json. D:** version 1.0.145 `Value` uses BTreeMap by default; `preserve_order` changes it to insertion-preserving IndexMap. Rust `str` ordering is byte-lexicographic. Indexing a missing field with square brackets yields `Value::Null`, whereas `get` can report absence. [S6, S7] | **I:** neither sorted maps nor convenient indexing establish profile correctness. Preserve an explicit UTF-16 comparator for generic JCS and presence-aware access for required nullable fields. Specimens: absent `replacement_digest` versus explicit null; supplementary/BMP property-name order in a module-only test. |
| **Rust numbers/options. D:** `arbitrary_precision` preserves arbitrary number text/precision; `float_roundtrip` is a separate feature. `Number::from_f64` returns no Number for NaN/infinity, but direct serializer `serialize_f64` writes null for those values; `serialize_none` also writes null. `skip_serializing_if` can omit fields. [S8–S11] | **I:** pin call path and feature set, not just crate name. A pre-validation conversion to null could erase a forbidden value; omission attributes can break exact-field contracts. Test direct values and parsed values separately. Arbitrary precision is not automatically a JCS number-rendering policy. |
| **Go / encoding/json. D:** v1 documentation records last/merged duplicate handling, float64 decoding into interface values, invalid-UTF replacement, HTML-oriented escaping, sorted map keys and omission options. `UseNumber` changes interface-number representation; `Encoder.Encode` adds a line terminator. Strings compare bytewise. [S12, S13] | **I:** pin v1/v2 API, options, tags and custom methods. Reject before destructive decode; separately validate number tokens and field presence. `SetEscapeHTML(false)`, sorted maps or `UseNumber` alone cannot certify the profile. A trailing newline belongs to framing, not canonical payload bytes. |
| **CPython. D:** `json` defaults escape non-ASCII, add separator whitespace and permit NaN/infinity; repeated names keep the last value. It offers `object_pairs_hook`, number hooks and `parse_constant`; its examples preserve a float's `.0`. Python string comparison is code-point-based. [S4, S5] | **I:** strict hooks can support admission, but `sort_keys=True` plus compact separators is not a general JCS implementation. Distinguish exact integers from bools, avoid dictionary access that conflates absence with None, and test GIL/free-threaded arms separately. Do not infer a native impossibility from defaults. |
| **TypeScript/ECMAScript. D:** `JSON.stringify` converts nonfinite Numbers to null, omits unrepresentable object values, substitutes null in arrays, invokes `toJSON`/replacers, and throws for an untransformed BigInt. TypeScript annotations are erased. [S3, S40] | **I:** admit plain profile data before serialization; reject forbidden values rather than silently normalizing them. Preserve raw input evidence before ordinary object construction. Schema validation must be runtime behavior. Integer-index object enumeration must not be mistaken for JCS lexical ordering. |
| **C#/.NET. D:** System.Text.Json's default encoder escapes non-ASCII; documented block lists can change between releases. NumberHandling can read quoted numbers, write numbers as strings, or encode nonfinite values as named strings. Null/default-ignore conditions omit properties. [S14–S16] | **I:** freeze encoder, number-handling, ignore rules, converter/source-generation configuration and runtime. “Relaxed escaping” or a flag named `Strict` is not a JCS certificate. Test `0`, false, null and missing separately and reject forbidden numeric tokens before conversion. |
| **Java/Kotlin-JVM. D:** Java String comparison uses UTF-16 character values, but `Double.toString` has Java-specific formatting including signed-zero representations. Jackson 2.18.0 has explicit duplicate-detection configuration. Kotlin `explicitNulls=false` omits nulls and can yield asymmetric round trips with non-null defaults; `encodeDefaults` and special-float admission are separate options. [S17–S22] | **I:** a suitable string comparator does not qualify a whole JSON stack. Pin parser, serializer, Kotlin/JDK versions, generated serializers and defaults. No blanket “JVM JSON” result is meaningful. Test missing/null/default and decimal/exponent tokens independently. |
| **C++ / example JSON library. D:** nlohmann/json uses sorted objects by default; `ordered_json` preserves insertion order. Its numeric documentation distinguishes `0`, `0.0`, `-0.0` and serializes NaN as null. [S23, S24] | **I:** freeze the library and numeric/object template parameters, compiler and exception settings. Default sorted output is insufficient; a documented `-0.0` output is already a generic-JCS counterexample. Under M1B, those float cases belong to rejection tests instead. |

**I, scope note:** the public sources establish mechanisms that may implement a neutral contract with adapters. They do not prove all constraints, malicious-input handling, complete supported OS closure or budgeted implementation cost. Each row needs a frozen executable identity before experimental comparison. C#/.NET, JVM, C++ and Node remain optional comparison expansion, not required new build work. [P1, P2]

### 2.3 Proposed replacement text (C2)

> The language-neutral contract owns input grammar, field presence/nullability, numeric domains, Unicode validity, key comparator, array semantics, canonical UTF-8 bytes, digest preimages and refusal precedence. Language-specific implementations may choose parsers, data structures, serializers, numeric representations and error mechanisms only behind adapters that preserve that contract. M1B strict admission, generic JCS module conformance and candidate FU-03 test interchange remain distinct profiles. Options and generated-code settings are frozen inputs, not incidental defaults.

**Competing option:** implement only schema-specialized encoders for the small frozen M1B domain, with an independent literal-byte oracle, rather than require a reusable general-JCS adapter layer. Either must preserve the accepted profile. Letting native output define the specification is a rejected shortcut. **Decisive falsifier for C2:** the supposedly neutral specification leaves two defensible outcomes, or the general adapter approach cannot preserve a required distinction within an accepted bound while the specialized approach can. **Non-proof:** the split does not establish any candidate passes or justify a wider profile.

## 3. Checklist item (iii): make an inability claim checkable

**I:** replace the unrestricted “required invariant cannot be expressed in language L” with a claim about an identified *language + runtime + toolchain + topology + allowed primitives + host profile*. A library default failing one vector falsifies that configuration. It does not prove the language incapable because a different parser, representation or boundary may be allowed.

### Required witness record (proposal C3)

1. Exact accepted invariant and source; admitted and rejected input domains; expected byte/refusal/state transition.
2. Exact arm identity, including forbidden escape hatches, allowed FFI/native dependencies and resource bounds.
3. Smallest input or event history exposing the defect, with expected observable from an independent source.
4. Complete loss/constraint argument: identify the step that makes two contract-distinct inputs indistinguishable, or a required platform mechanism unavailable.
5. Candidate remedy and why it is permitted, disallowed or still unknown; independently reviewed result if a later authorized test occurs.
6. Narrow verdict: configuration rejected, topology unavailable on profile, bounded implementation incomplete, specification ambiguous, or genuine inability under stated constraints. No unsupported upgrade to whole-language impossibility.

### Concrete non-executed examples

- **Rust configuration falsifier, I:** use bracket indexing for `replacement_digest`, then test otherwise-identical records with that field absent versus null. If the adapter obtains Null for both and never checks presence, it cannot enforce P3's distinction on that path. Presence-aware `get` is a documented competing mechanism; the failure does not condemn Rust. [P3, S6]
- **Go topology falsifier, D/I:** Go's native plugin facility documents support only for Linux, FreeBSD and macOS and strong toolchain/dependency coupling. Therefore a proposed “same native Go plugin facility on required Windows and Linux” topology fails its stated portability requirement on the documented facility, without executing it. A process, Wasm or native-adapter design is a different arm, not proof the Go language fails. [S25, P5]
- **Numeric-token falsifier, I:** if an M1B entry point converts input `1`, `1.0` and `1e0` to one indistinguishable numeric value before admission and retains no token evidence, a later numeric-value predicate cannot reconstruct which lexical input arrived. Remedy: an admitted token-aware decoder, not an invented refusal code or changed wire profile. [P3]
- **Contract ambiguity falsifier, I:** if two independently authored implementations preserve every written rule but disagree because absent/default behavior was never specified, the architecture is incomplete. Do not label either language “unsupported” until that seam is resolved. [P2]

**Proposed replacement text (C3):**

> An invariant-feasibility rejection identifies the exact language/runtime/topology and frozen constraints, a minimal counterexample, independent expected behavior, the failing boundary and permitted remedies considered. A default-library mismatch rejects that configuration only. Claims of language-level inability require a constraint argument excluding compliant permitted implementations; unresolved implementation or specification work is unknown, not inability.

**Competing option:** retain the case as bounded unknown and compare a different admitted arm, without spending the current budget on an inability argument. Rejecting a whole language after the first prototype is a rejected shortcut. **Decisive falsifier for C3:** a compliant implementation within the same frozen constraints defeats the proposed inability witness, or its proof effort exceeds the admitted decision budget. **Non-proof:** finite success is not an impossibility proof, universal correctness proof or proof of affordable implementation.

## 4. Checklist item (iv): re-pin canonicalization and digest KATs per toolchain

**D [P3, P4]:** M1B already names SHA-256; candidate FU-03 distinguishes typed digest preimages and requires retained bytes, independent stacks and literal expected outputs. The architecture should not replace those contracts with a bare hash equality check.

**D [S26, S27]:** NIST provides SHA test vectors and a worked SHA-256 example: message bytes `61 62 63` have digest `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad`. NIST expressly says using its vectors does not replace CAVP validation. This is a primitive KAT, not a canonical-JSON fixture.

### Proposed four-layer KAT record (C4; NOT RUN)

| Layer | Frozen evidence | Failure that rejects the candidate |
|---|---|---|
| Strict admission | Input bytes, profile/schema, literal decision/refusal, permitted limits, token-aware negative cases | Invalid input normalized into valid state; missing equals null; bool accepted as a count; differing refusal precedence |
| Canonical bytes | Literal accepted value/input, exact expected UTF-8 bytes and first-difference report | Wrong comparator, whitespace, escaping, ordering, value spelling, newline/BOM or hidden normalization |
| Primitive digest | Independently sourced algorithm vectors, raw byte inputs, literal output bytes | Mismatch; string encoding/hex text accidentally hashed in place of intended bytes |
| Integrated digest binding | Independently reviewed exact preimage, domain/schema/omission rules, canonical payload bytes, literal digest and envelope | Correct hash over wrong preimage; wrong omitted field/domain/length; actual payload bytes absent from evidence |

**I:** retain one identical normative corpus across arms, but a distinct result manifest for each executable identity. A public API citation is not the pin. Required fields: compiler/runtime distribution and patch; source/build artifact digests; dependency lock; serializer source/version/features; generated bindings/options; hash implementation and native provider; architecture/OS; profile/errata policy; corpus and oracle revision; independently reviewed expected-data digest. A change creates a new result identity. Existing historical reference versions do not authorize automatic upgrades. [P2, P4]

### Per-family repinning obligations

| Family | Public hash mechanism (availability only) | Candidate identity and additional KAT focus |
|---|---|---|
| Rust | `sha2` 0.10.9 documents SHA-256/Digest APIs. [S28] | Exact rustc/target, dependency closure, serde_json features and actual canonicalizer; compare direct serialization versus Value construction; absence/null and comparator controls |
| Go | `crypto/sha256` documents byte-oriented Sum256. [S29] | Exact Go build, architecture, JSON API generation/options/tags; delimiter/newline and UseNumber controls; do not borrow one arm's output as oracle |
| CPython GIL | `hashlib` documents byte input and SHA-256; backend may depend on OpenSSL/HACL* availability. [S30] | Interpreter patch/build, linked hash backend, json implementation/options, extension closure and GIL state |
| CPython free-threaded | Same documented API, separate executable arm. [P2, S30] | Independent build/runtime identity and extension closure; same bytes required, no result inherited from GIL arm |
| C#/.NET | SHA256.HashData exposes byte/span/stream APIs. [S31] | Runtime/publish mode, native provider, source-generated serializers/converters, encoder and ignore/number options |
| Java/Kotlin-JVM | MessageDigest supplies SHA-256. [S32] | JDK distribution/security-provider identity; Kotlin/compiler/serializer plugin; JSON library and all null/default/number options |
| TypeScript/Node | `crypto.createHash` supplies byte-buffer hashing. [S33] | Node/V8/OpenSSL build and native addons; emitted JS, canonicalizer and serializer hooks; runtime schema/admission |
| C++ | OpenSSL EVP exposes digest operations as one illustrative implementation. [S34] | Compiler/stdlib/build flags and chosen digest implementation/provider; exact JSON library/template settings; exception and conversion paths |

**U:** actual candidate executable hashes, canonicalizer lockfiles, archived corpus identities and per-arm PASS receipts are not supplied as a newly frozen acceptance package. This report has not manufactured them. Historical documentation versions above are source anchors, not selected toolchains or a request to install anything.

### Small proposed specimens for independent materialization

All are **I** test design, not accepted project fixture IDs or execution results:

- Reversed construction order of `{"b":2,"a":1}`: module bytes `{"a":1,"b":2}`; preserve array `[2,1]`. [S1]
- Module-only keys U+1F680 and U+E000: expected UTF-16 ordering puts U+1F680 first. Preserve composed/decomposed text distinctly. Closed M1B object schemas still reject unknown names. [S1, P3]
- Required nullable M1B field: absent fails; explicit null follows that field's schema. Mutate one state at a time through missing, null, zero, false and empty container. [P3]
- M1B number admission: `-0`, `1.0`, `1e0`, NaN, infinity, bool-as-count and upper-bound-plus-one fail at their specified boundary; ordinary valid bounded integers are positive controls. Preserve lexical evidence for the first three. [P3]
- Generic-JCS numeric module only: use the published Appendix B corpus as a pinned external test source. Do not apply its float-admission expectations to M1B. A separate defensive-input policy records erratum 7920. [S1, S2]
- Digest composition: pair an independently reviewed M1B object with its exact omitted-self-digest preimage, then separately test accidental inclusion, wrong encoding, newline and prefix hashing. Expected integrated digest values remain **U** until independent materialization; the NIST primitive example alone is insufficient. [P3, S26]

**Proposed replacement text (C4):**

> Every candidate toolchain carries a frozen strict-admission, canonical-byte, digest-primitive and integrated-preimage KAT manifest. The same reviewed literal corpus is used across arms; actual bytes and typed results are retained. Compiler/runtime/dependency/feature/backend changes invalidate the previous result identity. A passing primitive hash test or equal hashes alone cannot establish canonicalization or profile conformance.

**Competing option:** per-schema generated encoders, each checked against an independently authored literal-byte oracle and the same digest KATs, rather than a reusable canonicalization module. Shared smoke tests alone are insufficient. **Decisive falsifier for C4:** one admitted input produces mismatched normative bytes, refusal or digest preimage, or the allegedly independent oracle derives expectations from the candidate. **Non-proof:** KATs do not establish collision resistance, exhaustive parser security, compiler soundness or cryptographic certification.

## 5. Checklist item (v): no-language-leak is a host-qualified obligation

**D [P1, P2]:** no FFI/plugin host is selected in the retrieved R-LANG intake. P1 proposes external records rather than host-language authority artifacts. “Retest against the selected host” is therefore a future admission dependency, not a completed step.

### Host alternatives and factual constraints

| Option, not selection | Primary-source feasibility fact | Proposed no-leak counterexample |
|---|---|---|
| Native C/system FFI | **D:** Rust's FFI documentation distinguishes unwind-compatible ABIs; foreign exceptions entering a non-unwind boundary can produce undefined behavior, and abort settings remain material. [S35] | **I:** a plugin returns an exception type, pointer, runtime object handle or callback closure whose interpretation is necessary to decide authority. Also test allocator ownership, lifetime, reentrancy and stale references across replacement. An ABI match alone is insufficient. |
| Wasm component boundary | **D:** WIT has records, variants, option/result, byte lists and owned/borrowed resources. NaN payload bits are not guaranteed across WIT interfaces. Canonical ABI specifies cross-language lowering/lifting and memory representation. [S36, S37] | **I:** language bindings collapse missing/null/default states or change error meaning; a resource handle is mistaken for an authority grant. “Canonical ABI” does not mean canonical JSON or equal authority semantics. |
| A particular Wasm engine | **D:** Wasmtime documents nondeterminism through imports, NaNs, relaxed SIMD, growth and interruption, with configuration measures; Node v24.0.0 explicitly warns against relying on its WASI implementation for untrusted-code sandboxing. [S38, S39] | **I:** host clocks, RNG, ambient filesystem, epoch interruption or platform resource failure change canonical decisions without admitted input evidence. Qualify an engine/configuration, never the label “Wasm.” |
| Process/IPC or remote protocol | **D:** these remain compared alternatives in R-LANG, with no transport adopted. [P1, P2] | **I:** identical records yield different command identity, refusal ordering, epoch fences, credit/cursor behavior or uncertain-effect treatment after a core-language swap. Process separation does not itself provide a neutral schema or durable authority. |

### Proposed host-neutral compatibility boundary (C5)

**I:** keep schema and behavior independent of generated host APIs. Review exact widths, encodings, nullability, errors, ownership, resource lifetime, time/cancellation meaning, version negotiation, digest binding and all independent authority fields. Treat SDK-native values as adapter-local. Pointers and handles can exist internally; their mere bit patterns must not become portable evidence of authority. [P2, P3]

**I:** future tests require the unchanged external plugin artifact and contract transcript against both core implementations, independently observed disabled behavior, revoke/reload with stale queued work, malformed/oversized payloads, host exceptions/traps and valid positive controls. A candidate-returned `no_effects=true` is not an observation of an effect boundary. The current verifier reconciliation specifically separates independent expectations from independent observations. [P2, P6]

**Proposed replacement text (C5):**

> The plugin/edition contract is language-neutral; SDK signatures, pointers, object identities, runtime type names, exception classes and callbacks are adapter-local and confer no authority. Host choice remains open. Each admitted host/topology must preserve the same schema, bytes, grants, lifecycle, refusal and uncertainty semantics under independent observations, including disabled and stale-reload cases. Generated WIT/C/IPC bindings are mechanisms, not behavioral acceptance.

**Competing option:** a versioned C ABI with explicit neutral ownership/error/schema semantics and independently qualified language bindings, versus a message/component interface. A native-language API with semantics postponed until a port is a rejected shortcut. **Decisive falsifier for C5:** changing core language requires an external plugin rewrite or changes a grant outcome, canonical record, epoch fence or disabled trace within the approved contract. **Non-proof:** passing transcripts does not prove malicious-native-code isolation, universal binary portability, sandbox security or legal reuse.

## 6. Checklist item (vi): integrated acceptance is the invariant bar

**D [P4]:** the FU-03 synthesis calls all numbered completion requirements proposed until reconciliation and independent validation. It distinguishes reduced diagnostic bounds from `Integrated`, proposes 17 invariant monitors, 36 classified mutant rows with retained evidence and `K = RNE`, no surviving reachable non-equivalent or inconclusive row, a minimum 88 named byte rows plus mandatory expansions, scope-aligned Alloy checks and two genuinely independent implementations. Equivalent/unreachable rows are not kills. These are requirements/status statements, not newly observed success.

**D [P2, P4]:** RC-01…RC-09 remain reconstruction proposals. The integrated model's abstraction mapping is an obligation, not a proved theorem. Changes to byte format, refusal or digest domains must preserve the separate frozen M1 profile rather than silently adopting candidate RCV1.

**I:** increasing a diagnostic model's state bound cannot fix an absent monitor, missing transition, shared oracle, unobserved protected sink or unreconciled specification. A valid gate needs both predicate coverage and evidence provenance. No number of candidate-produced booleans establishes boundary behavior without independent observation. [P4, P6]

**Proposed replacement text (C6):**

> The invariant-checking acceptance bar is the reconciled integrated gate at the admitted bounds, with independent monitors, reachability/vacuity controls, retained mutation dispositions, scope-aligned cross-checks and exact independent byte/behavior conformance. Diagnostic model results remain evidence only within their declared scope; larger diagnostic bounds do not substitute for integration. Candidate FU-03 details and RC-01…RC-09 remain proposed until reconciled and accepted. No freeze or runtime qualification is claimed without the required receipts and Owner disposition.

**Competing option:** independently specified modular gates plus an explicit composition argument against the same accepted requirements and bounds; any substitution still needs the required reconciliation and disposition. Enlarging diagnostic bounds alone is a rejected shortcut. **Decisive falsifier for C6:** an invariant lacks an independent predicate/control; the integrated witness is unreachable; a required mutant survives or is inconclusive; independent stacks contradict normative bytes; or acceptance is asserted without disposition. **Non-proof:** passing bounded integration does not prove unbounded correctness, native durability, process containment, real effect truth or cheap reconstruction.

## 7. What blocks building versus what can remain a choice

This is **I**, a sequencing recommendation, not a new release decision. It preserves accepted comprehensive scope. The relevant sources are P2–P6.

### Build blockers for claiming a conforming reconstruction

1. An unclosed semantic seam that changes admission, authority, canonical bytes, refusal, effect uncertainty or recovery. Name the seam and accepted resolution; do not hide it in an implementation default.
2. No exact corpus/profile identity, missing literal expected bytes or unreviewed preimage rules. The digest algorithm's name cannot replace these.
3. No admitted toolchain/host identity or no independently reviewed observation boundary. Parser/host drift makes a comparison uninterpretable.
4. Missing reconciliation of candidate FU-03 requirements, integrated acceptance receipts, independence evidence or the reconstruction cost ceiling.
5. An outline that treats the P1/RC proposals as accepted, conflates generic JCS with strict M1B, reports 15 units without a fifteenth row, or declares an entire language unable based on a default serializer.

### Choices that may remain open in the architecture

Language, runtime topology, JSON/canonicalizer package, store engine, transport, FFI/plugin-host mechanism, internal data structures and build framework may remain variables **if** the observable contract and later qualification obligation are explicit. An open implementation choice is different from an undefined authority or byte rule. A future implementation task still needs its particular choices and boundaries admitted before it can run.

**Competing option:** a separately admitted, budget-bounded disposable prototype to expose seams while explicitly withholding semantic freeze and conformance claims. **Decisive falsifier:** the supposedly deferred choice changes accepted behavior or requires consulting original code to derive the missing architecture. **Non-proof:** closing these blockers does not authorize execution, alter protected contracts or settle resource ceilings.

## Completion and boundary

All six R-LANG checklist items are addressed with source facts, status boundaries, replacement text, competing options, falsifiers and non-proofs. This report adds one independently observed source-count correction. A bounded, provider-free verification design is in `local-verification-handoff.md`, explicitly **NOT RUN** and not a request to execute now.

No repository path was edited, target program/script imported or run, fixture executed, package installed, model/provider called, live host operated, session created, acceptance claimed or technology selected. No legal conclusion is made.
