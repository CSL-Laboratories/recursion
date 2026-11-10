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
  otherwise the result is "not grantable". A pinned route (pi, hermes, cursor,
  antigravity) is the exception: its first PATH hit is offered whatever its kind, because
  `grant` decides it against the pin table and runs nothing.
- `grant(engine, path)`: the only issuer of a `Grant`. In order: engine in the table
  (`engine_unknown`); absolute path (`path_not_absolute`); file stem is one of the engine's
  names (`name_mismatch`, or `wrapper_refused` for an interpreter or wrapper name); the path
  exists and is executable (`binary_missing`); the resolved file is native and not a
  wrapper name (`wrapper_refused`: shebang scripts, links to scripts, `.cmd`/`.ps1`/`.js`/
  `.sh`, text). An engine with no identity rule and no pin is then refused
  (`identity_unverified`) without being run. It hashes the realpath (full SHA-256 and size), runs the zero-egress version
  probe, re-hashes (`binary_changed` if it changed while probing), and refuses a non-zero
  exit or timeout (`version_probe_failed`), a line naming another engine (`engine_mismatch`),
  for claude_code, codex and opencode a line that does not name the engine
  (`identity_mismatch`), an unparsed version (`version_unparsed`), or a prerelease or a version below a
  known floor (`version_refused`). An unset functional floor is not a refusal:
  the grant is tagged `beta` for route.list (O50).
- `grant` on a pinned route (pi, hermes, cursor, antigravity) takes another path after the
  name check: the path exists and is executable (`binary_missing`), then the launcher and
  its package files or binary must match a recorded pin (`pin_mismatch`, refused until
  re-pinned) and the pinned version must meet the floors (`version_refused`). Nothing is
  run. See [Pinned routes](#pinned-routes).
- The grant record holds engine, path, realpath, sha256 (64 hex), size, raw version line,
  exit code, parsed version, both floors, a `beta` tag when the functional floor is
  unset, `api_key_only` (Gemini CLI), the floor-table version and the refusal-code version;
  for a pinned route also the pin kind, pin-table version, and the matched target and its
  digest. The grant is a frozen dataclass whose fields are MACed (HMAC-SHA-256) with a
  per-process key; `launch_plan` refuses anything else (`grant_forged`) and any grant issued
  under another table or code version (`grant_stale`), and re-checks the floors.
- `launch_plan(grant, argv, env)`: `argv` non-empty (`argv_empty`); `argv[0]` is the bare
  engine name (`argv0_mismatch`), never an interpreter or wrapper (`sh`, `bash`, `env`,
  `node`, `python`, `npx`, `uv`, `cmd`, `powershell`, `pwsh`, `wsl`, `.cmd`/`.ps1`/`.js`/
  `.sh` and others: `wrapper_refused`); argv plus env under the byte caps, NUL-free text
  (`spawn_too_large`: 128 KiB per item, 256 KiB total). `env` is the child environment
  already built by the child-env boundary (recursion #320), not `os.environ`. For Gemini
  CLI, the one API-key-only row, the caller must also state `auth="api_key"`: a
  subscription sign-in, any other value or no statement is `sign_in_refused`, and the
  refusal names Antigravity CLI. The other rows ignore `auth`.
- `LaunchPlan.exec_argv()` authenticates and re-verifies on **every use** (C1-37): the
  path must still resolve to the pinned realpath and the whole file is re-hashed. A binary
  swapped behind an unchanged symlink shim, a retargeted link, or a same-size rewrite with
  the mtime restored is `binary_changed`; a deleted file or link is `binary_missing`.
  For a pinned route the package tree or binary is re-hashed too, and the pin must still
  be recorded.
  It returns `[realpath, *argv[1:]]`; spawning stays with the child-env launch boundary.
- `launch_check(path_string)`: one pass over all nine engines; one record each
  (`granted`, `refused` with code, or `absent`). Grants are returned apart from records.

Every failure is a `LaunchRefused` with a code from the closed set `REFUSAL_CODES`
(`r2a-refusals/1.3.0-candidate-2026-11-09`, 20 codes). No path returns a grant or plan
after an error.

### Zero-egress version probe

`<realpath> --version` (a row with `probe_name` reads a sibling binary instead, whose
digest is held and re-verified too; no row uses it since Hermes became a pinned route),
argv as a list, no shell,
stdin `/dev/null`, `close_fds`, a new session killed as a group on timeout (20 s default),
output read in memory and capped at 64 KiB while it runs (nothing is written to disk). A
process that leaves the probe's session and keeps the output open past the timeout refuses
`version_probe_failed` instead of hanging the check. The environment is built from nothing: `PATH=/usr/bin:/bin`,
`HOME`, `TMPDIR` and `XDG_*` inside a fresh temp dir removed afterwards, `LC_ALL=C`, plus
Claude Code's `DISABLE_AUTOUPDATER=1` and `DISABLE_UPDATES=1`. No proxy, credential,
config-path or `CLI_VERSION` variable can reach it. Pinned routes are never probed. Limit: this is not a kernel network
block; a network namespace belongs to the launch sandbox (B6), not this check.

## Floor table `r2a-floors/1.4.0-candidate-2026-11-09`

Two slots per engine. A grant needs `found >= security` when a security floor is set.
When a functional floor is set, it also needs `found >= functional`. With no functional
floor the engine still runs and the grant is tagged `beta` for route.list (O50, "Beta
until measured"). A floor change is a new table version (grants under the old version
become `grant_stale`). `floor_unset` remains in the closed refusal set for a table
floor that does not parse. An engine with no identity rule (no `ident` regex and no
pin) is refused `identity_unverified` before its binary is run; no row is in that state
now.

Identity is checked positively only for the three engines whose real version line is
recorded and names the engine: claude_code (`2.1.318 (Claude Code)`), codex
(`codex-cli 0.160.0`) and opencode (`opencode v2.2.5`).
For these a line that does not match the engine's identity rule is `identity_mismatch`
(so a native `claude` printing `2.1.400` is refused). For pi, hermes, cursor and
antigravity the pin is the identity. For grok and gemini the real line is bare (`0.39.1`)
or was never read, and no format is invented: a native binary with the right name printing
a bare version that passes the floors is granted; the only identity check is that the line
names no other engine. Whether those two need a pinned digest or are accepted as is, is
open for Lucas.

| Engine (binaries) | Scheme | Functional floor (Q43) | Security floor (relayed) | Measured help |
|---|---|---|---|---|
| claude_code (`claude`) | semver | 2.1.314 (decided, O50/Q43; not yet measured, CX-956) | 2.1.260 (11.6) | #336 fixtures, one per measured build (see below) |
| codex (`codex`) | semver | unset (beta until measured) | 0.131.0 (A-35, 11.6; security floor only) | none measured |
| opencode (`opencode`, `opencode2`) | semver | unset | 1.18.33 (D2-72) | none measured |
| grok (`grok`) | semver | unset (O50: once measured) | none | none measured |
| antigravity (`agy`): Antigravity CLI, the Google subscription route; pinned beta route (`binary+launcher`) | semver | unset | none | none measured |
| cursor (`agent`, `cursor-agent`): pinned beta route (`launcher+tree`) | date | unset | 2025.09.17-25b418f (D2-72) | none measured |
| gemini (`gemini`): Gemini CLI, API-key route only; no Google sign-in route, no pin | semver | unset | 0.39.1 (D2-72) | none measured |
| pi (`pi`): pinned beta route (`launcher+tree`) | semver (assumed) | unset | none | none measured |
| hermes (`hermes`): pinned beta route (`launcher+tree`) | semver (assumed) | unset | none | none measured |

Agent (Grok Bot Liaison, for Lucas): Claude Code measured builds. Data only: nothing below
changes `floors.py`, the table version or any refusal (wrapper refusals and #336's
`--bare` refusal both wait on Lucas). "Measured help" is the full `claude --help` committed
on #336 (CSL-726) at `14f8b011`, measured on linux-x64 with
`env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C.UTF-8 COLUMNS=80 <claude> --help` (no login,
no model call); the full method and hashes are in #336's
[`bare-default-measured-table.md`](https://github.com/CSL-Laboratories/recursion/blob/14f8b0114b673193fe46cd4275f2112fc73c4aed/core/recursion_core/routes/tests/fixtures/bare-default-measured-table.md). "This table" is
`r2a-floors/1.4.0-candidate-2026-11-09` at this PR's head (the Claude Code floors are
the same as in 1.3.0).

| Build | npm tag (2026-11-09 12:17 PST) | This table | Measured help (#336 fixture) | `--bare` default for `-p` (#336 `classify_help`) |
|---|---|---|---|---|
| 2.1.311 | `stable` | `version_refused` (below 2.1.314) | [`claude-2.1.311-help.txt`](https://github.com/CSL-Laboratories/recursion/blob/14f8b0114b673193fe46cd4275f2112fc73c4aed/core/recursion_core/routes/tests/fixtures/claude-2.1.311-help.txt) (identical to 2.1.312) | unknown, refused |
| 2.1.312 | none | `version_refused` (below 2.1.314) | [`claude-2.1.312-help.txt`](https://github.com/CSL-Laboratories/recursion/blob/14f8b0114b673193fe46cd4275f2112fc73c4aed/core/recursion_core/routes/tests/fixtures/claude-2.1.312-help.txt) | unknown, refused |
| 2.1.313 | never published | n/a | none | n/a |
| 2.1.314 | none (the floor) | passes floors | [`claude-2.1.314-help.txt`](https://github.com/CSL-Laboratories/recursion/blob/14f8b0114b673193fe46cd4275f2112fc73c4aed/core/recursion_core/routes/tests/fixtures/claude-2.1.314-help.txt) | unknown, refused |
| 2.1.315-2.1.318 | none | passes floors | same SHA-256 as 2.1.314, no separate file | unknown, refused |
| 2.1.319 | none (was `latest` on Nov 8) | passes floors | [`claude-2.1.319-help.txt`](https://github.com/CSL-Laboratories/recursion/blob/14f8b0114b673193fe46cd4275f2112fc73c4aed/core/recursion_core/routes/tests/fixtures/claude-2.1.319-help.txt) (identical to 2.1.314) | unknown, refused |
| 2.1.320 | never published (npm 404, native bucket 404) | n/a | none | n/a |
| 2.1.321 | `latest`, `next` | passes floors | [`claude-2.1.321-help.txt`](https://github.com/CSL-Laboratories/recursion/blob/14f8b0114b673193fe46cd4275f2112fc73c4aed/core/recursion_core/routes/tests/fixtures/claude-2.1.321-help.txt) (identical to 2.1.314) | unknown, refused |

npm `stable` is 2.1.311, below the 2.1.314 functional floor, so a user who installs the
`stable` channel is refused `version_refused` here. Measured 2026-11-09 12:17 PST: this PR's
`grant("claude_code", …)` on the real npm binaries refused 2.1.311 (`version_refused`) and
granted 2.1.319 and 2.1.321 (version lines `2.1.319 (Claude Code)`, `2.1.321 (Claude Code)`).
"Passes floors" for the other rows is `check_floors` on the version line only. A build that
passes these floors is still refused on the Claude subscription route by #336 until its help
settles the `--bare` question; that pick is Lucas's.

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

The `beta` field is `true` exactly when no functional floor is set; Claude Code (2.1.314,
not yet measured, CX-956) therefore shows `beta: false`. What `beta` should mean is open
for Lucas.

## Pinned routes

Pin table `r2a-pins/1.0.0-candidate-2026-11-09`. Lucas's owner call of 2026-11-09 (item 2),
with the same rules as the route layer on #321 (`wrapper_pin.py` at `38f41d3b`). Pi, Hermes and Cursor are wrapper installs (JS
bundles under node, a Python entry point; 11.6, #1087 section 6.2) and Antigravity CLI is
one Go binary. These four are granted only when the install on disk matches a pin in
`pins.py`, and then run as beta. Nothing is executed to decide this: the pin is the
engine's identity and its version. Anything else is `pin_mismatch`, refused until someone
re-pins.

| Engine | Pin kind | What must match |
|---|---|---|
| pi, hermes, cursor | `launcher+tree` | SHA-256 of the launcher (the file the engine name resolves to) and `tree_sha256` of the installed package root |
| antigravity (`agy`) | `binary+launcher` | the pinned version, plus SHA-256 of the launcher and of the native `agy` binary it starts (one file when `agy` is the binary itself); no package files |

- `tree_sha256(root)` covers every entry under the package root in sorted order: each
  file's path, owner exec bit and SHA-256, each symlink's text (links are never followed)
  and each directory. A file added, removed, renamed, edited or made executable changes
  it. Any other file type, more than 100,000 entries or more than 4 GiB is refused.
- A pin's `target` is a relative path from the launcher's directory to the package root
  or binary (`.`, `..`, `../lib/x`); it is empty when the launcher is the binary.
- The pinned version must parse and meet the engine's floors, on grant and on every plan
  use (`version_refused`).
- On every plan use the launcher is re-hashed and the tree or binary is re-hashed in full
  (`binary_changed` on drift). The pin must still be in the table (`pin_mismatch`) under
  the same pin-table version (`grant_stale`).
- A native file that only has the right name and prints a good version line is not a pin:
  `agent`, `agy`, `hermes` and `pi` are `pin_mismatch` unless a pin matches. The
  `hermes-acp` version read is no longer used by any row.

Recorded pins (measured from real installs only; no hash here is invented):

| Engine | Version | Platform | Launcher SHA-256 (bytes) | Target | Target SHA-256 | Measured | Source |
|---|---|---|---|---|---|---|---|
| none recorded | | | | | | | |

With no pin recorded, Pi, Hermes, Cursor and Antigravity are all refused `pin_mismatch`
on a real install.

To re-pin: measure the install with `pins.measure(engine, kind, path, target, version=...,
platform=..., measured=..., source=...)` (it hashes; it runs nothing), add the `Pin` to
`PINS`, raise `PIN_TABLE_VERSION`, add the row above, and commit that on its own, apart
from logic, as #336 does for its measured help.

### Gemini CLI and Antigravity CLI

Google stopped serving Gemini CLI for Google AI Pro, Ultra and free individual accounts on
2026-06-18; signing in with Google no longer works there. The Google subscription route is
Antigravity CLI (`agy`), added above as a pinned beta route. The Gemini CLI subscription
sign-in route is dropped. Gemini CLI stays in the table as an API-key route only
(`api_key_only: true` in the table record and in its grant record):

- `launch_plan` on a Gemini grant needs `auth="api_key"`, on the first check and on every
  plan use. `subscription`, `oauth`, a Google sign-in, a Code Assist licence, any other
  value or no statement is `sign_in_refused`; the refusal says Gemini CLI is an API-key
  route only and names Antigravity CLI (`agy`). `auth` is the caller's statement: this
  package reads no credential and cannot see whether a key is paid (#321's
  `gemini_cli.py` records that statement).
- Gemini gets no pin. A real Gemini CLI is a JS bundle under node, so on a real install it
  is still `wrapper_refused`, and that refusal carries the same note. A native `gemini`
  that meets the 0.39.1 security floor is granted as beta, as before.

Whether to pin the Gemini bundle so the API-key route runs on a real install, or to drop
the row, is open for Lucas. The Google sources are cited in the PR body. T3 Code driving
Antigravity through Google's ACP agent is background only, not a route this package ships.

## Limits

- The MAC key is per process; same-process code can read it. The boundary is the core's
  process and IPC edge; the MAC stops forged or edited grant objects reaching a plan.
- A swap between `exec_argv()` and the exec itself is still possible (ledger CX-1402).
  Holding the file open across the spawn (#1087 section 6.4, Windows spike) or `fexecve`
  on Linux is follow-up work.
- A pin covers the launcher and the package tree or binary, nothing else. The interpreter a
  launcher names (`node`, `python`) is not pinned unless it sits inside the tree; a symlink
  in the tree is pinned by its text, not by the file it points to; files an engine loads
  from outside the tree (config, plugins) are not covered.
- A tree that differs per machine needs its own pin: npm dependency resolution, absolute
  shebang paths in a venv, and `__pycache__` files (which are part of the digest, since
  Python runs them). An engine that writes into its own tree drifts to `binary_changed`.
- A whole package tree is re-hashed on every plan use; a large install costs that each time.
- Only installs that were measured can match. When an engine updates (Antigravity CLI's
  installer downloads from a host named as an auto-updater; whether `agy` replaces itself
  was not observed), its pin goes stale and the route is refused until re-pinned.
- `auth` on a launch plan is a statement by the caller, not something this check observes.
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
skip. Pinned-route tests (`tests/test_pins.py`) build fake installs (an npm-shaped Pi, a
venv-shaped Hermes, a Cursor version directory, an `agy` binary with and without a
launcher), measure them in place and patch the pin table; none of those pins is real.
No real `claude`, `codex` or other engine, account, provider or network is used.
`tools/ci_gate.py` does not discover `core/` tests yet (its `TEST_GLOBS` edit is build
item 1, a protected-path change on its own issue).

## Evidence (links only; the research stays in csl-research)

- csl-research PR #1087, `submissions/20261106-csl732-launch-check-floors-nine-engines-01/result.md`
  at [`38208204`](https://github.com/CSL-Laboratories/csl-research/blob/382082045248f9beccace6ff781370a8ab4c89ff/submissions/20261106-csl732-launch-check-floors-nine-engines-01/result.md):
  the prototype these rules come from (refusal codes, wrapper rules, two-slot floor table
  0.2.0, strict parser, authenticated grant). Candidate, unreviewed.
- `submissions/20261101-csl460-wrapper-shape-gate-prototype-01` (pass bar shape),
  `submissions/20261102-csl657-cli-version-floor-grounding-01` (C1-36, C1-38, C1-40).
