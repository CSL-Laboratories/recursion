# Recursion

**Recursion v3** is a provider-neutral agent platform: a harness with its own client,
execution kernel, context/compaction engine and multi-node fabric, plus adapters for
supported upstream harnesses. The v3 specifications, research and decision log that used to
live under `docs/` and `state/` moved to csl-research under CSL-536: see
[`contracts/recursion/history/`](https://github.com/CSL-Laboratories/csl-research/tree/f9d8526acbaf02abb227b38d88ed95e170a1f6d4/contracts/recursion/history). The next product contract
will be a `PRODUCT.md` in csl-research.

V1/v2 — the previous Hermes-based supervisor and control plane — are **retired**. They were
removed from the trunk on 2026-09-28 and remain recoverable from the
`pre-v3-clean-cut-20260928` tag.

Start here:

- [csl-research `contracts/recursion/history/`](https://github.com/CSL-Laboratories/csl-research/tree/f9d8526acbaf02abb227b38d88ed95e170a1f6d4/contracts/recursion/history) — the former `docs/` and `state/` records, byte for byte
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute
- [ANCHOR.md](ANCHOR.md) — why the work exists; [CONTROL_PLANE.md](CONTROL_PLANE.md) — who decides

## What Recursion connects to

Only what you configure. Recursion sends nothing to its makers: no usage counts, no crash
reports, no update check, and it makes no install or machine identifier. The daemon opens two
doors, both on your own machine: the control door, a Unix socket in your runtime folder, and the
browser view on the loopback address `127.0.0.1`. It starts the engine programs you configured,
with only the addresses you configured; what an engine program reaches on its own is that
program's setting (Codex is started with its update check turned off). A plugin reaches a network
only under a grant you gave; that proof belongs to the plugin checks in
`core/recursion_core/plugin_security/`, not to this list. The shipped code may import only the
modules listed with their reasons in `tools/network_reach.toml`, and every place in it that could
reach a network is written down there with who supplies its address and the exact text of each call.

Run the proof on your own machine:

- `python3 tools/network_reach.py` reads every shipped file by its syntax tree. It prints PROVEN,
  or NOT PROVEN with each problem: a module that is not on the list; running text as code, or
  reaching a module or a function by a name built at run time; a foreign-function, private
  socket or subprocess, zip-import or unpickling module; a log handler that sends over a network;
  an import of a test module or of anything outside the package; a call that could reach a network
  and has no row, or whose text is not the text its row holds; a file it cannot read.
- `python3 core/tests/test_no_call_home_runtime.py` (Linux, CPython 3.12 or later) starts a daemon
  of its own in a temporary folder, leaves it idle, asks its status and stops it, while the
  interpreter's audit hook watches that it reaches no address and looks up no name; then it starts
  a stand-in engine at an address it configured and checks that only that address is reached. It
  prints PROVEN (run-time) or NOT PROVEN, and the list of what it does not cover. The daemon itself
  runs only on free-threaded CPython 3.14t; on another interpreter the test stands in for that one
  check, and the rest of the daemon runs as shipped.

What is proven, and what is not:

- Proven before every merge: the tests of both proofs (`core/tests/test_network_reach.py`, which
  checks what the first command checks and plants each refused kind, and the file of the second
  command) run in the hosted gate (CPython 3.12 on Linux), so a later change that adds a call home, a module or a refused construct goes red there. The
  whole test suite can also be run once with the hook in every Python process it starts:
  `python3 tools/network_audit.py`; it names every program that is not Python and refuses one that
  is not listed in `tools/network_audit.toml`. That run is not part of the hosted gate.
- Not proven: the engines' own traffic to the addresses you configured, and anything else an
  engine program reaches by itself; what a plugin does under a grant you gave (the plugin checks'
  proof); what the operating system does by itself (its own name lookups and services); what any
  program the daemon starts reaches by itself (the test runs only its own stand-ins); the traffic
  of a socket made from a descriptor handed in from outside with its family named; a call into the
  operating system through a foreign-function module at run time (the shipped code may not import
  one, and the run-time test refuses any such call it sees). The test runs on Linux only; Windows
  and macOS are not covered.

> Governance note: `CONTROL_PLANE.md` and `ANCHOR.md` are the operative authority and scope
> records. They already describe Recursion v3 (D-017, 2026-09-28); the older claim that they
> "still describe the pre-v3 control plane" was stale. Neither file is edited in the
> 2026-10-24 spec amendment (`ANCHOR.md` is a protected path with no founder authoring
> session). GUI-timing supersession for ANCHOR's "terminal first, GUI later" is already
> D-038; additions beyond D-038 live in `docs/v3/AMENDMENT-2026-10-24.md` (now in csl-research
> history, see above). Status:
> amendment proposed (2026-10-24); acceptance is a human decision by Lucas.

## Layout

| Path | What it is |
|---|---|
| `docs/` | only the operational files this repository's code, a gate or CI reads (`docs/ci-event-contract.md`, `docs/v3/x1/`). Every other doc goes to [csl-research](https://github.com/CSL-Laboratories/csl-research) (CSL-536). |
| `experiments/v3/` | runnable product checks only. Earlier runs are kept as evidence in csl-research [`contracts/recursion/evidence/experiments/v3/`](https://github.com/CSL-Laboratories/csl-research/tree/d29bb4a4bec86fb6637eaa7ec3afeaab083eb0e0/contracts/recursion/evidence/experiments/v3) (CSL-596). |
| `schemas/control-plane/v3/` | v3 contract schemas (planning, execution, integrity) |
| `scripts/v3/` | v3 planning replay and validation |
| `scripts/tests/v3/` | v3 M1 test programs and fixtures |
| `state/` | `INBOX.md` (optional escalation) and `PROPOSALS.md` (protected-change proposals). The frozen decision log moved to csl-research history. |
| `contract/PRODUCT.pin.json` | pin to the product contract in csl-research (repository, full commit, path, SHA-256); `tools/check_product_pin.py` checks it (CSL-585) |

## Retired

The v1/v2 custom harness — the Hermes executive, supervisor loop, decision queue, project
pilot and reconciler, `work.sh`/`publish.sh`, the MVP contract kernel, execution fabric,
remote adapters, and their configs and deployment units — was removed from the trunk on
2026-09-28 because it contributed no dependency to v3.
Nothing is lost: the complete prior tree is at tag `pre-v3-clean-cut-20260928` and in git
history.
