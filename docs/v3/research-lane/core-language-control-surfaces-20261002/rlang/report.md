# Recursion core language and architecture reconstruction evaluation

Research date: 2026-10-02 UTC  
Repository: NotSkittlz/recursion at `699a29d622d1a1d3cecdaf3504cc5d649fb9d20e` (PR 159)  
Parent goal: `goal-recursion-v3-harness-orchestrator`  
Status: advisory research; all proposed target experiments unexecuted

## 1 Findings and decision boundary

**I/U:** The language question is now sufficiently specified to compare candidates, but the evidence does not select a winner. Every candidate can express the four-function deterministic boundary; none supplies Recursion authority, canonical ordering, durable acknowledgements or safe cancellation merely by being chosen. The strongest near-term discriminator is the complete, reviewed implementation of those contracts on the accepted Windows and Linux profiles, including its store wrapper and process broker. Source-documented mechanisms are available; Recursion performance, reviewed authoring cost, native recovery and distribution fit remain unknown.

The proposed evaluation shortlist preserves Rust, Go and the two CPython reference arms already named by FU-01. C#/.NET is the first additional Windows-service challenger; Java/Kotlin-JVM is a genuine managed-backend challenger. C++ and TypeScript/Node remain credible comparison options with explicit re-entry conditions, rather than being dismissed by reputation. This is a priority for gathering evidence, not a product-language selection or authorization to expand the experiment budget.

For D-039, the deliverable that matters is a versioned reconstruction kit: exact semantics and literal byte corpus, not an architecture diagram plus the first implementation. It must let a second team reconstruct the same behavior without reading the original reducer or asking its author to resolve missing rules. The kit's acceptance requires independent implementations, store and OS conformance, unchanged plugin contracts, and measured review/rework cost against a preregistered ceiling. No bound on that cost has yet been established.

### Evidence classes

- **D — source-documented:** a retrieved public specification/API/license, or a pinned repository requirement or recorded result. A source reporting a test is still a source report here.
- **L — locally observed static:** direct inspection of the exported pinned source, filenames or artifact content. No target program, import, benchmark, checker or fixture was executed in this research.
- **I — inferred/proposed:** product-specific implication, design recommendation or future test. Each recommendation below has a rival, falsifier and non-proof.
- **U — unknown/unverified:** missing measurement, unresolved implementation closure or unqualified profile. Unknown is never zero, unsupported, safe, cheap or passed.

External citations were accessed on 2026-10-02. Versioned links identify the documentation/release examined, not a certified deployment. Moving documents are identified in the source register. Historical runtime examples do not silently replace FU-01's freeze. References such as [R1] and [S1] resolve to the pinned repository or primary public sources at the end.

## 2 Current inputs and constraints

### Accepted boundaries

**D [R1], [R2], [R3]:** Recursion is one product and both a native harness and a harness orchestrator. Hermes is an optional upstream integration. The privileged kernel remains small, deterministic and surrounded by typed adapters; neither a JVM nor an interpreter is admitted or excluded by relabeling it “native.” A clarification of eligible topologies belongs in an experiment admission, not in this report. D-018, D-019, D-022, D-023, D-024, D-030, D-031, D-033, D-034, D-035 and D-039 are preserved.

**D [R3]:** D-028 fixes Windows 11 25H2 build 26200 x64 as primary, additionally qualifying 24H2 build 26100 Enterprise; Linux 6.12 LTS primary and 6.6 LTS minimum with cgroup v2; local NTFS/ext4 canonical storage. WSL2 is development/test only, with WSL2-ext4 and `/mnt/c` distinct mandatory cases. SMB/NFS, ReFS/xfs/btrfs and physical platter interruption are outside the first-release promise. “Survives power loss” means application-consistent replay after an abrupt VM reset at the commit boundary. These floors are fixed; qualification remains unknown.

**D [R3]:** D-035 externalizes plugin artifacts by digest while retaining the single core repository during R&D. D-037 permits Python disposable fixtures and does not choose the product language. D-038 resolves older “GUI later” timing in favor of built, opt-in GUI/mobile clients. D-039 accepts the mobile advice as evidence and commissions reconstruction; it does not adopt the mobile stacks. Nothing here renews a prior construction permission.

### What FU-01, FU-02 and FU-03 actually provide

| Input | Current source-backed status | Consequence for this evaluation |
|---|---|---|
| FU-01 | **D/U [R4]:** `EXP-FHAR-10` remains unexecuted. Historical arms include Rust 1.98.1, Go 1.27.1 and CPython 3.13.15 GIL/free-threaded separately. Later versions require a new freeze | Preserve those reference arms; do not merge version, JIT, GC or native-binding changes into one result |
| FU-02 | **D/U [R5]:** segmented JSON Text Sequences + CAS versus SQLite WAL + identical CAS; wrappers, fault exposure and crash/replay outcomes unmeasured | Language comparison must hold semantic commit unit and CAS obligations equal |
| FU-03 Session A | **D/L [R6], [R7], [R8]:** current source records 51 vectors, 11 instantiated mutants, 10 killed, M31 not killed within bound, 24 not yet implemented and one needing a larger bound; independent acceptance is diagnostic only | Reuse it as diagnostic input, not the complete integrated-bound or independent A/B acceptance |
| FU-03 Stage B | **D/L [R14], [R15]:** 26 synthetic fixtures at `e21cd75`: 16 bounded-consistent, five unsupported, three unknown, two blocked; independent bounded ACCEPT at `d56cc44` is recorded | Native reset/store and real-process cases remain blocked; no IT-02 acceptance, integrated freeze or external-effect qualification |
| FU-03 final gate | **D/U [R9]:** full corpus release, integrated-bound evidence, required mutant dispositions and independent implementation equivalence remain conditional | Resource ranking cannot substitute for missing conformance evidence; kernel freeze is not claimed |

There is a source-status defect worth preserving: Session A README lines 40–48 record independent diagnostic verification, while lines 67–69 still say no independent verifier rerun occurred. The dated verification result records the rerun at `74fb18dea58361a2d0f51f8aafd194659014cadf`; it is stronger evidence for that diagnostic scope. This research does not rerun it or upgrade it to integrated acceptance. [R6], [R7], [R8]

### Three byte contracts must remain distinct

1. **Existing M1B wire contract, D [R10]:** UTF-8 I-JSON and RFC 8785 canonicalization, with stricter admission: duplicate keys, invalid/non-shortest UTF-8, BOM, floats, negative zero and non-JSON constants rejected; exact fields, bounded depth/size and specified array ordering. Missing differs from null, booleans are not integer counts, and timestamps are evidence inputs rather than ambient clocks.
2. **FU-03 candidate interchange, D [R9]:** `RCV1` deterministic-CBOR framed vectors and domain-separated digests are a proposed test interchange, not the product transport or store format. The current diagnostic corpus is not automatically the complete released corpus.
3. **Public prior art, D [S1], [S2], [S3]:** JCS sorts property names by UTF-16 code units and does not normalize Unicode; CBOR has deterministic-encoding rules but permits application profiles; RFC 7464 defines JSON sequence framing. None independently specifies Recursion epochs, refusal precedence or authority.

**I:** Carry separate profile identifiers and adapters; never “simplify” by replacing frozen M1B bytes with CBOR, by allowing JCS floats at an M1B entry point, or by treating insertion order as canonical sort order. Rival: one new universal encoding. Falsifier for the separate-profile approach: independently reviewed vectors show the profile adapter cannot preserve required semantic distinctions without ambiguity. Non-proof: profile separation does not select a future serialization or prove its implementation correct.

## 3 Candidate comparison

### 3.1 Scope and version identity

The fair set includes Rust, Go, C++, C#/.NET, Java/Kotlin-JVM, TypeScript/Node and CPython. Python is an important reference because it is already in FU-01 and supplies disposable fixtures; fixture success is not product acceptance. Kotlin and Java share a runtime family but differ in compiler/stdlib/coroutine closure. GIL and free-threaded CPython are separate arms. JIT, AOT, embedding, cgo and process-split forms are separate topologies, not free switches within an arm.

**D [S4], [S5], [R4]:** Rust 1.99.0 was announced on 2026-10-01; Go's release record lists 1.27.1 on 2026-09-01. The Rust standard-library pages examined show 1.99.0. These facts identify the documentary evidence and a possible prospective freeze, not permission to alter Rust 1.98.1 in the historical experiment. Binary/archive hashes, compiler flags and native SDK contents remain **U** until a later admitted freeze. Reference versions for the added managed candidates are documented in section 3.5. They are deliberately historical documentation anchors; all seven candidates still require a prospective exact deployment freeze or an explicitly historical replication identity. No unsupported old patch is recommended for deployment.

### 3.2 Determinism and concurrency

Every cell below describes a mechanism or a risk. “Can express” is an inference about implementability, not observed conformance.

| Candidate | Canonical-byte hazard and required control | Single writer and cancellation |
|---|---|---|
| Rust | **D [S6], [S7]:** HashMap uses randomized hashing; BTreeMap iterates by key order. **I:** explicit profile comparator, checked arithmetic, strict duplicate-preserving decode and literal bytes still required; Rust string ordering is not a substitute for JCS UTF-16 ordering | **D [S8]:** Send/Sync express cross-thread safety conditions. **I:** one owned reducer plus bounded mailbox can serialize admissions; `unsafe`, FFI and competing processes remain outside that guarantee. Epoch recheck at durable commit and dispatch is mandatory; no drop/abort is a durable PAUSE receipt |
| Go | **D [S9], [S10]:** map iteration is unspecified; channels/locks establish synchronization and data-race-free execution has memory-model guarantees. **I:** ordered canonical traversal and explicit checked bounds still required | **I [S10], [R4]:** one goroutine may own state, but concurrent send arrival is not a deterministic business order. Cancellation signals must be followed by join/observation and epoch checks; a channel or mutex cannot fence a stale process |
| C++ | **D [S11], [S12]:** unordered-container order is not portable; unsynchronized conflicting accesses can be undefined behavior. **I:** explicit ordering, fixed-width integer bounds and lifetime discipline are required | **I [S12]:** single owner/queue, locks and atomics can implement the boundary, but race/lifetime defects may invalidate reasoning. Sanitizer-clean tests are bounded evidence, not proof. A crashed process does not run orderly cleanup |
| C#/.NET | **D [DN01], [DN03], [DN05]:** Dictionary order is undefined; overflow and culture-sensitive comparison need explicit policy. **I:** profile-owned ordering/encoding and fixed bounds | **D/I [DN06]:** bounded channels can feed one owner; channel synchronization does not fence another process. Cancellation is a request until durable fencing and join/observation complete |
| Java/Kotlin-JVM | **D [JV01], [JV03], [JV05]:** HashMap order is not guaranteed; fixed-width integer overflow does not signal; default locale comes from the environment. **I:** checked domain bounds and explicit comparator | **D [JV06]:** a single-thread executor serializes tasks but its queue is unbounded. **I:** bound admission separately and never use thread interruption as the authority receipt |
| TypeScript/Node | **D [ND01], [ND12]:** JS Map/Set preserve insertion order, Number is binary64, and TypeScript types disappear at runtime. **I:** raw-token duplicate/number checks precede object creation | **D/I [ND03]:** workers permit parallel/shared-memory execution; an event loop does not serialize other processes or durable commits. Await/callback order cannot determine canonical order implicitly |
| CPython | **D [PY01], [PY02], [PY07]:** dict preserves insertion order, sets do not; process hashes are salted; default JSON permits non-finite numbers. **I:** explicit bounds/sorting/strict decode | **D/I [PY04]:** GIL and free-threaded container locks are not a multi-step protocol. Keep those arms separate; one logical owner and durable epochs remain required |

**I, shared design [R11]:** `decode`, `admit`, `reduce` and `derive_work_packet` receive immutable state and admitted inputs, with no filesystem, process, network, plugin, provider, credentials, wall clock or random reads. Time, randomness and external completion order become explicit admitted data. Define a deterministic order for equal-priority work and refusal precedence. Diagnostic text, stack traces, GC timing, scheduler order, hash seeds and locale never enter canonical state.

The single-writer invariant has three separate boundaries: logical admission, durable mutation and effect dispatch. At each required boundary revalidate writer/lease/goal/plan/pause epochs and expected high water. A process mutex, the GIL, a database writer lock or an atomic variable can serialize a location; none alone proves current authority. Abrupt reset discards volatile state in every language. Recovery reconstructs receipts and uncertainty before any dispatch. **D/I [R5], [R11]**

### 3.3 Platform, durability and process fit

| Candidate | Documented file barrier and cross-platform route | Windows service and child control gap |
|---|---|---|
| Rust | **D [S13], [S14]:** `File::sync_all` requests content/metadata sync and surfaces errors; target documentation includes Windows x64 and Linux x64. **U:** exact NTFS/ext4 VM-reset behavior, libc/SDK and distribution closure | **D/I [S15], [S21], [S22], [S23]:** native extern/system bindings can reach Win32; service lifecycle, Job Objects, ConPTY and Linux cgroup/pidfd need explicit adapters. Standard child handles alone are not descendant containment |
| Go | **D [S16]:** `File.Sync` commits file contents to stable storage according to the OS implementation. **I/U [S17], [S21], [S22], [S23]:** Windows/Linux executable target support does not establish the accepted host profile; native SDK, cgo and process-group closure must be pinned | **I [S21], [S22], [S23]:** services/jobs/cgroups are adapter obligations; `CommandContext` termination of a child is not an independent tree-complete receipt. Race detached grandchildren and inherited handles |
| C++ | **D/I [S12], [S18], [S19]:** language stream flushing does not specify an NTFS/ext4 durable commit; use audited native file/VFS paths. Compiler, C/C++ runtime, Windows SDK, linker and Linux libc are part of the arm | **D/I [S21], [S22], [S23], [S24]:** direct native API access is available; lifecycle/handle ownership, breakaway policy, pseudoconsole shutdown and service control acknowledgements must still be implemented and tested |
| C#/.NET | **D [DN07], [DN08], [DN09], [DN23]:** Flush(true) reaches native sync paths; published OS support is distribution/build specific. **U:** actual qualified .NET/native closure | **D [DN10], [DN11]:** P/Invoke exposes native APIs; tree-kill completion does not prove descendant exit and inaccessible descendants may be skipped. **I:** independently observe the Job/cgroup |
| Java/Kotlin-JVM | **D [JV08], [JV21]:** FileChannel.force specifies a local-device persistence guarantee within its scope; published JDK OS configurations are distro/build specific. **U:** Temurin/distribution native-floor qualification | **D/I [JV09], [JV11]:** ProcessHandle state/descendants change asynchronously; JNI can bridge missing native controls. PID enumeration plus destroy is not atomic descendant containment |
| TypeScript/Node | **D [ND05], [ND06], [ND11]:** fsync/filehandle.sync are available; the pinned Windows libuv path uses FlushFileBuffers. Documented OS minimums are below D-028, but the exact profile is **U** | **D [ND04]:** killing a shell need not kill descendants. **I:** explicit native Job/cgroup/SCM boundary and drain behavior are required |
| CPython | **D [PY03], [PY12]:** os.fsync maps to Unix fsync/Windows _commit; buffered flush comes first. Windows/Linux build-tier support is documented; exact profile remains **U** | **D/I [PY05], [PY06]:** subprocess/ctypes expose process and native APIs, but Popen.communicate timeout does not automatically kill its child and GIL behavior does not establish containment |

**D [S18]:** Windows `FlushFileBuffers` operates on the supplied handle; its documented guarantees and error result must be respected. **D [S19]:** on Linux a file `fsync` does not itself make a new directory entry durable; directory persistence needs its own synchronization. **I:** neither a language's buffered flush, a rename nor successful close is a portable substitute for the complete commit protocol. Do not assume a Windows parent-directory handle plus flush has POSIX-equivalent semantics; the Windows publication path remains a qualification item.

**D [S21], [S22], [S23], [S24]:** Windows Job Objects manage associated process groups and accounting, with breakaway and membership caveats. Services use the SCM lifecycle. Linux cgroup v2 manages hierarchical resource groups; pidfds identify processes without relying only on a reusable PID [S46]. **I:** containment, credentials and authorization are separate properties. The broker must prove group membership, bounded queues, cancellation and exit observation; a process-exit observation never proves whether an external effect happened.

**U for all seven candidates:** native floor qualification, complete service install/update/rollback, p99/p999 latency, GC/allocator stalls, RSS slope, stable idle cost, all-bytes storage, reproducible release artifacts, dependency count, AI-assisted authoring/review cost and long-term maintenance cost. No undocumented advantage is encoded as a score.

### 3.4 Ecosystem and lifecycle

| Candidate | JSON, hashes, wire and embedded-store possibilities | Lifecycle risk and specific evidence to collect |
|---|---|---|
| Rust | **D [S25], [S26], [S27]:** serde_json supplies JSON values/serialization; sha2 supplies SHA-2; rusqlite binds SQLite. **U:** JCS and M1B strict-decoder conformance, schema/MCP/HTTP/PTY closure, complete transitive licenses | **D/I [S28], [S29]:** locked/offline Cargo modes support a frozen workflow; build scripts execute build-time code. Audit procedural/build dependencies and native link closure. **U:** reviewed productivity, binary reproducibility and allocator tails |
| Go | **D [S30], [S31]:** encoding/json and crypto/sha256 exist. **I:** default JSON acceptance/escaping is not proof of the strict wire profile; SQL driver and wire-schema closure remain explicit choices | **D [S17], [S32]:** cgo needs a C toolchain when cross-compiling; Go's native plugin facility supports only Linux/FreeBSD/macOS and has toolchain/dependency coupling. **U:** native closure and end-to-end review cost |
| C++ | **D [S33], [S34], [S35]:** nlohmann/json, OpenSSL digest APIs and SQLite C API are credible examples, not an adopted bundle. **U:** canonicalizer, duplicate detection, schema stack and exact ABI closure | **D/I [S12], [S36]:** compiler flags, implementation-defined behavior and sanitizer configuration must be pinned; audit every native dependency and lifetime boundary. **U:** review burden relative to Rust/Go, deployment size and performance |
| C#/.NET | **D [DN13], [DN14], [DN15], [DN24]:** System.Text.Json, SHA256, Microsoft.Data.Sqlite and JsonSchema.Net are candidate role providers. **U:** strict JCS/wire validation, schema version and native closure | **D [DN19], [DN21], [DN22]:** deterministic assembly inputs, deployment modes and GC are documented. **I:** self-contained/AOT are distinct arms; signed packaging, native closure, pauses and review cost remain **U** |
| Java/Kotlin-JVM | **D [JV13], [JV14], [JV16], [JV25]:** MessageDigest SHA-256, Jackson streaming JSON, SQLite JDBC and NetworkNT schema validation are role candidates. **U:** canonicalization, native artifacts and exact schema/driver versions | **D [JV22], [JV23]:** collector choice and custom runtime images are explicit variables. **I:** JDK/JNI/Kotlin closure and runtime-image updates must be counted; pauses, install size and review cost remain **U** |
| TypeScript/Node | **D [ND09], [ND10], [ND18]:** crypto hashes, Ajv and node:sqlite are available; v24.0.0 SQLite is active-development API with synchronous operations. **U:** strict canonicalizer and native engine closure | **D [ND15], [ND16], [ND17]:** locked installation and experimental single-executable packaging exist; GC is observable. **I:** lifecycle scripts, native addons and event-loop blocking require explicit controls; costs remain **U** |
| CPython | **D [PY07], [PY08], [PY09], [PY15]:** json/hashlib/sqlite3 and jsonschema cover distinct roles; these do not certify JCS or M1B admission. **U:** actual linked SQLite and extension closure | **D [PY10], [PY14]:** cyclic collection and hashed dependency installs are documented. **I:** interpreter/wheels/native extensions/GIL mode belong to identity; idle/soak, packaging and review costs remain **U** |

**I, lifecycle measurement:** record toolchain update effort, clean/offline rebuild success, SBOM and provenance completeness, signing/update/rollback work, cold diagnosis time, escaped defects, review minutes and rework. Compare code generation only as part of the same complete task through independent acceptance. Hiring availability and AI correctness are **U** for this team; popularity, lines generated and toolchains already installed are not substitutes. Rival: choose the easiest initial prototype. Falsifier: no later repair/review/upgrade penalty appears within a preregistered representative comparison and the simpler option wins on approved margins. Non-proof: one task cannot estimate long-term staffing or maintenance.

### 3.5 License evidence and unresolved distribution questions

License names below are documentary observations only. They are not legal opinions, compatibility findings, redistribution approval or a CVE-cleanliness claim. A language has no single license covering every compiler, SDK, runtime, linker and dependency it might use. Pinning these examples does not resolve an application closure.

| Candidate or dependency | Actual primary text retrieved | Remaining question |
|---|---|---|
| Rust reference toolchain | **D:** Rust 1.98.1 MIT and Apache-2.0 texts [L1], [L2] | Which compiler/runtime components, LLVM material, SDK and redistributed libraries enter the actual artifact? |
| Go reference toolchain | **D:** Go 1.27.1 BSD-style license [L3] | Which cgo/native components or bundled system libraries add separate obligations? |
| C++ reference compiler | **D:** LLVM 20.1.8 Apache-2.0 text with LLVM exceptions [L4] | Which standard library, linker and Windows SDK/MSVC redistribution terms apply? MSVC is not covered by the LLVM license |
| C#/.NET, Java/Kotlin, Node/TypeScript, CPython | **D:** .NET runtime 8.0.0 and moving Roslyn MIT [DN17], [DN18]; OpenJDK 21+35 GPL-2.0 with Classpath exception and component notes [JV18], [JV19]; Kotlin 2.1.0 Apache-2.0 [JV20]; Node 24.0.0 license bundle [ND13]; TypeScript 5.8.3 Apache-2.0 [ND14]; CPython 3.13.0 PSF/historical bundle [PY11] | Which exact distribution/patch/native components are shipped? These are reference texts, not current security-approved deployment pins |
| serde_json 1.0.145 | **D:** MIT and Apache-2.0 texts [L5], [L6] | Feature and transitive dependency closure? |
| sha2 0.10.9 | **D:** MIT license text retrieved [L7] | Exact selected feature/assembly/backend and complete notices? This entry does not infer an unexamined license alternative |
| rusqlite 0.37.0 | **D:** MIT license text [L8] | Bundled versus system SQLite, compile flags, wrapper dependencies and notices? |
| nlohmann/json 3.12.0 and OpenSSL 3.5.0 | **D:** MIT and Apache-2.0 texts respectively [L9], [L10] | What is actually redistributed and which optional providers/dependencies are included? |
| Managed ecosystem examples | **D:** Microsoft.Data.Sqlite MIT [DN16]; JsonSchema.Net/json-everything MIT [DN25]; Jackson core Apache-2.0 [JV15]; SQLite JDBC Apache-2.0 [JV17]; NetworkNT validator Apache-2.0 [JV26]; Ajv 8.17.1 MIT [ND19]; jsonschema 4.23.0 MIT [PY16] | Moving branches and unresolved exact package versions cannot qualify a frozen closure; wrapper and engine notices remain separate |
| SQLite | **D:** upstream public-domain statement and its stated caveats [L11] | Does the actual package/wrapper/extension add obligations, and is that evidence sufficient for the distribution jurisdiction? |
| Wasmtime example host | **D:** moving upstream license text, Apache-2.0 with LLVM exceptions [L12] | Exact engine/binding release and full runtime closure are **U**; no binary admitted |

Unresolved license and distribution questions remain in the existing G-32/counsel-gated lane. This research contacted no counsel and made no legal conclusion.

## 4 Shortlist and rejection rules

These are **I** recommendations to prioritize later evidence. All resource advantages are **U**. A falsifier rejects an exact arm/profile or the prioritization hypothesis, not a programming language for every possible use.

| Priority and option | Rationale and real rival | Decisive falsifier | What it does not prove |
|---|---|---|---|
| Retain Rust core + typed adapters in FU-01 | Ownership and explicit native boundary make a useful contrast to Go/CPython [S8], [S15], [R4]. Rival: Go owner-loop or C++ native kernel | Accepted byte/refusal mismatch, stale epoch crossing an unsafe/FFI path, failure to qualify required OS adapter, or failure to meet approved review/resource ceilings | No proof Rust makes all code safe, removes allocation stalls or reduces review cost |
| Retain Go owner-loop backend/core arm | Channels and OS/file APIs provide a different concurrency/runtime design [S10], [S16]. Rival: Rust and .NET | Goroutine or child surviving a claimed completed fence; GC/resource ceiling breach under the same workload; unusable native-library closure | No bound on GC pauses, no deterministic arrival ordering, no single-binary guarantee with cgo |
| Retain CPython GIL and free-threaded reference arms | Preserves the existing FU-01 counterfactual and tests the claim that optimization needs another runtime [R4]. Rival: Rust/Go | Strict bytes fail; FT identity silently re-enables GIL; unsafe extension sharing; accepted latency/memory/review ceiling breached | Disposable-harness authorization is not product adoption; quick fixture writing is not whole-lifecycle cheapness |
| Add C#/.NET as first expanded challenger if budget is admitted | Windows integration and explicit managed deployment alternatives make it decision-relevant [DN11], [DN21]. Rival: Go or Rust | Required service/Job/flush behavior unavailable in the frozen topology; Native AOT/native binding incompatibility; documented floors cannot be demonstrated; GC/closure ceiling breached | No Windows qualification, managed safety across native FFI or smaller distribution claimed |
| Keep Java/Kotlin-JVM as managed-backend challenger | JVM primitives offer an independent managed comparison [JV08], [JV11], [JV23]. Rival: .NET/Go | Native process/store wrapper cannot meet the contract; startup/footprint/review ceilings fail; runtime/image identity cannot be reproduced | No assumption Java 21 availability proves best fit; Kotlin conveniences do not supply fencing |
| Keep C++ as native challenge arm | Direct OS/library access and independent implementation semantics matter [S12], [S36]. Rival: Rust | Uncontrolled UB/ownership defect, wire divergence, unbounded unsafe review or missing reproducible ABI closure | Deferral is not a measured maintenance disadvantage. Re-enter if reviewed equivalent code meets floors and improves a preregistered objective |
| Keep TypeScript/Node as backend/control-plane challenge arm | Useful comparison when orchestration dominates CPU and the strict kernel can be isolated [ND03], [ND04]. Rival: Go/.NET; monolithic Node versus separately fenced pure core | Input precision or duplicate collapse breaks bytes; synchronous store/CPU work blocks control deadlines; workers/helpers hide cost or violate fencing | No blanket “too slow” result. Re-enter if full-group resource and review evidence meets all floors |

The retained experiment roster and the expanded research shortlist are separate. A new language or topology gets its own identity, implementation budget, reviewer and paired cells. If the Owner declines expansion, the original FU-01 comparison remains intact. “No selection” is an acceptable final outcome when floors, intervals or tradeoffs are unresolved.

## 5 Store and replay fit

### The unit that must survive

**D/I [R5], [R11]:** an acknowledged mutation binds ordered canonical events, state/high-water, command receipt, authority epoch, outbox changes and referenced immutable artifacts. Staging an artifact before the transaction is safe only if lifetime/GC pins and recovery make the reference set coherent. A lost acknowledgement is an uncertain outcome: retry by the same command identity, inspect the durable receipt and reconcile effects; never manufacture a new effect attempt because the caller timed out.

**D [S37], [S38]:** SQLite WAL is a multi-file operating mode with writer/checkpoint interactions; FULL synchronization adds commit synchronization relative to NORMAL. **I:** commit success is not proof that external CAS content, wrapper policy or stale-writer fencing is correct. The same independent oracle must judge both candidates.

| Store candidate | Proposed comparison identity and responsibilities | Real rival and decisive falsifier | Non-proofs |
|---|---|---|---|
| Segmented append-only sequence + CAS | **D/I [R5], [S3], [S19]:** retain JS1 profile: complete frames, group commit record, event/receipt binding, short-write loop, file barrier, durable segment/manifest publication, replayable index and shared CAS. Windows publication path separately qualified | Rival: SQLite WAL + identical CAS. Falsified by any witnessed acknowledged complete group lost/changed after an in-scope reset, accepted interior corruption, stale writer mutation or dangling acknowledged CAS reference | Framing and a checksum do not establish transactions, fencing, authentication, rollback protection or correct repair |
| Embedded relational WAL + CAS | **D/I [R5], [S37], [S38]:** retain SQL1 documentary baseline SQLite 3.53.4, verified WAL/FULL, explicit checkpoint policy, `busy_timeout=0`, explicit VFS, single DB transaction containing canonical group/receipt/outbox/reference changes and atomic epoch/high-water check. Exact wrapper/native build remains **U** | Rival: JS1. Falsified by acknowledged-history loss/divergence, stale writer mutation, WAL/reset/checkpoint oracle failure, or CAS mismatch under the same fault domain | ACID does not prove epoch authority, external-effect truth, complete backup, portability or superior performance |

Version 3.53.4 and the historical WAL-reset regression controls are inherited comparison identities from FU-02, not a fresh security certification. A future session must resolve official artifact hashes and the exact regression fixture before executing. Secondary stores remain conditional branches of FU-02; this report does not introduce an unreviewed third finalist or reopen multi-node authority.

### Language-to-store binding

| Shortlist arm | Append-only design | Embedded WAL design | Decisive binding test |
|---|---|---|---|
| Rust | **I [S13]:** explicit fallible sync and publish adapter | **D/I [S27]:** rusqlite/native SQLite is a candidate bridge | Kill/reset between final CAS publication, durable receipt and ACK; inspect panic/FFI error translation |
| Go | **I [S16]:** explicit File.Sync and Windows/Linux publish adapters | **I [S17]:** native cgo versus non-cgo engine/binding must be separate identities, neither selected | Same transaction semantics and full-group resource accounting; no hidden busy/retry loop changes the workload |
| CPython GIL/FT | **I [PY03]:** flush buffering before OS barrier; hold one logical owner independent of GIL | **D/I [PY09]:** sqlite3 runtime may bundle a different native SQLite than the language release suggests | Record the actual native engine/VFS/options; FT extension safety and interrupted transaction outcomes must be observed |
| C#/.NET | **I [DN07], [DN08], [DN09]:** Flush(true) path and native publication audit | **D/I [DN15]:** managed wrapper + native SQLite are one measured closure | Crash at managed/native exception, flush and ACK seams; never count only managed heap |
| Java/Kotlin-JVM | **I [JV08]:** FileChannel.force with explicit metadata/publication boundary | **I [JV11], [JV16]:** JDBC/JNI binding and native store must be pinned separately | Interrupt/exception around native commit; verify exact engine/VFS and receipt recovery, not only Java return values |

C++ and Node use the same semantic oracles if admitted: native C API/OS calls for C++, and explicit fs barriers plus a frozen native SQLite integration for Node. No language-level convenience wrapper changes the durable commit definition.

### Required failure observations

**I [R5], [S39]:** pre-admission, post-admission/pre-mutation, mid-frame, post-data/pre-barrier, post-barrier/pre-ACK, CAS publish, manifest replace, checkpoint/rotation and recovery cuts are distinct cases. Include short/torn writes, disk-full/I/O error, stale WAL/SHM, interior corruption, missing CAS, stale epoch resumption and duplicate conflict. Retain the independent ACK witness and compare exact ordinal/event/state bytes, receipts, outbox and artifact closure. A valid no-fault positive control and a fault-sensitive negative control must precede every family.

The required destructive qualification is the accepted disposable-VM reset domain. Process kills are useful but weaker. No physical power-cut or network-storage cell is silently promoted back into D-028 scope. Native Windows and native Linux qualify separately; WSL2 outcomes cannot qualify either native production profile. Backup/restore is a coherent canonical cut plus its CAS references, followed by independent replay and reacquisition of authority, not simply copying the main database file.

## 6 Plugin host and language-independent boundary

### Competing host mechanisms

| Mechanism | Documented capability and product inference | Falsifier and non-proof |
|---|---|---|
| Process/IPC protocol | **I [R10], [S21], [S22], [S23], [S24]:** separate address spaces and language-neutral messages fit external digest-bound artifacts; versioned framing/backpressure and native containment adapters remain necessary. Real rival: WASM component | Falsified by escaped descendant, undeclared handle/credential access, stale request accepted after revoke or failure to bound queues. Process separation alone is not a sandbox or D-029 credential boundary |
| WebAssembly component or module host | **D [S40], [S41], [S42], [S43]:** imports/exports bound interaction; WIT defines interface contracts rather than behavior; Wasmtime documents deterministic-import, NaN and relaxed-SIMD caveats. **I:** a denied-by-default host can expose a narrow capability surface. Rival: process host | Falsified by host-import authority bypass, unbounded execution/memory, binding feature mismatch or disabled instantiation. WASM does not prove host safety, deterministic imports, syscall isolation outside the engine or portable full WASI support |
| Native FFI/in-process trusted extension | **D [S15], [S17], [S12]:** C/system ABI paths are available, while Rust's native ABI is unstable and cgo adds native-toolchain constraints. **I:** useful only under explicitly admitted trust and lifetime rules. Rival: process boundary | Falsified by arbitrary core mutation, callback/reentrancy across admission, allocator/unwind mismatch or unload dangling reference. ABI compatibility is not memory isolation, capability confinement or cross-language behavioral compatibility |
| Remote protocol | **I [R10]:** same logical envelope can span a remote host if authentication, authorization, replay, loss and ordering are independently specified. Rival: local process | Falsified by reconnect resurrecting a revoked grant, unauthenticated epoch takeover or duplicate effect. A network protocol is not a new authority model and does not resolve G-06 |

**D [S44]:** Wasmtime's core repository supports Rust/C/C++ embeddings; other bindings have their own release/support cadence, and C API feature parity may lag Rust. **I:** this is an integration-closure question for Go/.NET/Python/JVM, not evidence that those languages cannot host WASM. Node's built-in WebAssembly support is likewise not automatic WASI/component-model parity; its v24.0.0 WASI documentation explicitly disclaims secure sandboxing [ND08]. Compare process-host control with the exact engine/binding closure before favoring an embedding.

### D-035 contract that must not leak the implementation language

**I, recommendation P1:** preserve the existing M1B records and extend only through reviewed versioned specifications: manifest/artifact/dependency-lock digests; protocol/schema epochs; explicit trust/isolation mode; separately scoped grants; resource envelope; lifecycle; command identity; expected state; goal/plan/writer/lease/pause/subscription epochs; structured result/refusal; credit/cursor; and trace provenance. No contract passes native pointers, object addresses, runtime type names, stack traces, language exception classes, host callbacks or compiler-mangled symbols as authority. [R10]

The manifest describes requested capability; it does not grant it. Installation, enablement, activation and current grant validity remain different states. Reload must drain and fence old packet/lease/subscription epochs before activation of the replacement digest. Runtime discovery must inspect only permitted metadata for a disabled artifact: no implementation import, payload scan, process/thread/timer/watcher, network/credential/quota/spend work, cache write, context injection, tool registration, callback or subscription. The existing `DisabledActivityTrace@1` is the test contract, not a promise inferred from a UI toggle. **D [R10]**

Rival to P1: language-native plugin API with adapters added during a port. Decisive falsifier: an unchanged external plugin cannot pass the same reviewed transcript corpus against both core implementations, or disabled-plugin activity differs because loading/introspection runs code. Non-proof: stable messages do not establish least privilege, safe third-party code, cheap porting or legal reuse. WIT can describe the shape of an option, but behavioral rules and epoch tests still have to be written.

## 7 Language-agnostic reconstruction specification

This is **I**, a concrete outline for the versioned kit required by D-039, grounded in the existing four-function, wire, release and context contracts [R10], [R11], [R12], [R13]. It is a specification proposal, not a new accepted format, schema epoch or stack.

| Neutral specification unit | Required content and reviewable output | What may remain implementation-specific |
|---|---|---|
| Authority and topology | Exact principal/role model; kernel/adapters/clients boundaries; immutable accepted decisions; trusted launch assumptions; forbidden ambient inputs | Module directories, dependency injection, internal class names |
| State and transitions | Complete typed state; initial state; every command/event/transition; guard precedence; no-op/idempotent/refused outcomes; pre/postconditions; atomic multi-event groups; explicit time/entropy inputs | Private data structures, persistent collection strategy, allocation |
| Invariants and abstraction | AuthorityScope, Lease/Amendment/Pause fences, command idempotence, AtomicAppend, chain/replay, cancellation monotonicity, verifier independence, effect uncertainty, bounded flow and projection parity; monitor inputs and model-to-trace map | Scheduler/thread/executor choice, provided observations meet the same contract |
| Four-function API | Exact input/output algebra and ownership semantics for `decode`, `admit`, `reduce`, `derive_work_packet`; no I/O or ambient authority; typed failure versus adapter uncertainty | Language signatures, result/exception representation behind the adapter |
| Schemas and wire | Field names/types/bounds, unknown/missing/null/boolean rules, Unicode/number domains, ordering, duplicate rejection before collapse, version negotiation, epoch policy, maximum frames/depth | Parser library and internal storage, if exact rejection/bytes match |
| Canonical bytes and digest domains | Literal serialization grammar, comparator, escaping, length, encoding, domain strings and schema binding; retained bytes plus digest; test-interchange versus production-wire distinction | Vector materialization tooling, never self-generated expected values from the candidate |
| Refusal alphabet | Stable code and detail enum, schema epoch, field/path rules, precedence when several faults apply, semantic versus decode-frame versus adapter errors | Localized human diagnostics and stack traces, excluded from equality |
| Concurrency and fencing | One admitted mutation owner; atomic expected-state/epoch recheck; takeover order; durable high-water; pause/stop transition and completion meanings; bounded mailboxes; retry/cancellation joins | Lock/channel/actor design and runtime cancellation primitives |
| Persistence and recovery | Complete commit unit, acknowledgement point, uncertain outcomes, CAS publication/retention, checkpoint/segment rules, crash cuts, fail-closed recovery, backup closure and rollback constraints | Engine/VFS, file layout, platform barrier implementation, within qualified profiles |
| Effects and adapters | Durable intent/outbox, dispatch permit, observed completion/reconciliation, no blind reissue after uncertainty; external outcomes do not create authority | Provider/harness transport, process launcher, trusted OS isolation implementation |
| Plugin and edition interface | P1 contract, immutable digests, grant dimensions, lifecycle and disabled traces; client capabilities remain optional projections over backend authority | Language SDKs, OS-native client renderers, host mechanism after separate acceptance |
| Context and composition | Immutable goal/plan/evidence/config and authority bindings; one compaction governor per route epoch; exact non-droppable references; invalidation and projection order | Cache implementation and adapter formatting that preserve the declared output |
| Release and build identity | `ReleaseId`, boot receipt, source/config/dependency/toolchain/SDK digests, SBOM, provenance, build instructions, environment normalization and migration compatibility | Compiler/linker/build/package tools and signed distribution envelope |
| Evidence package | Literal vector manifest, expected observations, independent oracle identity, held-out/adversarial cases, trace schema, fault receipts, coverage and unresolved classes | Internal test runner, provided it cannot rewrite expected data or hide failed/censored observations |

**D [R11]:** the four-function API already prohibits the I/O and ambient-time behaviors listed above. **D [R12]:** `ReleaseId` includes product/protocol/schema identity, policy digest, core artifact, dependency lock, provenance, SBOM and edition manifest digests; core-only boot must expose state/replay with optional capabilities absent. **D [R13]:** context caches cannot refresh authority, and summaries cannot replace the retained source of accepted directives. Reconstruction preserves these facts independently of language.

**I, recommendation A1:** author the missing neutral pieces as small, cross-linked normative units with exact examples and an explicit unresolved-seam register. Rival: treat the first implementation plus generated bindings as the specification. Decisive falsifier: a second team must infer an unrecorded guard, order, byte rule or recovery policy from the original source to pass. Non-proof: a complete kit still needs empirical cost and equivalence evidence; generated bindings from one template do not make two implementations independent.

## 8 Reconstruction acceptance criteria

“Bounded” means a ceiling fixed before reconstruction on human implementation/review/rework time and allowed scope, not an assertion that the rewrite feels small. Numeric ceilings are **U** and reserved for experiment admission. The following **I** gates operationalize A1; they do not declare success now.

| Gate | Concrete evidence required | Decisive falsifier |
|---|---|---|
| RC-01 specification closure | Immutable kit manifest; accepted semantics/epochs; every unresolved item either resolved or excluded from a named claim before work starts | Implementer needs undocumented author knowledge, live original-code consultation or post-outcome semantic invention |
| RC-02 exact behavior | A bytes/results = B bytes/results = reviewed normative literal bytes/refusals for all required vectors and expansions; retained inputs/outputs, not hashes alone | One unexplained byte, ordinal, refusal precedence or state divergence |
| RC-03 implementation independence | Authorship/dependency attestations; no shared reducer, serializer, decoder registry, generator/template or expected-output generator; any permitted shared hash primitive disclosed with independent known-answer tests | Two languages wrap the same semantic implementation, or expected bytes are emitted by the candidate under test |
| RC-04 model bridge | Same accepted state model and abstraction; integrated-bound/mutant evidence and valid adversarial trace-checker controls | Lost invariant, unreachable required witness disguised as pass, unexplained model/oracle disagreement or monitor edited to fit candidate |
| RC-05 OS/store equivalence | Same acknowledged groups, receipts, canonical high-water and CAS closure across required native profiles and VM-reset cuts | Lost acknowledged group, stale mutation, unaudited barrier or corruption silently repaired into different history |
| RC-06 plugin/client compatibility | Unchanged external artifact digests and protocol-version transcripts pass on both cores; disabled and reload traces match; shared client contracts retain authority semantics | Core language change forces plugin rewrite, changes grant semantics, hydrates stale authority or starts disabled code |
| RC-07 rebuild and release | Two clean offline builds under declared environment; artifact identity policy; full native/managed closure; ReleaseId and boot/recovery receipts | Missing native binary, nondeterministic unexplained output or optional plugin required for core-only recovery |
| RC-08 bounded cost | Contemporaneous implementation/review/rework and clarification ledger; same scope, acceptance and workload; all hidden repair included | Accepted cost exceeds preregistered ceiling, required behavior removed to fit budget or substantial architectural re-derivation occurs |
| RC-09 migration safety | Canonical export/import or compatible persistence proof, shadow replay, one fenced cutover, rollback restrictions and unresolved effects retained | Dual writers, changed history, duplicated uncertain effect or rollback erases an already real/ambiguous effect |

Rival to this gate set: ordinary regression tests plus a migration checklist. Falsifier for the stricter reconstruction proposal: a preregistered independent comparison shows no decision-relevant additional defect detection or reduction in reconstruction uncertainty at its approved review cost. Non-proofs: finite vectors and bounded models do not establish unbounded correctness, compiler soundness, all future plugin compatibility, physical power-loss durability or universally cheap porting. D-033 remains a separate hypothesis for importing existing ecosystems.

## 9 Provider-free comparison protocol

**Entirely unexecuted.** This section is a local-session handoff design; no executable implementation or benchmark is supplied. It preserves FU-01's hard-floor-first ordering and FU-02's paired store comparison. Public method evidence supports replication and uncertainty handling, not a predicted winner. [R4], [R5], [S45]

### Admission and immutable identity

Before execution, record the Owner-approved purpose (historical replication or prospective refreeze), exact arms/topologies, accepted corpus/refusal profile and reviewer. Pin source, compiler/runtime distribution/patch, JIT/AOT/GC flags, native SDK/libc/VFS and every dependency; actual CPU/VM/storage/cache configuration; OS edition/build or kernel/distribution; cgroup/job membership; locale/time zone; environment; workload generator; instrumentation and analysis script digests. Any change makes a new experiment identity. No toolchain or package installation is authorized by this packet.

Freeze product ceilings, practical superiority/noninferiority margins, maximum repetitions/wall-clock exposure, valid fault domains, allowed repair/retry count, experiment resource envelope and stopping rule before observing confirmatory results. Missing thresholds block selection; they are not filled with arbitrary numbers. The existing E-FHAR2-102 workload is a documented baseline: one goal, 10 plan revisions, 1,000 tasks, 3,000 edges and 10,000 events; scale case 10 goals, 10,000 tasks, 100,000 events. Its build/execution counts and FU-01's larger tail/soak proposal are different designs to reconcile prospectively, not pooled outcomes. [R4], [R11]

### Workload and arm matrix

| Family | Synthetic workload and comparison | Primary observations |
|---|---|---|
| W0 boot/closure | Offline core-only boot, all optional artifacts absent; installed-disabled population; cold/warm process starts | Boot receipt, imported dependencies, handles/threads/wakes, elapsed start, whole-group RSS, forbidden activity |
| W1 canonical kernel | All released valid/invalid decode/admit/reduce/packet vectors, insertion-order permutations and adversarial Unicode/integer boundaries | Exact bytes/refusal/state, per-operation CPU/elapsed/allocation; failure before performance ranking |
| W2 writer/fences | Contended synthetic commands, duplicate equal/conflicting IDs, stale writer resume, goal/plan/lease/pause changes at every seam | Linearization order, refusal and no unauthorized mutation/dispatch; bounded queues and join observations |
| W3 replay/store | Identical log/groups/CAS on JS1 versus SQL1 at matched acknowledgement/batching semantics | O1–O5 replay/fencing/duplicate/CAS/structural oracles, ACK witness, all-bytes write/storage amplification |
| W4 process/plugins | Synthetic echo/CPU/blocked/descendant stand-ins; process versus exact WASM/native topology if admitted; reload and disabled artifacts | Full-group resources, native crossings/copies, containment, backpressure and stale epoch rejection |
| W5 views/context | Slow/multiple detachable views; deterministic context projection; revocation during cached continuation | Control ACK/append/view latency separately, projection bytes, dropped/coalesced presentation data, no canonical blockage |
| W6 steady/growth/recovery | Fixed logical population versus controlled growth; repeated recovery, compaction and checkpoint cycles | RSS/disk slopes per time and per event, GC/allocator pauses, handles, backlog, recovery time, renewed hard floors |
| W7 reconstruction/lifecycle | Independent reimplementation and one pinned dependency/OS upgrade task, equal completed scope | Human review/rework/diagnosis time, clarification count, escaped defects, offline rebuild and unchanged plugin evidence |

No real provider/model/account/credential is present in these workloads. Synthetic stand-ins do not establish provider cache, quota, subscription efficiency, task intelligence or route qualification.

### Oracles and hard floors

The oracle is independent of the candidate and retains literal expected outputs. The diagnostic Session A oracle is useful input but requires its declared gaps to be reconciled; copying it into every arm would defeat independence. Require byte/refusal equality, command idempotence, current authority and all fences, PauseSafety, expected-state semantics, replay fail-closed/parity, capability/privacy isolation, bounded ownership, offline closure, release evidence, honest diagnostics and recovery correctness before any resource ranking. Repeat floors after soak and every artifact change. **D/I [R4], [R6], [R9]**

For each test family include a valid positive control, deliberate adverse mutation and an independent observable. A malformed injector invalidates a trial; it does not convict the candidate. A silent/failed monitor makes the outcome inconclusive, never passed. Record attempts, refusals, generator lateness, censored timeouts, unfinished work and missing counters instead of deleting them.

### Measurement and fair analysis

**I:** pair arms on the same declared host/workload cell and randomize or counterbalance order across independent process/machine blocks. Separate cold from warm/JIT-stabilized states; do not discard warmup without an advance rule. Use externally offered arrivals with intended-send, actual-send, admission, durable-ACK and presentation timestamps so a stalled candidate cannot suppress its own offered load. Retain sequential human-control workloads as another stratum.

Report p50/p95/p99 and, only with sufficient preregistered exposure, p999 latency in time per scheduled operation; accepted operations/second with refusal counts; CPU time per accepted event and per offered command; peak whole-group resident/private memory and steady/growth slope; cold artifact/install bytes; dependency counts; logical and allocated DB/WAL/SHM/temp/CAS/quarantine/backup bytes; read/write bytes per committed group; GC/allocator distributions; FFI calls and copied bytes per operation; review/rework minutes per independently accepted task. Linux uses verified cgroup subtree membership; Windows uses verified Job Object membership plus declared omissions. Do not sum per-process maxima or double-count shared RSS.

Choose replication and confirmatory sample size from a separately recorded pilot, practical margins and desired precision. Preserve block/run-level dependence in intervals; millions of events in one VM do not create millions of independent host replications. Report paired effects and simultaneous uncertainty where many primary cells are compared. Insufficient tails, autocorrelation, unobserved counters or confidence intervals crossing the practical margin yield **U/inconclusive**, not a tie at zero. Kalibera/Jones provides a primary methodological rationale for accounting for multiple variation levels; it supplies no Recursion sample size. [S45]

Decision order [R16]: reject hard-floor violations; exclude incomplete essential evidence from ranking; check every approved ceiling; then report Pareto/noninferiority tradeoffs and the accepted responsiveness-first preference without a weighted composite. A proposed winner needs a practically relevant improvement on a declared objective without hiding regressions. Unresolved tradeoffs remain the Owner's decision. Stop on safety counterexample, invalid identity, missing independent witness, unintended target/non-synthetic data, unapproved destructive action, budget/deadline exhaustion or preregistered futility. Preserve evidence before repair and refreeze repaired artifacts; never keep testing until a favorable result appears.

### Threats to validity and evidence that would change the shortlist

- **Toy workload/gaming:** copying the oracle, precomputing vectors, omitting malformed input or offloading work hides cost. Retain held-out variants, complete operation semantics and group accounting. A genuinely superior full-workflow challenger changes priority.
- **Local environment bias:** existing Rust/Go/Java/Python/Node installations do not make a deployment reproducible. Native Windows and Linux outcomes, SDK/licensing closure and offline rebuilds can overturn any documentary preference.
- **Runtime tuning bias:** compare equal semantic guarantees; expose GC/JIT/warmup/cgo/native-store choices as separate arms. A tuned arm is not the old experiment.
- **Authoring and AI bias:** counterbalance implementation order, experience and task variants; measure review, correction, failures and later repairs, not generated LOC or first compile. Do not infer a labor advantage without this evidence.
- **Fault-oracle bias:** a process kill is not a VM reset; a clean reopen is not recovered ACK parity; a fault that never triggered is not a passed recovery test. Sensitivity controls can invalidate an apparent winner.
- **Source/vendor bias:** documentation identifies supported primitives and constraints. Marketing, language reputation, model agreement and unrelated benchmarks establish no local advantage.
- **Portability overclaim:** one successful language pair or finite corpus cannot predict every future change. A failed RC gate or reconstruction overrun narrows or falsifies D-039's bounded-cost claim for that change.

## 10 Unexecuted local-session handoff

The companion `local-session-handoff.md` identifies exact prerequisite evidence, test vectors and stop conditions. It is ready for Owner review, not an instruction to open a session or run tests now. The next evidence-producing action is to release the accepted FU-03 contract/corpus and freeze a bounded comparison identity, including missing authority, resource and recovery details. No decision is needed merely to read this report.

## 11 Non-proofs and completion

This report establishes a source-grounded comparison, conditional shortlist, architecture outline and falsifiable reconstruction/test criteria. It establishes no measured performance, memory/GC advantage, maintenance saving, hiring advantage, bounded port cost, complete dependency/security clearance, accepted runtime, storage/serialization/sandbox/transport/provider selection, integrated kernel freeze, native platform qualification or qualified route. Source-export inspection is not target execution. Existing recorded diagnostic verification is acknowledged at its actual scope.

Research output was written outside the exported repository; protected paths were not edited. Only public technical queries were sent to web research, with no private repository excerpts, user data, credentials or host identifiers. Artifact checks and independent review status are recorded in the packet's QA evidence; report completeness does not mean any target gate passed.

## Sources

All entries accessed 2026-10-02 UTC. Versioned source tags are documentary pins; moving pages require archive/hash capture at an experiment freeze. No runtime binary was downloaded or qualified. The supplementary ledger records evidence classes and per-page paraphrase allowances.

- **R1** [Operative governance](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/CONTROL_PLANE.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R2** [Mission and accepted boundaries](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/ANCHOR.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R3** [Accepted D018 through D039 decisions](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/state/DECISIONS.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R4** [FU01 experiment synthesis](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/followups-20261002/synthesis/FU-01-language-runtime.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R5** [FU02 store replay synthesis](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/followups-20261002/synthesis/FU-02-store-crash-replay.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R6** [Session A diagnostic status and stale sentence](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-kernel-session-a/README.md#L22-L69). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R7** [Recorded independent diagnostic verification](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-kernel-session-a/verification/results/74fb18d-muse-spark-1-3.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R8** [Oracle scope and limitations](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-kernel-session-a/oracle/README.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R9** [FU03 candidate corpus and acceptance obligations](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/followups-20261002/synthesis/FU-03-formal-model-vectors.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R10** [Frozen M1B wire plugin and process contract](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/M1B-WIRE-PROFILE.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R11** [E-FHAR2-102 four-function kernel and experiment](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md#L97-L134). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R12** [E-FHAR2-108 release and boot contract](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md#L339-L390). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R13** [Context authority and deterministic projection](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/CONTEXT.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R14** [Stage B bounded source-recorded run](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-stage-b/README.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R15** [Stage B recorded independent bounded ACCEPT](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-stage-b/verification/results/d56cc44-muse-spark-1-3.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **R16** [Canonical performance decision rule](https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/PERFORMANCE-PLAN.md). 699a29d622d1a1d3cecdaf3504cc5d649fb9d20e
- **S1** [RFC 8785 JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785). RFC 8785 June 2020
- **S2** [RFC 8949 CBOR](https://www.rfc-editor.org/rfc/rfc8949.html). RFC 8949 December 2020
- **S3** [RFC 7464 JSON Text Sequences](https://www.rfc-editor.org/rfc/rfc7464.html). RFC 7464 February 2015
- **S4** [Rust 1.99.0 announcement](https://blog.rust-lang.org/2026/10/01/Rust-1.99.0/). 2026-10-01 release
- **S5** [Go release history](https://go.dev/doc/devel/release#go1.27.1). go1.27.1 release 2026-09-01; moving history
- **S6** [Rust HashMap](https://doc.rust-lang.org/std/collections/struct.HashMap.html). docs show Rust 1.99.0 b940084d7; moving
- **S7** [Rust BTreeMap](https://doc.rust-lang.org/std/collections/struct.BTreeMap.html). docs show Rust 1.99.0; moving
- **S8** [Rust Send and Sync](https://doc.rust-lang.org/nomicon/send-and-sync.html). moving Rustonomicon
- **S9** [Go language specification](https://go.dev/ref/spec). moving specification; map range semantics
- **S10** [Go memory model](https://go.dev/ref/mem). revision 2022-06-06
- **S11** [C++ unordered_map ordering](https://learn.microsoft.com/en-us/cpp/standard-library/unordered-map-class?view=msvc-170). MSVC170 documentation view; moving
- **S12** [C++ working draft](https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2023/n4950.pdf). N4950 2023-05-10; draft not final standard
- **S13** [Rust File sync and close behavior](https://doc.rust-lang.org/stable/std/fs/struct.File.html). docs show Rust 1.99.0 b940084d7; moving
- **S14** [Rust platform support](https://doc.rust-lang.org/rustc/platform-support.html). moving rustc target policy
- **S15** [Rust external blocks and ABI](https://doc.rust-lang.org/reference/items/external-blocks.html). moving reference; Rust2024 unsafe extern rules
- **S16** [Go os File Sync](https://pkg.go.dev/os@go1.27.1). go1.27.1 versioned API
- **S17** [cgo cross compilation and native interface](https://pkg.go.dev/cmd/cgo). moving package documentation
- **S18** [Windows FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers). moving Win32 API documentation
- **S19** [Linux fsync](https://man7.org/linux/man-pages/man2/fsync.2.html). Linux man-pages fsync2; moving
- **S21** [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects). Win32 documentation updated 2025-07-14
- **S22** [Windows service programs](https://learn.microsoft.com/en-us/windows/win32/services/service-programs). moving Win32 SCM documentation
- **S23** [Linux cgroup v2](https://docs.kernel.org/6.12/admin-guide/cgroup-v2.html). kernel6.12 documentation
- **S24** [Windows pseudoconsole creation](https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session). moving ConPTY documentation
- **S25** [serde_json API](https://docs.rs/serde_json/latest/serde_json/). moving latest API; library example license separately pinned
- **S26** [RustCrypto sha2](https://docs.rs/sha2/0.10.9/sha2/). 0.10.9 versioned API
- **S27** [rusqlite](https://docs.rs/rusqlite/0.37.0/rusqlite/). 0.37.0 versioned API
- **S28** [Cargo build locked offline controls](https://doc.rust-lang.org/cargo/commands/cargo-build.html). moving Cargo documentation
- **S29** [Cargo build scripts](https://doc.rust-lang.org/cargo/reference/build-scripts.html). moving Cargo documentation
- **S30** [Go JSON package](https://pkg.go.dev/encoding/json@go1.27.1). go1.27.1 versioned API
- **S31** [Go SHA256 package](https://pkg.go.dev/crypto/sha256@go1.27.1). go1.27.1 versioned API
- **S32** [Go native plugin limitations](https://pkg.go.dev/plugin). moving package documentation
- **S33** [nlohmann JSON parsing](https://json.nlohmann.me/features/parsing/parse_exceptions/). moving official library documentation
- **S34** [OpenSSL digest APIs](https://docs.openssl.org/3.5/man3/EVP_DigestInit/). OpenSSL3.5 documentation
- **S35** [SQLite C interface introduction](https://www.sqlite.org/cintro.html). moving official documentation; introductory scope only
- **S36** [Clang compiler manual](https://clang.llvm.org/docs/UsersManual.html). moving official compiler manual
- **S37** [SQLite WAL](https://www.sqlite.org/wal.html). moving official WAL documentation
- **S38** [SQLite synchronous pragma](https://sqlite.org/pragma.html#pragma_synchronous). moving official pragma documentation
- **S39** [Crash consistency filesystem study](https://www.usenix.org/conference/osdi14/technical-sessions/presentation/pillai). OSDI2014 original paper record
- **S40** [WebAssembly type soundness appendix](https://webassembly.github.io/spec/core/appendix/properties.html). moving core3.0 spec dated2026-10-02
- **S41** [Wasmtime security model](https://docs.wasmtime.dev/security.html). moving upstream documentation
- **S42** [WIT reference](https://component-model.bytecodealliance.org/design/wit.html). moving component-model documentation
- **S43** [Wasmtime deterministic execution](https://docs.wasmtime.dev/examples-deterministic-wasm-execution.html). moving upstream documentation
- **S44** [Wasmtime language embeddings](https://docs.wasmtime.dev/lang.html). moving upstream documentation
- **S45** [Rigorous Benchmarking in Reasonable Time](https://kar.kent.ac.uk/33611/). Kalibera and Jones ISMM2013 corrected author manuscript record; retrieved search result
- **S46** [Linux pidfd_open](https://man7.org/linux/man-pages/man2/pidfd_open.2.html). Linux man-pages pidfd_open2; moving
- **L1** [Rust MIT license](https://github.com/rust-lang/rust/blob/1.98.1/LICENSE-MIT). 1.98.1 source tag
- **L2** [Rust Apache license](https://github.com/rust-lang/rust/blob/1.98.1/LICENSE-APACHE). 1.98.1 source tag
- **L3** [Go license](https://github.com/golang/go/blob/go1.27.1/LICENSE). go1.27.1 source tag
- **L4** [LLVM license and exceptions](https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/LICENSE.TXT). llvmorg20.1.8 source tag
- **L5** [serde_json MIT license](https://github.com/serde-rs/json/blob/v1.0.145/LICENSE-MIT). v1.0.145 source tag
- **L6** [serde_json Apache license](https://github.com/serde-rs/json/blob/v1.0.145/LICENSE-APACHE). v1.0.145 source tag
- **L7** [sha2 MIT license](https://github.com/RustCrypto/hashes/blob/sha2-v0.10.9/sha2/LICENSE-MIT). sha2-v0.10.9 source tag
- **L8** [rusqlite MIT license](https://github.com/rusqlite/rusqlite/blob/v0.37.0/LICENSE). v0.37.0 source tag
- **L9** [nlohmann JSON MIT license](https://github.com/nlohmann/json/blob/v3.12.0/LICENSE.MIT). v3.12.0 source tag
- **L10** [OpenSSL Apache license](https://github.com/openssl/openssl/blob/openssl-3.5.0/LICENSE.txt). openssl3.5.0 source tag
- **L11** [SQLite copyright statement](https://sqlite.org/copyright.html). moving upstream statement
- **L12** [Wasmtime license and LLVM exceptions](https://github.com/bytecodealliance/wasmtime/blob/main/LICENSE). moving main source text; exact host release unverified
- **DN01** [Dictionary enumeration order is undefined; concurrent writes require synchronization.](https://learn.microsoft.com/en-us/dotnet/api/system.collections.generic.dictionary-2?view=net-10.0). Moving API docs; net-10.0 view, not a selected runtime
- **DN03** [Checked and unchecked contexts determine integral overflow behavior; decimal overflow throws.](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/statements/checked-and-unchecked). Moving C# reference
- **DN05** [ICU/NLS and locale data affect comparison and formatting; app-local ICU can fix the deployed ICU dependency.](https://learn.microsoft.com/en-us/dotnet/core/extensions/globalization-icu). Moving .NET guide
- **DN06** [Channels provide producer/consumer synchronization and bounded-channel backpressure.](https://learn.microsoft.com/en-us/dotnet/core/extensions/channels). Moving .NET guide
- **DN07** [FileStream exposes Flush(bool); parameterless Flush forwards false.](https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Private.CoreLib/src/System/IO/FileStream.cs). Fixed source tag v8.0.0
- **DN08** [FlushToDisk calls FSync; EROFS, EINVAL and ENOTSUP are ignored for unsupported special-file synchronization.](https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Private.CoreLib/src/System/IO/Strategies/FileStreamHelpers.Unix.cs). Fixed source tag v8.0.0
- **DN09** [FlushToDisk calls FlushFileBuffers, ignoring access-denied to accommodate read-only handles.](https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Private.CoreLib/src/System/IO/Strategies/FileStreamHelpers.Windows.cs). Fixed source tag v8.0.0
- **DN10** [Tree kill is asynchronous; parent exit does not prove descendant exit, and inaccessible descendants may be skipped.](https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.process.kill?view=net-10.0). Moving API docs; net-10.0 view, not a selected runtime
- **DN11** [P/Invoke calls unmanaged functions, structures and callbacks, with Windows and Linux examples.](https://learn.microsoft.com/en-us/dotnet/standard/native-interop/pinvoke). Moving .NET guide
- **DN13** [System.Text.Json provides JSON reader/writer and serialization support, including UTF-8-oriented APIs.](https://learn.microsoft.com/en-us/dotnet/standard/serialization/system-text-json/overview). Moving .NET guide
- **DN14** [SHA256.HashData accepts bytes/spans and streams.](https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Security.Cryptography/src/System/Security/Cryptography/SHA256.cs). Fixed source tag v8.0.0
- **DN15** [Microsoft.Data.Sqlite is a lightweight ADO.NET provider for SQLite.](https://learn.microsoft.com/en-us/dotnet/standard/data/sqlite/). Moving Microsoft.Data.Sqlite guide
- **DN16** [EF Core repository, including Microsoft.Data.Sqlite source, carries MIT license text.](https://raw.githubusercontent.com/dotnet/efcore/v8.0.0/LICENSE.txt). Fixed source tag v8.0.0
- **DN17** [Runtime root license text is MIT; dependency notices remain separate.](https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/LICENSE.TXT). Fixed source tag v8.0.0
- **DN18** [Roslyn root license text is MIT.](https://raw.githubusercontent.com/dotnet/roslyn/main/License.txt). Moving main branch
- **DN19** [Deterministic compilation promises identical assemblies for identical compiler inputs, including paths, compiler, references and environment-sensitive inputs.](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/compiler-options/code-generation). Moving C# compiler documentation
- **DN21** [Framework-dependent, self-contained and Native AOT publishing have different runtime/deployment obligations.](https://learn.microsoft.com/en-us/dotnet/core/deploying/). Moving .NET publishing guide
- **DN22** [Managed threads can be suspended for garbage collection; unmanaged resources require explicit cleanup.](https://learn.microsoft.com/en-us/dotnet/standard/garbage-collection/fundamentals). Moving .NET guide
- **DN23** [The table lists Windows 11 x64 and supported Linux distributions; a kernel version alone is not its support criterion.](https://raw.githubusercontent.com/dotnet/core/main/release-notes/8.0/supported-os.md). Moving 8.0 support table; observed 2026-09-28 update
- **DN24** [JsonSchema.Net evaluates JSON instances against explicitly chosen schema dialects; exact library release remains unpinned.](https://docs.json-everything.net/schema/basics/). Moving JsonSchema.Net guide; includes developing future dialect discussion
- **DN25** [Json-everything root license text is MIT.](https://raw.githubusercontent.com/gregsdennis/json-everything/master/LICENSE). Moving master branch
- **JV01** [HashMap does not promise iteration order or synchronization; fail-fast exceptions are best-effort diagnostics.](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/HashMap.html). Java SE 21 API edition
- **JV03** [Primitive integer widths are fixed; integer operators do not signal overflow; float/double correspond to IEEE formats.](https://docs.oracle.com/javase/specs/jls/se21/html/jls-4.html). Java SE 21 specification
- **JV05** [Default locale is established from host environment and influences locale-sensitive operations.](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/Locale.html). Java SE 21 API edition
- **JV06** [A single-thread executor guarantees sequential task execution, with an unbounded queue.](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/concurrent/Executors.html). Java SE 21 API edition
- **JV08** [force requests file-content/metadata persistence; its guarantee is limited to local devices and specified writes.](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/nio/channels/FileChannel.html). Java SE 21 API edition
- **JV09** [Process handles expose descendants and termination, but process state changes asynchronously.](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/lang/ProcessHandle.html). Java SE 21 API edition
- **JV11** [JNI specifies the native-code interface.](https://docs.oracle.com/en/java/javase/21/docs/specs/jni/index.html). Java SE 21 specification
- **JV13** [MessageDigest hashes bytes; SHA-256 support is required by the Java platform.](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/security/MessageDigest.html). Java SE 21 API edition
- **JV14** [Jackson core provides streaming JSON parsers/generators; its core runtime package reports no external dependencies.](https://raw.githubusercontent.com/FasterXML/jackson-core/2.18/README.md). Moving Jackson 2.18 branch
- **JV15** [Jackson core license text is Apache-2.0.](https://raw.githubusercontent.com/FasterXML/jackson-core/2.18/LICENSE). Moving Jackson 2.18 branch
- **JV16** [SQLite JDBC provides a Java driver with packaged native libraries for major operating systems.](https://raw.githubusercontent.com/xerial/sqlite-jdbc/master/README.adoc). Moving master branch
- **JV17** [SQLite JDBC wrapper license text is Apache-2.0; SQLite engine licensing is separate.](https://raw.githubusercontent.com/xerial/sqlite-jdbc/master/LICENSE). Moving master branch
- **JV18** [OpenJDK root license contains GPL version 2 and a Classpath exception applicable to expressly designated source files.](https://raw.githubusercontent.com/openjdk/jdk/jdk-21%2B35/LICENSE). Fixed OpenJDK jdk-21+35 tag
- **JV19** [Additional license information distinguishes third-party portions and source files covered by exceptions.](https://raw.githubusercontent.com/openjdk/jdk/jdk-21%2B35/ADDITIONAL_LICENSE_INFO). Fixed OpenJDK jdk-21+35 tag
- **JV20** [Kotlin root license text is Apache-2.0.](https://raw.githubusercontent.com/JetBrains/kotlin/v2.1.0/license/LICENSE.txt). Fixed Kotlin v2.1.0 tag
- **JV21** [Oracle lists Windows 11 x64 and specified Linux distributions; this is not blanket certification of arbitrary OpenJDK builds or kernels.](https://www.oracle.com/java/technologies/javase/products-doc-jdk21certconfig.html). Moving Oracle JDK 21 certification table
- **JV22** [JDK offers different collectors; collector choice and heap characteristics affect pauses and throughput.](https://docs.oracle.com/en/java/javase/21/gctuning/available-collectors.html). JDK 21 guide edition
- **JV23** [jlink creates custom runtime images; users are responsible for updating their images.](https://docs.oracle.com/en/java/javase/21/docs/specs/man/jlink.html). JDK 21 tool manual
- **JV25** [NetworkNT provides Java JSON Schema validation across supported dialects; exact release/dependency closure remains unpinned.](https://raw.githubusercontent.com/networknt/json-schema-validator/master/README.md). Moving master branch
- **JV26** [NetworkNT JSON Schema Validator root license text is Apache-2.0.](https://raw.githubusercontent.com/networknt/json-schema-validator/master/LICENSE). Moving master branch
- **ND01** [Map/Set iteration follows insertion order; Number is binary64, BigInt represents integers, and JSON has prescribed serialization behavior.](https://ecma-international.org/wp-content/uploads/ECMA-262_15th_edition_june_2024.pdf). ECMAScript 2024, 15th edition
- **ND03** [Workers execute JavaScript in parallel and can share memory; resource limits do not cover all allocations.](https://nodejs.org/download/release/v24.0.0/docs/api/worker_threads.html). Fixed Node v24.0.0 documentation
- **ND04** [Subprocess APIs expose pipes, IPC and termination; killing a shell does not necessarily kill its descendants.](https://nodejs.org/download/release/v24.0.0/docs/api/child_process.html). Fixed Node v24.0.0 documentation
- **ND05** [fsync and filehandle.sync request storage flush; concurrent writes and device-specific persistence need care.](https://nodejs.org/download/release/v24.0.0/docs/api/fs.html). Fixed Node v24.0.0 documentation
- **ND06** [Windows libuv fsync/fdatasync share an implementation calling FlushFileBuffers.](https://raw.githubusercontent.com/nodejs/node/v24.0.0/deps/uv/src/win/fs.c). Fixed Node v24.0.0 vendored libuv source
- **ND08** [Node provides WASI APIs but explicitly does not provide secure WASI sandboxing.](https://nodejs.org/download/release/v24.0.0/docs/api/wasi.html). Fixed Node v24.0.0 documentation
- **ND09** [Built-in node:sqlite is Stability 1.1, active development; DatabaseSync methods execute synchronously.](https://nodejs.org/download/release/v24.0.0/docs/api/sqlite.html). Fixed Node v24.0.0 documentation
- **ND10** [Crypto hash APIs accept byte buffers; string inputs have encoding rules.](https://nodejs.org/download/release/v24.0.0/docs/api/crypto.html). Fixed Node v24.0.0 documentation
- **ND11** [Windows x64 >=10 is Tier 1; Linux x64 Tier 1 requires kernel >=4.18 and glibc >=2.28, plus maintained platform support.](https://raw.githubusercontent.com/nodejs/node/v24.0.0/BUILDING.md). Fixed Node v24.0.0 source
- **ND12** [Type annotations are erased and cannot supply runtime validation by themselves.](https://www.typescriptlang.org/docs/handbook/2/basic-types.html). Moving TypeScript handbook
- **ND13** [Node's own license is MIT-style; this file also includes bundled dependency license texts.](https://raw.githubusercontent.com/nodejs/node/v24.0.0/LICENSE). Fixed Node v24.0.0 source
- **ND14** [TypeScript license text is Apache-2.0.](https://raw.githubusercontent.com/microsoft/TypeScript/v5.8.3/LICENSE.txt). Fixed TypeScript v5.8.3 tag
- **ND15** [npm ci requires a lockfile, fails on manifest mismatch and supports disabling package lifecycle scripts.](https://docs.npmjs.com/cli/v10/commands/npm-ci/). npm v10 documentation edition, moving page
- **ND16** [Single-executable applications exist with experimental constraints and a runtime-binary-dependent preparation flow.](https://nodejs.org/download/release/v24.0.0/docs/api/single-executable-applications.html). Fixed Node v24.0.0 documentation
- **ND17** [Node documents GC pauses and diagnostic tracing; no Recursion latency measurements are supplied.](https://nodejs.org/learn/diagnostics/memory/using-gc-traces). Moving Node diagnostics guide
- **ND18** [Ajv supports JSON Schema dialects including 2020-12; validation uses generated code.](https://raw.githubusercontent.com/ajv-validator/ajv/v8.17.1/README.md). Fixed Ajv v8.17.1 tag
- **ND19** [Ajv license text is MIT.](https://raw.githubusercontent.com/ajv-validator/ajv/v8.17.1/LICENSE). Fixed Ajv v8.17.1 tag
- **PY01** [Dict preserves insertion order; set is unordered; Python integers have unlimited precision.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/stdtypes.rst). Fixed CPython v3.13.0 source docs
- **PY02** [String/bytes hashes are salted across processes; hash values are unsuitable as persistent content identifiers.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/reference/datamodel.rst). Fixed CPython v3.13.0 source docs
- **PY03** [os.fsync calls Unix fsync or Windows _commit; buffered file flush must precede it; UTF-8 mode controls locale-sensitive defaults.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/os.rst). Fixed CPython v3.13.0 source docs
- **PY04** [Free-threading was experimental; built-in container locks are implementation details, not a concurrency protocol guarantee.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/howto/free-threading-python.rst). Fixed CPython v3.13.0 source docs
- **PY05** [Subprocess provides pipes, sessions/process groups and platform-specific termination; communicate timeout does not kill the child automatically.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/subprocess.rst). Fixed CPython v3.13.0 source docs
- **PY06** [ctypes calls DLL/shared-library functions; foreign calls can release the GIL.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/ctypes.rst). Fixed CPython v3.13.0 source docs
- **PY07** [JSON supports configurable key sorting/number parsing; default encoding permits NaN and Infinity.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/json.rst). Fixed CPython v3.13.0 source docs
- **PY08** [hashlib exposes SHA-256 and other byte-oriented digest constructors.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Lib/hashlib.py). Fixed CPython v3.13.0 source
- **PY09** [sqlite3 wraps SQLite; transaction configuration and threadsafety depend on settings and the linked engine.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/sqlite3.rst). Fixed CPython v3.13.0 source docs
- **PY10** [CPython reference counting is supplemented by a cyclic collector with configurable collection behavior.](https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/gc.rst). Fixed CPython v3.13.0 source docs
- **PY11** [Python license bundle includes PSF License Version 2 and historical component licenses.](https://raw.githubusercontent.com/python/cpython/v3.13.0/LICENSE). Fixed CPython v3.13.0 source
- **PY12** [CPython lists x86_64 Windows MSVC and Linux glibc GCC as Tier 1; this is not exact-kernel certification.](https://peps.python.org/pep-0011/). Moving PEP/platform list
- **PY14** [pip hash-checking mode requires pinned, hashed dependencies; binary-only installs can avoid source-build execution.](https://raw.githubusercontent.com/pypa/pip/25.0.1/docs/html/topics/secure-installs.md). Fixed pip 25.0.1 source docs
- **PY15** [jsonschema implements JSON Schema including 2020-12; format checks are not activated merely by schema keywords or extras.](https://python-jsonschema.readthedocs.io/en/v4.23.0/). jsonschema 4.23.0 documentation
- **PY16** [jsonschema license text is MIT.](https://raw.githubusercontent.com/python-jsonschema/jsonschema/v4.23.0/COPYING). Fixed jsonschema v4.23.0 tag


[R1]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/CONTROL_PLANE.md
[R2]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/ANCHOR.md
[R3]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/state/DECISIONS.md
[R4]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/followups-20261002/synthesis/FU-01-language-runtime.md
[R5]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/followups-20261002/synthesis/FU-02-store-crash-replay.md
[R6]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-kernel-session-a/README.md#L22-L69
[R7]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-kernel-session-a/verification/results/74fb18d-muse-spark-1-3.md
[R8]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-kernel-session-a/oracle/README.md
[R9]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/research-lane/followups-20261002/synthesis/FU-03-formal-model-vectors.md
[R10]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/M1B-WIRE-PROFILE.md
[R11]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md#L97-L134
[R12]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/FRONTIER-HARNESS-RESEARCH-TRANCHE-2-EVIDENCE.md#L339-L390
[R13]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/CONTEXT.md
[R14]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-stage-b/README.md
[R15]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/experiments/v3/fu-03-stage-b/verification/results/d56cc44-muse-spark-1-3.md
[R16]: https://github.com/NotSkittlz/recursion/blob/699a29d622d1a1d3cecdaf3504cc5d649fb9d20e/docs/v3/PERFORMANCE-PLAN.md
[S1]: https://www.rfc-editor.org/rfc/rfc8785
[S2]: https://www.rfc-editor.org/rfc/rfc8949.html
[S3]: https://www.rfc-editor.org/rfc/rfc7464.html
[S4]: https://blog.rust-lang.org/2026/10/01/Rust-1.99.0/
[S5]: https://go.dev/doc/devel/release#go1.27.1
[S6]: https://doc.rust-lang.org/std/collections/struct.HashMap.html
[S7]: https://doc.rust-lang.org/std/collections/struct.BTreeMap.html
[S8]: https://doc.rust-lang.org/nomicon/send-and-sync.html
[S9]: https://go.dev/ref/spec
[S10]: https://go.dev/ref/mem
[S11]: https://learn.microsoft.com/en-us/cpp/standard-library/unordered-map-class?view=msvc-170
[S12]: https://www.open-std.org/jtc1/sc22/wg21/docs/papers/2023/n4950.pdf
[S13]: https://doc.rust-lang.org/stable/std/fs/struct.File.html
[S14]: https://doc.rust-lang.org/rustc/platform-support.html
[S15]: https://doc.rust-lang.org/reference/items/external-blocks.html
[S16]: https://pkg.go.dev/os@go1.27.1
[S17]: https://pkg.go.dev/cmd/cgo
[S18]: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
[S19]: https://man7.org/linux/man-pages/man2/fsync.2.html
[S21]: https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
[S22]: https://learn.microsoft.com/en-us/windows/win32/services/service-programs
[S23]: https://docs.kernel.org/6.12/admin-guide/cgroup-v2.html
[S24]: https://learn.microsoft.com/en-us/windows/console/creating-a-pseudoconsole-session
[S25]: https://docs.rs/serde_json/latest/serde_json/
[S26]: https://docs.rs/sha2/0.10.9/sha2/
[S27]: https://docs.rs/rusqlite/0.37.0/rusqlite/
[S28]: https://doc.rust-lang.org/cargo/commands/cargo-build.html
[S29]: https://doc.rust-lang.org/cargo/reference/build-scripts.html
[S30]: https://pkg.go.dev/encoding/json@go1.27.1
[S31]: https://pkg.go.dev/crypto/sha256@go1.27.1
[S32]: https://pkg.go.dev/plugin
[S33]: https://json.nlohmann.me/features/parsing/parse_exceptions/
[S34]: https://docs.openssl.org/3.5/man3/EVP_DigestInit/
[S35]: https://www.sqlite.org/cintro.html
[S36]: https://clang.llvm.org/docs/UsersManual.html
[S37]: https://www.sqlite.org/wal.html
[S38]: https://sqlite.org/pragma.html#pragma_synchronous
[S39]: https://www.usenix.org/conference/osdi14/technical-sessions/presentation/pillai
[S40]: https://webassembly.github.io/spec/core/appendix/properties.html
[S41]: https://docs.wasmtime.dev/security.html
[S42]: https://component-model.bytecodealliance.org/design/wit.html
[S43]: https://docs.wasmtime.dev/examples-deterministic-wasm-execution.html
[S44]: https://docs.wasmtime.dev/lang.html
[S45]: https://kar.kent.ac.uk/33611/
[S46]: https://man7.org/linux/man-pages/man2/pidfd_open.2.html
[L1]: https://github.com/rust-lang/rust/blob/1.98.1/LICENSE-MIT
[L2]: https://github.com/rust-lang/rust/blob/1.98.1/LICENSE-APACHE
[L3]: https://github.com/golang/go/blob/go1.27.1/LICENSE
[L4]: https://github.com/llvm/llvm-project/blob/llvmorg-20.1.8/LICENSE.TXT
[L5]: https://github.com/serde-rs/json/blob/v1.0.145/LICENSE-MIT
[L6]: https://github.com/serde-rs/json/blob/v1.0.145/LICENSE-APACHE
[L7]: https://github.com/RustCrypto/hashes/blob/sha2-v0.10.9/sha2/LICENSE-MIT
[L8]: https://github.com/rusqlite/rusqlite/blob/v0.37.0/LICENSE
[L9]: https://github.com/nlohmann/json/blob/v3.12.0/LICENSE.MIT
[L10]: https://github.com/openssl/openssl/blob/openssl-3.5.0/LICENSE.txt
[L11]: https://sqlite.org/copyright.html
[L12]: https://github.com/bytecodealliance/wasmtime/blob/main/LICENSE
[DN01]: https://learn.microsoft.com/en-us/dotnet/api/system.collections.generic.dictionary-2?view=net-10.0
[DN03]: https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/statements/checked-and-unchecked
[DN05]: https://learn.microsoft.com/en-us/dotnet/core/extensions/globalization-icu
[DN06]: https://learn.microsoft.com/en-us/dotnet/core/extensions/channels
[DN07]: https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Private.CoreLib/src/System/IO/FileStream.cs
[DN08]: https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Private.CoreLib/src/System/IO/Strategies/FileStreamHelpers.Unix.cs
[DN09]: https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Private.CoreLib/src/System/IO/Strategies/FileStreamHelpers.Windows.cs
[DN10]: https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.process.kill?view=net-10.0
[DN11]: https://learn.microsoft.com/en-us/dotnet/standard/native-interop/pinvoke
[DN13]: https://learn.microsoft.com/en-us/dotnet/standard/serialization/system-text-json/overview
[DN14]: https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/src/libraries/System.Security.Cryptography/src/System/Security/Cryptography/SHA256.cs
[DN15]: https://learn.microsoft.com/en-us/dotnet/standard/data/sqlite/
[DN16]: https://raw.githubusercontent.com/dotnet/efcore/v8.0.0/LICENSE.txt
[DN17]: https://raw.githubusercontent.com/dotnet/runtime/v8.0.0/LICENSE.TXT
[DN18]: https://raw.githubusercontent.com/dotnet/roslyn/main/License.txt
[DN19]: https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/compiler-options/code-generation
[DN21]: https://learn.microsoft.com/en-us/dotnet/core/deploying/
[DN22]: https://learn.microsoft.com/en-us/dotnet/standard/garbage-collection/fundamentals
[DN23]: https://raw.githubusercontent.com/dotnet/core/main/release-notes/8.0/supported-os.md
[DN24]: https://docs.json-everything.net/schema/basics/
[DN25]: https://raw.githubusercontent.com/gregsdennis/json-everything/master/LICENSE
[JV01]: https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/HashMap.html
[JV03]: https://docs.oracle.com/javase/specs/jls/se21/html/jls-4.html
[JV05]: https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/Locale.html
[JV06]: https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/concurrent/Executors.html
[JV08]: https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/nio/channels/FileChannel.html
[JV09]: https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/lang/ProcessHandle.html
[JV11]: https://docs.oracle.com/en/java/javase/21/docs/specs/jni/index.html
[JV13]: https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/security/MessageDigest.html
[JV14]: https://raw.githubusercontent.com/FasterXML/jackson-core/2.18/README.md
[JV15]: https://raw.githubusercontent.com/FasterXML/jackson-core/2.18/LICENSE
[JV16]: https://raw.githubusercontent.com/xerial/sqlite-jdbc/master/README.adoc
[JV17]: https://raw.githubusercontent.com/xerial/sqlite-jdbc/master/LICENSE
[JV18]: https://raw.githubusercontent.com/openjdk/jdk/jdk-21%2B35/LICENSE
[JV19]: https://raw.githubusercontent.com/openjdk/jdk/jdk-21%2B35/ADDITIONAL_LICENSE_INFO
[JV20]: https://raw.githubusercontent.com/JetBrains/kotlin/v2.1.0/license/LICENSE.txt
[JV21]: https://www.oracle.com/java/technologies/javase/products-doc-jdk21certconfig.html
[JV22]: https://docs.oracle.com/en/java/javase/21/gctuning/available-collectors.html
[JV23]: https://docs.oracle.com/en/java/javase/21/docs/specs/man/jlink.html
[JV25]: https://raw.githubusercontent.com/networknt/json-schema-validator/master/README.md
[JV26]: https://raw.githubusercontent.com/networknt/json-schema-validator/master/LICENSE
[ND01]: https://ecma-international.org/wp-content/uploads/ECMA-262_15th_edition_june_2024.pdf
[ND03]: https://nodejs.org/download/release/v24.0.0/docs/api/worker_threads.html
[ND04]: https://nodejs.org/download/release/v24.0.0/docs/api/child_process.html
[ND05]: https://nodejs.org/download/release/v24.0.0/docs/api/fs.html
[ND06]: https://raw.githubusercontent.com/nodejs/node/v24.0.0/deps/uv/src/win/fs.c
[ND08]: https://nodejs.org/download/release/v24.0.0/docs/api/wasi.html
[ND09]: https://nodejs.org/download/release/v24.0.0/docs/api/sqlite.html
[ND10]: https://nodejs.org/download/release/v24.0.0/docs/api/crypto.html
[ND11]: https://raw.githubusercontent.com/nodejs/node/v24.0.0/BUILDING.md
[ND12]: https://www.typescriptlang.org/docs/handbook/2/basic-types.html
[ND13]: https://raw.githubusercontent.com/nodejs/node/v24.0.0/LICENSE
[ND14]: https://raw.githubusercontent.com/microsoft/TypeScript/v5.8.3/LICENSE.txt
[ND15]: https://docs.npmjs.com/cli/v10/commands/npm-ci/
[ND16]: https://nodejs.org/download/release/v24.0.0/docs/api/single-executable-applications.html
[ND17]: https://nodejs.org/learn/diagnostics/memory/using-gc-traces
[ND18]: https://raw.githubusercontent.com/ajv-validator/ajv/v8.17.1/README.md
[ND19]: https://raw.githubusercontent.com/ajv-validator/ajv/v8.17.1/LICENSE
[PY01]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/stdtypes.rst
[PY02]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/reference/datamodel.rst
[PY03]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/os.rst
[PY04]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/howto/free-threading-python.rst
[PY05]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/subprocess.rst
[PY06]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/ctypes.rst
[PY07]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/json.rst
[PY08]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Lib/hashlib.py
[PY09]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/sqlite3.rst
[PY10]: https://raw.githubusercontent.com/python/cpython/v3.13.0/Doc/library/gc.rst
[PY11]: https://raw.githubusercontent.com/python/cpython/v3.13.0/LICENSE
[PY12]: https://peps.python.org/pep-0011/
[PY14]: https://raw.githubusercontent.com/pypa/pip/25.0.1/docs/html/topics/secure-installs.md
[PY15]: https://python-jsonschema.readthedocs.io/en/v4.23.0/
[PY16]: https://raw.githubusercontent.com/python-jsonschema/jsonschema/v4.23.0/COPYING
