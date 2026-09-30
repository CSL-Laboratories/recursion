# Anchor

Reread this when deciding what to work on. If a task doesn't serve something below, it's
not in scope.

Last reviewed: 2026-09-29 · Next review: 2026-12-01

---

## What Recursion is

Recursion is a provider-neutral agent platform: a small deterministic kernel with typed
adapters, native and upstream execution, persistent goal-scoped assistants, configurable
context and compaction, plugins, interactive terminal control, and Windows/Linux operation
and recovery. The current development target is **Recursion v3**.

Other workstreams are separate repositories with their own lanes. Recursion does not embed
another project's source, curriculum, captures, or registry.

---

## Success criteria (12 month horizon)

**1. Shipped product.** Recursion v3 is buildable and publicly released, with every release
gate passing on a frozen candidate and a person outside this repository able to install and
run it.

**2. Verified behavior.** Accepted behavior is backed by deterministic source checks and
independent review. A provider return, an implementer's self-report, or model consensus is
never ground truth.

**3. Durable platform.** Authority, state, evidence, and recovery invariants hold under
crash, cancellation, restart, route change, and ambiguous external effects.

---

## Rationing rule

The constraint is review throughput, not tokens. Generated work that isn't verified is a
liability, not an asset.

- **Opus / frontier:** hard multi-file reasoning, architecture calls, security boundaries
- **Mid tier:** mechanical passes, refactors, test generation
- **Cheap tier / local:** the supervisor loop, summarization, triage, overnight batch
- **Local (4090 / Qwen):** unattended high-volume mechanical work

If output is accumulating faster than it's being reviewed, the answer is less generation,
not more capacity.

---

## Out of scope

Written down so it stops re-entering scope:

- Any repository, product, curriculum, capture, or dataset that is not Recursion itself.
  Those live in their own lanes.
- Market, investment, or "alpha" research. Near-term tooling spend converts to income via
  shipped work and rare skills, not via returns.
- Any autonomy that extracts from or acts against a third-party service's terms.
