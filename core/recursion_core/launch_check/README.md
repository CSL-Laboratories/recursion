# core/recursion_core/launch_check (CSL-732, B10, R2a; candidate)

The new core's R2a launch check: at launch, each detected engine's exact binary and
version is checked against a versioned floor table, failing closed. Candidate code: not
yet called by a running core, and not reviewed or accepted. Master `scripts/v3` is frozen
and untouched; its `LIVE_ADAPTERS_ENABLED` stays `False`.

Governing spec: `contracts/PRODUCT.md` at csl-research `055cc7a5c793b7135e5d0565dc4198cf96230531`
(SHA-256 `e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`), section 3.3
"R2a defined" (grill Q22), O50 ("Beta until measured") and 11.6 (grill Q43, functional floors), 34b (nine engines).
Build-order item 5 (B10). Recursion hard rule 8 still applies: no live provider call until
R2a merges and `CONTROL_PLANE.md` records each route's account and spend ceiling.
Facts for that gate (whether merging this package counts as "R2a landed" is open for Lucas):
nothing in the repository calls this check yet, so no launch path enforces it, and the
window between `exec_argv()` and the exec itself is still open (CX-1402, see Limits).

## What it does

- `detect(engine, path_string)` / `detect_all(path_string)`: static detection on an
  explicit PATH string (never `os.environ`). Lists the hits for each binary name, resolves
  symlinks and offers only a native target (ELF, Mach-O or PE magic). Nothing found is
  executed. Only the first hit of each name counts, so a native copy shadowed by a wrapper
  is not offered. Codex's npm shim (`bin/codex` -> `@openai/codex/bin/codex.js`) is followed
  to the vendored `vendor/<triple>/codex/codex` only when the layout is recognised;
  otherwise the result is "not grantable".
- `grant(engine, path)`: the only issuer of a `Grant`. In order: engine in the table
  (`engine_unknown`); absolute path (`path_not_absolute`); file stem is one of the engine's
  names (`name_mismatch`, or `wrapper_refused` for an interpreter or wrapper name); the path
  exists and is executable (`binary_missing`); the resolved file is native and not a
  wrapper name (`wrapper_refused`: shebang scripts, links to scripts, `.cmd`/`.ps1`/`.js`/
  `.sh`, text). An engine with no identity rule (Pi) is then refused
  (`identity_unverified`) without being run. It hashes the realpath (full SHA-256 and size), runs the zero-egress version
  probe, re-hashes (`binary_changed` if it changed while probing), and refuses a non-zero
  exit or timeout (`version_probe_failed`), a line naming another engine (`engine_mismatch`),
  an unparsed version (`version_unparsed`), or a prerelease or a version below a
  known floor (`version_refused`). An unset functional floor is not a refusal:
  the grant is tagged `beta` for route.list (O50).
- The grant record holds engine, path, realpath, sha256 (64 hex), size, raw version line,
  exit code, parsed version, both floors, a `beta` tag when the functional floor is
  unset, the floor-table version and the refusal-code version. The grant is a frozen dataclass whose fields are MACed (HMAC-SHA-256) with a
  per-process key; `launch_plan` refuses anything else (`grant_forged`) and any grant issued
  under another table or code version (`grant_stale`), and re-checks the floors.
- `launch_plan(grant, argv, env)`: `argv` non-empty (`argv_empty`); `argv[0]` is the bare
  engine name (`argv0_mismatch`), never an interpreter or wrapper (`sh`, `bash`, `env`,
  `node`, `python`, `npx`, `uv`, `cmd`, `powershell`, `pwsh`, `wsl`, `.cmd`/`.ps1`/`.js`/
  `.sh` and others: `wrapper_refused`); argv plus env under the byte caps, NUL-free text
  (`spawn_too_large`: 128 KiB per item, 256 KiB total). `env` is the child environment
  already built by the child-env boundary (recursion #320), not `os.environ`.
- `LaunchPlan.exec_argv()` authenticates and re-verifies on **every use** (C1-37): the
  path must still resolve to the pinned realpath and the whole file is re-hashed. A binary
  swapped behind an unchanged symlink shim, a retargeted link, or a same-size rewrite with
  the mtime restored is `binary_changed`; a deleted file or link is `binary_missing`.
  It returns `[realpath, *argv[1:]]`; spawning stays with the child-env launch boundary.
- `launch_check(path_string)`: one pass over all nine engines; one record each
  (`granted`, `refused` with code, or `absent`). Grants are returned apart from records.

Every failure is a `LaunchRefused` with a code from the closed set `REFUSAL_CODES`
(`r2a-refusals/1.1.0-candidate-2026-11-09`, 17 codes). No path returns a grant or plan
after an error.

### Zero-egress version probe

`<realpath> --version` (for Hermes, the sibling `hermes-acp --version`, which skips the
network update check; its digest is pinned and re-verified too), argv as a list, no shell,
stdin `/dev/null`, `close_fds`, a new session killed as a group on timeout (20 s default),
output read in memory and capped at 64 KiB while it runs (nothing is written to disk). A
process that leaves the probe's session and keeps the output open past the timeout refuses
`version_probe_failed` instead of hanging the check. The environment is built from nothing: `PATH=/usr/bin:/bin`,
`HOME`, `TMPDIR` and `XDG_*` inside a fresh temp dir removed afterwards, `LC_ALL=C`, plus
Claude Code's `DISABLE_AUTOUPDATER=1` and `DISABLE_UPDATES=1`. No proxy, credential,
config-path or `CLI_VERSION` variable can reach it. Limit: this is not a kernel network
block; a network namespace belongs to the launch sandbox (B6), not this check.

## Floor table `r2a-floors/1.2.0-candidate-2026-11-09`

Two slots per engine. A grant needs `found >= security` when a security floor is set.
When a functional floor is set, it also needs `found >= functional`. With no functional
floor the engine still runs and the grant is tagged `beta` for route.list (O50, "Beta
until measured"). A floor change is a new table version (grants under the old version
become `grant_stale`). `floor_unset` remains in the closed refusal set for a table
floor that does not parse. An engine with no identity rule (no `ident` regex and no
pinned digest) is refused `identity_unverified` before its binary is run: a bare name plus a
semver line does not identify an engine.

| Engine (binaries) | Scheme | Functional floor (Q43) | Security floor (relayed) |
|---|---|---|---|
| claude_code (`claude`) | semver | 2.1.314 (decided, O50/Q43; not yet measured, CX-956) | 2.1.260 (11.6) |
| codex (`codex`) | semver | unset (beta until measured) | 0.131.0 (A-35, 11.6; security floor only) |
| opencode (`opencode`, `opencode2`) | semver | unset | 1.18.33 (D2-72) |
| grok (`grok`) | semver | unset (O50: once measured) | none |
| antigravity (`agy`) | semver (assumed) | unset | none |
| cursor (`agent`, `cursor-agent`) | date | unset | 2025.09.17-25b418f (D2-72) |
| gemini (`gemini`) | semver | unset | 0.39.1 (D2-72) |
| pi (`pi`) | semver (assumed); no identity rule: `identity_unverified` | unset | none |
| hermes (`hermes`, read via `hermes-acp`) | semver (assumed) | unset | none |

Codex: 0.131.0 is a security floor only. There is no measured functional floor, so Codex
runs as beta until measured (O50). `rust-v0.145.0` (first tag with all 15 `--disable`
keys, 11.6) is an **unmeasured candidate** and is not applied; measuring it is F17 work.
Claude Code keeps its 2.1.314 functional floor and 2.1.260 security floor.

Version parsing is strict. Semver: the first word holding `digit.digit` must be
`[v]MAJOR.MINOR.PATCH[-prerelease]` with no leading zeros; a prerelease is `version_refused`
(floors name releases); a fourth part, glued letters or `+build` is `version_unparsed`.
Cursor: `[v]YYYY.M.D[-HH-MM-SS][-<hex commit>]`, compared numerically as
(year, month, day, build time); the commit does not order. Lines over 256 characters are
unparsed. Real-shape lines covered: `2.1.318 (Claude Code)`, `codex-cli 0.160.0`,
`opencode v2.2.5`, `2026.10.01-e373342`, `hermes-acp 0.9.1`.

Known consequences at this head: Gemini CLI and Pi run JS bundles under node and Hermes is a
Python entry point (11.6), so under the native-binary rule they are `wrapper_refused`
(refused, not beta) until a launcher-plus-tree pin exists; Cursor likewise (no native
engine binary, #1087 section 6.2). So on a real install these four routes are refused.
Whether to build a launcher-plus-tree pin or accept four refused routes is open for Lucas.
Pi additionally needs an identity rule or pinned digest: a native `pi` is not the real Pi,
so it is `identity_unverified`. Antigravity (`agy`) keeps its identity rule (a line naming
another engine is refused) and is not changed here: its version line was never read, so
requiring it to name itself could refuse the real binary; its format remains an assumption.
The `beta` field is `true` exactly when no functional floor is set; Claude Code (2.1.314,
not yet measured, CX-956) therefore shows `beta: false`. What `beta` should mean is open
for Lucas.

## Limits

- The MAC key is per process; same-process code can read it. The boundary is the core's
  process and IPC edge; the MAC stops forged or edited grant objects reaching a plan.
- A swap between `exec_argv()` and the exec itself is still possible (ledger CX-1402).
  Holding the file open across the spawn (#1087 section 6.4, Windows spike) or `fexecve`
  on Linux is follow-up work.
- POSIX only in the tests. Windows detection here does not read npm `.cmd` shims, so on
  Windows such engines are not grantable (fail closed).

## Tests

From the repository root (Python 3.11+, standard library only; also passes on
free-threaded 3.14t with `-X gil=0`, the GIL stays off after import):

```sh
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/launch_check/tests -t core -v
```

Fixtures are fake engines built in temp dirs (`tests/fixtures.py`): with a C compiler, one
tiny program is compiled per run and each fixture is a byte-patched copy that prints a
given version line and exit code (and can dump its environment for the zero-egress test);
without one, copies of `/bin/true` plus a test-only probe seam, and the exec-only tests
skip. No real `claude`, `codex` or other engine, account, provider or network is used.
`tools/ci_gate.py` does not discover `core/` tests yet (its `TEST_GLOBS` edit is build
item 1, a protected-path change on its own issue).

## Evidence (links only; the research stays in csl-research)

- csl-research PR #1087, `submissions/20261106-csl732-launch-check-floors-nine-engines-01/result.md`
  at [`38208204`](https://github.com/CSL-Laboratories/csl-research/blob/382082045248f9beccace6ff781370a8ab4c89ff/submissions/20261106-csl732-launch-check-floors-nine-engines-01/result.md):
  the prototype these rules come from (refusal codes, wrapper rules, two-slot floor table
  0.2.0, strict parser, authenticated grant). Candidate, unreviewed.
- `submissions/20261101-csl460-wrapper-shape-gate-prototype-01` (pass bar shape),
  `submissions/20261102-csl657-cli-version-floor-grounding-01` (C1-36, C1-38, C1-40).
