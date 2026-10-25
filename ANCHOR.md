# Anchor

Reread this when deciding what to work on. If a task doesn't serve something below, it's not
in scope.

Last reviewed: 2026-10-02 · Next review: 2027-01-02

Amendment proposed 2026-10-24; acceptance is a human decision by Lucas. See
`docs/v3/AMENDMENT-2026-10-24.md`. This file is a protected path; the only proposed
change is the GUI-timing sentence in Mission.

---

## Mission

Build and ship **Recursion v3**: a community, open-source agent harness that is both a
first-class native harness *and* a first-class unifying wrapper over the wider harness ecosystem.
A person can drive it — a client (terminal first, GUI later), a local execution kernel, a
context/compaction engine, and a multi-node fabric — with typed adapters for supported upstream
harnesses. Its public thesis: **"the harness that scales with your workflow"** — a capable baseline
alone that grows deterministically, without lock-in, as capabilities are added. Its purpose is to
remove ecosystem lock-in and give users optimized performance on
the models and subscriptions they already prefer, including by reducing unnecessary quota/usage
burn. It does not hold or relay provider credentials it was not explicitly granted; where
possible it delegates to a user-authenticated tool. The comparison standard is tools like T3
Code. "First usable release" is defined by the accepted platform release in `docs/v3/PRODUCT.md`
(see `state/DECISIONS.md` D-018…D-034).

Amendment proposed 2026-10-24 (acceptance is a human decision by Lucas): the parenthetical
"terminal first, GUI later" is **proposed superseded**. Recursion is the frontier-defining
harness ("the goat") with a first-release GUI client (daemon-served web UI in a thin Tauri
shell), standalone app and CLI/TUI, plus agent-operable MCP/control API. The thesis sentence
and credential non-custody sentence above are **unchanged**. Comparison standard remains
tools like T3 Code, now as a bar Recursion must beat on user effort.

## Success criteria (near term)

1. **One trustworthy specification.** `docs/v3/*` is evidence-grounded, internally consistent,
   and coherent enough that a new contributor can pick up a task and act on it.
2. **A proven deterministic core.** The kernel and its contracts (planning, execution,
   integrity) pass their executable gates before live code is written (OR-018).
3. **A first usable release** a person would choose for at least one real workflow, judged on
   responsiveness, then subscription efficiency, then completion (OR-024), subject to
   correctness, authorization, resource limits and recoverability.
4. **Research depth preserved.** Every open question resolves to a pinned source, a
   deterministic gate, or an explicit `unknown` with a named blocker.
5. **No lock-in, measurably.** A goal can be driven through more than one harness/route without
   holding credentials Recursion was not granted, and measured quota/usage burden falls versus
   driving each harness directly — or the public claim is withdrawn.

## Not this year / explicitly out of scope

Written down so it stops re-entering scope:

- Supervising other repositories. That control-plane role was retired with v1/v2.
- Any standing autonomous executive that decomposes and assigns work on its own.
- Live provider, host or spend work without a fresh, reviewed admission.
- Choosing a final language, store, framework or sandbox from research alone (OR-017, OR-020).

## Rationing rule

The constraint is review throughput, not tokens. Generated work that isn't verified is a
liability, not an asset. If output accumulates faster than it is reviewed, the answer is less
generation, not more capacity.

## Evidence discipline

Keep evidence classes distinct: source-documented, locally observed, inferred, and unmeasured
are different things. A paper or a model consensus never proves local fit. A confident wrong
claim is the worst possible output of this system.
