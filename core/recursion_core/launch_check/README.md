# core/recursion_core/launch_check (CSL-732, B10, R2a; candidate)

The new core's R2a launch check: at launch, each detected engine's exact binary and
version is checked against a versioned floor table, failing closed. Candidate code: not
yet called by a running core, and not reviewed or accepted. Master `scripts/v3` is frozen
and untouched; its `LIVE_ADAPTERS_ENABLED` stays `False`.

Governing spec: `contracts/PRODUCT.md` at csl-research `055cc7a5c793b7135e5d0565dc4198cf96230531`
(SHA-256 `e64783152f6d67d4ede774dc41d112fe46c2d7536d53cb2c04e71a3566044daf`), section 3.3
"R2a defined" (grill Q22), O50 ("Beta until measured") and 11.6 (grill Q43, functional floors), 34b (nine engines).
This package ships eight of them: Lucas dropped the Gemini CLI row on 2026-11-09 (see
[Gemini CLI is not a route](#gemini-cli-is-not-a-route)).
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
  `grant` decides it against the pin table and runs nothing. On Windows each PATH directory
  is searched for the bare name and `.exe`, and for a pinned route only also `.cmd` then
  `.ps1`; elsewhere only the bare name is searched.
- `grant(engine, path)`: the only issuer of a `Grant`. In order: engine in the table
  (`engine_unknown`); absolute path (`path_not_absolute`); file stem is one of the engine's
  names (`name_mismatch`, or `wrapper_refused` for an interpreter or wrapper name); the path
  exists and is executable (`binary_missing`); the resolved file is native and not a
  wrapper name (`wrapper_refused`: shebang scripts, links to scripts, `.cmd`/`.ps1`/`.js`/
  `.sh`, text). The one exception: on Windows, a pinned route's `<name>.cmd` or
  `<name>.ps1` (the name exact, the extension in any case) goes to the pin check below
  instead of `wrapper_refused`. An engine with no identity rule and no pin is then refused
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
  run. A Windows `.cmd` or `.ps1` launcher is granted the same way and only this way: its
  SHA-256 must be a recorded launcher pin for that engine and the pinned tree must match,
  otherwise `pin_mismatch`. That launcher is a check only and is never executed: the grant
  records the pin's `entry` (each item a regular file inside the pinned tree, else
  `pin_mismatch`), and the grant is still issued when the pin has no entry. The same holds
  for a bare or `.exe` path that resolves to a script: check-only is decided here, at grant
  time, and recorded in the grant's `check_only` field (see
  [Check-only launchers](#check-only-launchers)). The grant also copies the pin's
  `interpreter`. A pin whose `source` is a vendor archive, or whose `entry` or
  `interpreter` is malformed, is never matched (`pin_mismatch`). See
  [Pinned routes](#pinned-routes).
- The grant record holds engine, path, realpath, sha256 (64 hex), size, raw version line,
  exit code, parsed version, both floors, a `beta` tag when the functional floor is
  unset, the floor-table version and the refusal-code version;
  for a pinned route also the pin kind, pin-table version, and the matched target and its
  digest, `check_only` (the launcher is a script that is checked and never executed),
  `entry` (for a check-only grant, the realpaths of the pin's entry items) and
  `interpreter` (the pin's interpreter outside the pinned tree, or empty). The grant is a frozen dataclass whose fields are MACed (HMAC-SHA-256) with a
  per-process key; `launch_plan` refuses anything else (`grant_forged`) and any grant issued
  under another table or code version (`grant_stale`), and re-checks the floors.
- `launch_plan(grant, argv, env)`: `argv` non-empty (`argv_empty`); `argv[0]` is the bare
  engine name (`argv0_mismatch`), never an interpreter or wrapper (`sh`, `bash`, `env`,
  `node`, `python`, `npx`, `uv`, `cmd`, `powershell`, `pwsh`, `wsl`, `.cmd`/`.ps1`/`.js`/
  `.sh` and others: `wrapper_refused`; this holds for a grant issued on a `.cmd` or `.ps1`
  launcher too, whose `argv[0]` is still the bare engine name); argv plus env under the byte caps, NUL-free text
  (`spawn_too_large`: 128 KiB per item, 256 KiB total). `env` is the child environment
  already built by the child-env boundary (recursion #320), not `os.environ`; it is the
  only environment a plan holds, `exec_env()` returns exactly it, and no `env` means an
  empty one (see [Environment](#environment-the-routes-child_env-only)). Then, here and on
  every plan use: a grant whose pin names an interpreter outside the pinned tree is refused
  (`interpreter_unpinned`), and a check-only grant whose pin records no entry is refused
  (`pin_entry_unset`), in that order.
- `LaunchPlan.exec_argv()` authenticates and re-verifies on **every use** (C1-37): the
  path must still resolve to the pinned realpath and the whole file is re-hashed. A binary
  swapped behind an unchanged symlink shim, a retargeted link, or a same-size rewrite with
  the mtime restored is `binary_changed`; a deleted file or link is `binary_missing`.
  For a pinned route the package tree or binary is re-hashed too, and the pin must still
  be recorded.
  It returns `[realpath, *argv[1:]]`; spawning stays with the child-env launch boundary.
  For a check-only grant (a pinned Windows `.cmd`/`.ps1` launcher, or a bare or `.exe` path
  that resolves to a script) it returns `[*entry, *argv[1:]]` instead,
  never the launcher's path: the launcher digest and the tree digest (which covers the
  entry files' bytes) are re-checked as above, and each entry path must still resolve to
  the same file inside the tree (`binary_changed`).
- `launch_check(path_string)`: one pass over all eight engines; one record each
  (`granted`, `refused` with code, or `absent`). Grants are returned apart from records.

Every failure is a `LaunchRefused` with a code from the closed set `REFUSAL_CODES`
(`r2a-refusals/1.7.0-candidate-2026-11-10`, 21 codes; 1.7.0 adds `interpreter_unpinned`:
the pin records that its launcher starts an interpreter from outside the pinned tree, which
is not hashed, so the launch is refused until external interpreters can be pinned; 1.6.0
added `pin_entry_unset`: a pinned script launcher was accepted as a check, but its pin
records no entry to execute, so the launch is refused until the entry is measured; 1.5.0 was the same 19
codes as 1.4.0 and moved a Windows `.cmd`/`.ps1` launcher on a pinned route from
`wrapper_refused` to the pin check, so it is `pin_mismatch` unless pinned; 1.4.0 removed
`sign_in_refused`, which only the dropped Gemini CLI row raised). No path returns a grant
or plan after an error.

The 21 codes: `engine_unknown`, `path_not_absolute`, `binary_missing`, `wrapper_refused`,
`name_mismatch`, `version_probe_failed`, `binary_changed`, `engine_mismatch`,
`identity_mismatch`, `identity_unverified`, `pin_mismatch`, `pin_entry_unset`,
`interpreter_unpinned`, `version_unparsed`, `floor_unset`, `version_refused`, `argv_empty`,
`argv0_mismatch`, `grant_forged`, `grant_stale`, `spawn_too_large`.

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

## Floor table `r2a-floors/1.5.0-candidate-2026-11-09`

Two slots per engine. A grant needs `found >= security` when a security floor is set.
When a functional floor is set, it also needs `found >= functional`. With no functional
floor the engine still runs and the grant is tagged `beta` for route.list (O50, "Beta
until measured"). A floor change is a new table version (grants under the old version
become `grant_stale`). `floor_unset` remains in the closed refusal set for a table
floor that does not parse. An engine with no identity rule (no `ident` regex and no
pin) is refused `identity_unverified` before its binary is run; no row is in that state
now. 1.5.0 removes the Gemini CLI row; no other row changed from 1.4.0.

Identity is checked positively only for the three engines whose real version line is
recorded and names the engine: claude_code (`2.1.318 (Claude Code)`), codex
(`codex-cli 0.160.0`) and opencode (`opencode v2.2.5`).
For these a line that does not match the engine's identity rule is `identity_mismatch`
(so a native `claude` printing `2.1.400` is refused). For pi, hermes, cursor and
antigravity the pin is the identity. For grok the real line was never read, and no format
is invented: a native binary with the right name printing a bare version that passes the
floors is granted; the only identity check is that the line names no other engine in the
table. Whether grok needs a pinned digest or is accepted as is, is open for Lucas.

| Engine (binaries) | Scheme | Functional floor (Q43) | Security floor (relayed) | Measured help |
|---|---|---|---|---|
| claude_code (`claude`) | semver | 2.1.314 (decided, O50/Q43; not yet measured, CX-956) | 2.1.260 (11.6) | #336 fixtures, one per measured build (see below) |
| codex (`codex`) | semver | unset (beta until measured) | 0.131.0 (A-35, 11.6; security floor only) | none measured |
| opencode (`opencode`, `opencode2`) | semver | unset | 1.18.33 (D2-72) | none measured |
| grok (`grok`) | semver | unset (O50: once measured) | none | none measured |
| antigravity (`agy`): Antigravity CLI, the Google subscription route; pinned beta route (`binary+launcher`; no pin recorded, so refused) | semver | unset | none | none measured |
| cursor (`agent`, `cursor-agent`): pinned beta route (`launcher+tree`) | date | unset | 2025.09.17-25b418f (D2-72) | none measured |
| pi (`pi`): pinned beta route (`launcher+tree`) | semver (assumed) | unset | none | none measured |
| hermes (`hermes`): pinned beta route (`launcher+tree`) | semver (assumed) | unset | none | none measured |

Agent (Grok Bot Liaison, for Lucas): Claude Code measured builds. Data only: nothing below
changes `floors.py`, the table version or any refusal (wrapper refusals and #336's
`--bare` refusal both wait on Lucas). "Measured help" is the full `claude --help` committed
on #336 (CSL-726) at `14f8b011`, measured on linux-x64 with
`env -i PATH=/usr/bin:/bin HOME="$HOME" LANG=C.UTF-8 COLUMNS=80 <claude> --help` (no login,
no model call); the full method and hashes are in #336's
[`bare-default-measured-table.md`](https://github.com/CSL-Laboratories/recursion/blob/14f8b0114b673193fe46cd4275f2112fc73c4aed/core/recursion_core/routes/tests/fixtures/bare-default-measured-table.md). "This table" is
`r2a-floors/1.5.0-candidate-2026-11-09` at this PR's head (the Claude Code floors are
the same as in 1.3.0 and 1.4.0).

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

Pin table `r2a-pins/1.5.0-candidate-2026-11-10` (1.5.0 adds the optional `interpreter`
field and sets it to `node (PATH)` on the two Pi rows, with no hash, size, target or entry
changed; 1.4.0 removed the archive-derived Antigravity CLI row and added the optional
`entry` field).
Lucas's owner call of 2026-11-09 (item 2),
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
- A `launcher+tree` pin may carry an `entry`: the program its launcher reaches, as argv
  items, each a relative `/`-separated path inside the package root (Cursor's would have
  the shape `("node.exe", "index.js")`; no such entry is committed). Empty means not
  measured. An entry on a `binary+launcher` pin, an absolute path, an empty item, a `..`
  that leaves the root or a non-tuple is a table bug: that pin is never matched
  (`pin_mismatch`).
- A pin may carry an `interpreter`: a measured fact that its launcher starts an interpreter
  from outside the pinned tree, named as the launcher resolves it (`node (PATH)`). Empty
  means none, or one inside the tree. The pin still matches and the grant is issued, and
  every launch on it is refused `interpreter_unpinned`. Anything but text is a table bug
  (`pin_mismatch`). Nothing outside the tree is hashed or looked up.

### Provenance: real installs only

Lucas's owner call of 2026-11-10 (csl-research PR #1159 comment 6777082374): "pins stay
measured from a real install only for Nov 11; no archive-derived pins".

- A pin is measured from the files of a real install. A vendor archive may be recorded as
  a cross-check of a measured pin, never as the pin's source.
- Enforced, fail closed: a pin whose `source` names a vendor archive is never matched.
  `pins.match` refuses it at grant time and `pins.listed` on every use of an existing
  grant, both as `pin_mismatch`. "Names an archive" means the source's path, ignoring any
  query or fragment, ends in `.tar.gz`, `.tgz`, `.tar.xz`, `.txz`, `.tar.bz2`, `.tbz2`,
  `.tar.zst`, `.tar`, `.zip` or `.7z`, in any case. `pins.measure` refuses such a source.
- The Antigravity CLI 1.5.3 linux_amd64 row of table 1.3.0 was hashed with `sha256sum`
  from the `antigravity` member of the vendor's `cli_linux_x64.tar.gz`, not from an
  installed `agy`, so it is removed. Antigravity CLI has no pin and is refused
  `pin_mismatch` on every platform until someone measures one on a real install. The
  `binary+launcher` pin kind and its logic stay.

Recorded pins (measured from real installs only; no hash here is invented; no entry has
been measured, so every `entry` is empty; both Pi rows carry `interpreter` `node (PATH)`,
because Pi's launchers run the Node found on PATH, and the Cursor row carries none, because
its `node.exe` is inside its tree):

| Engine | Version | Platform | Launcher SHA-256 (bytes) | Target | Target SHA-256 | Measured | Source |
|---|---|---|---|---|---|---|---|
| cursor | 2026.11.05-86047df | windows_amd64 | `b5975caa126e855f4be9b6084791a47eb40dd0babccb297265bf131df033acfc` (2527) | `versions/2026.11.05-86047df` (408 entries, 265684223 bytes, includes an empty `.running` directory) | `d9088fb279e9a004d4098b927df9b1257cd05b367cdc0959ba11d082e291bf23` | 2026-11-09 | launcher `C:\Users\Lucas\AppData\Local\cursor-agent\cursor-agent.ps1` on sentry, installed from https://cursor.com/install?win32=true |
| pi | 1.5.1 | windows_amd64 | `c56ef3cdd441b3c00d49e6e36d4a163bbcc715bf32e4de3473ece96efd232c4b` (464) | `../install/releases/1.5.1` (16776 entries, 113981899 bytes) | `2c477ce6abcd257434138b4affd6f9b12b92ec2527bc8eb50f04948a25323ac7` | 2026-11-09 | launcher `C:\Users\Lucas\.pi\agent\bin\pi.cmd` on sentry, installed from https://pi.dev/install.ps1; PowerShell resolves `pi` to `pi.ps1` first (SHA-256 `3f422e96483162aad373b8e3e28f282236bdffb5cc1b8bfb6c25a8d918bd31d1`, 500 bytes) |
| pi | 1.5.1 | windows_amd64 | `3f422e96483162aad373b8e3e28f282236bdffb5cc1b8bfb6c25a8d918bd31d1` (500) | `../install/releases/1.5.1` (16776 entries, 113981899 bytes) | `2c477ce6abcd257434138b4affd6f9b12b92ec2527bc8eb50f04948a25323ac7` | 2026-11-09 | launcher `C:\Users\Lucas\.pi\agent\bin\pi.ps1` on sentry, installed from https://pi.dev/install.ps1 |

The three `windows_amd64` rows were measured on Lucas's Windows machine "sentry" with the
hasher at commit `66fe11b1`. Lucas's owner call of 2026-11-09: "Yes, accept pinned .cmd and
.ps1 launchers on Windows, bound to the pin." So on Windows, for pinned routes only,
`detect.path_hits` tries the bare name, `.exe`, `.cmd` and `.ps1`, in that order, in each
PATH directory (the first hit of each name still counts). `grant()` accepts a `<name>.cmd`
or `<name>.ps1` launcher only when its SHA-256 matches a recorded launcher pin for that
engine and the pinned tree matches; any other `.cmd`/`.ps1` launcher on a pinned route
(unpinned, or one byte changed) is `pin_mismatch`, and a byte changed after the grant is
`binary_changed` on the next plan use. Non-pinned engines (`claude.cmd`, `codex.ps1`) and
every non-Windows platform are unchanged: `wrapper_refused`, as are other wrapper
extensions (`.bat`, `.js`, `.sh`) on a pinned route. Both Pi launchers are pinned over the
same tree, so the code does not choose between them beyond the search order. The Cursor
tree includes an empty `.running` directory, so its tree pin goes stale if Cursor writes
into it or removes it.

### Check-only launchers

Lucas's owner call of 2026-11-10 (same comment, calls 13/14): "accept a pinned .cmd/.ps1
launcher as a check, but execute the pinned entry directly". So the launcher is hashed and
never executed. His answer of 2026-11-10 (via Liaison): "Extend the check-only rule to a
bare or .exe granted path that resolves to a script (.cmd/.ps1, by content or resolution as
appropriate). It fails closed, with tests."

- Check-only is decided once, at grant time, and recorded in the grant's MAC-covered
  `check_only` field (`grant_forged` if edited, or if it is not a bool). A pinned route's
  grant (`launcher+tree` or `binary+launcher`) is check-only when any of these holds:
  - the granted path is a `<name>.cmd` or `<name>.ps1` (Windows only, as before);
  - by resolution, on any platform: the granted path is bare or `.exe` and its realpath
    ends in `.cmd` or `.ps1`, in any case (a `pi` or `pi.exe` link to `pi.ps1`);
  - by content, on Windows only: the granted path is bare or `.exe` and the resolved file
    is not a native executable (batch, PowerShell, shebang or other text behind a bare or
    `.exe` name). If the file cannot be read for this check the grant is refused
    (`binary_missing`).
- A check-only grant records the pin's entry as realpaths under the matched tree root.
  Each item must be a regular file whose realpath is inside that root, else
  `pin_mismatch`. With no entry in the pin the grant is still issued (the launcher check
  passed) with an empty `entry`.
- `launch_plan` and every plan use refuse such a grant with `pin_entry_unset` when it has
  no entry. With one, `exec_argv()` returns `[*entry, *argv[1:]]`, never the launcher's
  path, after re-checking the launcher digest, the tree digest (the entry files are inside
  the tree, so their bytes are covered) and that each entry path still resolves to the same
  file inside the tree (`binary_changed`). `argv[0]` of the request is still the bare
  engine name. The entry field is MAC-covered like every other grant field
  (`grant_forged` if edited).
- A granted path re-pointed after the grant (a link moved to another launcher or to a
  native file) no longer resolves to the granted realpath: `binary_changed` on the next
  use, so the decision made at grant time cannot be carried to another file.
- No `cmd.exe` or PowerShell is named or started anywhere.
- Unchanged: off Windows, a bare launcher that is a shebang script (the Linux
  `launcher+tree` shape, such as a `pi` shell script or a link to `cli.js`) is still
  executed by its realpath and its pin's entry is not used. A native bare or `.exe`
  launcher is unchanged on every platform, as is a `binary+launcher` pin whose launcher is
  the native binary. A `binary+launcher` pin behind a script launcher on Windows is
  check-only and, since such a pin can carry no entry, always `pin_entry_unset`.

### Interpreter outside the pinned tree

Lucas's answer of 2026-11-10 (via Liaison): "A Windows Pi launch whose interpreter (Node
from PATH) is outside the pinned tree must refuse with a new typed code
`interpreter_unpinned`. Hashing external interpreters is post-Nov-11; don't implement it."

- A grant copies its pin's `interpreter` into a MAC-covered field. When it is not empty,
  `launch_plan` and every plan use refuse `interpreter_unpinned`, whatever the launcher
  kind, after the argv and size checks and before `pin_entry_unset` (the more specific
  cause). The grant itself is still issued: the launcher check passed.
- Both committed Pi 1.5.1 rows record `node (PATH)`: Pi's `pi.cmd` and `pi.ps1` run the
  Node found on PATH (the installer's templates contain no path). No external interpreter
  is hashed, resolved or looked up.
- Cursor and Pi stay refused (`pin_entry_unset` / `interpreter_unpinned`) until entries
  are measured; sentry not touched.

### Environment: the route's child_env only

Lucas's answer of 2026-11-10 (via Liaison): "A launch uses the Recursion route's child_env,
never the environment the launcher would have set; nothing ambient is inherited, which is
Lucas's route-owned design on #320."

- `launch_plan(grant, argv, env)` takes the child_env built by `core/recursion_core/child_env`
  and holds only that. `exec_env()` returns exactly it. Nothing is read from `os.environ`
  or from the launcher, for a check-only grant as for any other, and a plan made with no
  `env` has an empty environment.
- Whatever variables a `.cmd`/`.ps1` launcher would have set are not reproduced. If an
  entry needs one, it belongs in the route's child_env.

Hermes v0.23.1 (`3b421578588243f920886e6fe79a6faa87ea026e`) was measured on Windows only
for its launchers (`hermes.exe`, `hermes-acp.exe`) and has no pin. Its Windows checkout
was 170870 entries (~2.85 GiB), over `MAX_TREE_ENTRIES` (100000), and stock `open()` hit
`MAX_PATH` under `node_modules/.pnpm`, so no tree hash exists and Hermes stays
`pin_mismatch`. Pi and Cursor on linux_amd64, or on any other platform or version, still
have no pin and are refused `pin_mismatch`. Antigravity CLI has no pin on any platform
and is refused the same way (see [Provenance](#provenance-real-installs-only)).
`install.sh` is the bootstrapper, not the launcher, and is not pinned.

To re-pin: measure a real install (never an archive member) with `pins.measure(engine,
kind, path, target, version=..., platform=..., measured=..., source=..., entry=...,
interpreter=...)` (it hashes; it runs nothing; `entry` is optional and is what the launcher
was read to start; `interpreter` is optional and names an interpreter the launcher starts
from outside the tree),
add the `Pin` to `PINS`, raise `PIN_TABLE_VERSION`, add the row above, and commit that on
its own, apart from logic, as #336 does for its measured help.

### Gemini CLI is not a route

Lucas's owner call of 2026-11-09: drop the Gemini CLI row. Background (Google sources are
cited in the PR body): Google stopped serving Gemini CLI for Google AI Pro, Ultra and free
individual accounts on 2026-06-18, and its replacement is Antigravity CLI (`agy`), the
pinned beta route above. This closes the earlier open item (pin the Gemini bundle or drop
the row).

- Gemini CLI is not an engine here: no floor row, no pin, no API-key route. `gemini` is
  not a binary name any row looks for.
- `grant("gemini", ...)` and `detect("gemini", ...)` are `engine_unknown`, as for any
  name outside the table. `launch_check` does not look for a `gemini` binary, so one left
  on PATH gets no record and no grant. A file named `gemini` passed to another row is
  `name_mismatch`.
- A version line saying `gemini` names no engine in the table. Rows whose line must name
  them (claude_code, codex, opencode) still refuse it (`identity_mismatch`).
- The `auth` statement on `launch_plan`, the `api_key_only` field and the
  `sign_in_refused` code existed only for the Gemini API-key row and are removed.

T3 Code driving Antigravity through Google's ACP agent is background only, not a route
this package ships.

## Limits

- The MAC key is per process; same-process code can read it. The boundary is the core's
  process and IPC edge; the MAC stops forged or edited grant objects reaching a plan.
- A swap between `exec_argv()` and the exec itself is still possible (ledger CX-1402).
  Holding the file open across the spawn (#1087 section 6.4, Windows spike) or `fexecve`
  on Linux is follow-up work.
- A pin covers the launcher and the package tree or binary, nothing else. An interpreter
  outside the tree is never hashed (post-Nov 11 work): a pin that records one refuses every
  launch (`interpreter_unpinned`). That record is a statement by whoever measures the pin;
  a pin that omits it is not detected. A symlink
  in the tree is pinned by its text, not by the file it points to; files an engine loads
  from outside the tree (config, plugins) are not covered.
- A tree that differs per machine needs its own pin: npm dependency resolution, absolute
  shebang paths in a venv, and `__pycache__` files (which are part of the digest, since
  Python runs them). An engine that writes into its own tree drifts to `binary_changed`.
- A whole package tree is re-hashed on every plan use; a large install costs that each time.
- Only installs that were measured can match. When an engine updates, its pin goes stale
  and the route is refused until re-pinned. Antigravity CLI has no pin now; when one is
  measured the same holds for it (its installer downloads from a host named as an
  auto-updater; whether `agy` replaces itself was not observed).
- The archive rule reads only the pin's `source` text. A pin hashed from an archive but
  recorded with another source is not detected; provenance still rests on whoever measures.
- The tests run on POSIX only. Windows is simulated in them through `binary.is_windows()`
  (the one switch `detect` and `grant` read); no test runs on real Windows. On Windows a
  non-pinned engine's npm `.cmd` shim or `.ps1` launcher is still not grantable (fail closed).
- A check-only launcher is never executed (Lucas, 2026-11-10: "accept a
  pinned .cmd/.ps1 launcher as a check, but execute the pinned entry directly").
  `exec_argv()` returns the pin's entry files and `argv[1:]`. The launcher's working
  directory and any arguments it adds between the entry and the user's are not reproduced.
  Its environment is not reproduced either, by rule: "A launch uses the Recursion route's
  child_env, never the environment the launcher would have set; nothing ambient is
  inherited, which is Lucas's route-owned design on #320."
- Cursor and Pi stay refused (`pin_entry_unset` / `interpreter_unpinned`) until entries are
  measured; sentry not touched. Committed pins carry no entry, and both Pi rows record the
  Node on PATH.
- The entry is a statement by whoever measures the pin of what the launcher starts. The
  code checks that the entry files are inside the pinned tree and unchanged, not that the
  launcher's text actually names them.
- A grant holds the entry realpaths, not the pin's relative items. A changed entry in the
  table reaches existing grants through the new `PIN_TABLE_VERSION` (`grant_stale`).
- A grant holds the pin's `interpreter` as issued, like the entry; a change in the table
  reaches existing grants through the new `PIN_TABLE_VERSION` (`grant_stale`).
- The content rule for a bare or `.exe` launcher is Windows only. Off Windows a bare
  launcher that is a script and does not resolve to a `.cmd`/`.ps1` name is executed by its
  realpath, so its shebang interpreter runs; that is the existing Linux `launcher+tree`
  behaviour.

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
launcher, and a Windows-shaped Pi whose fake tree holds its own `node.exe` so its pin can
carry an entry; the same fixture with a synthetic `interpreter` covers `interpreter_unpinned`,
and bare or `.exe` links and copies of its launchers cover the check-only rule), measure them in place and patch the pin table; none of those fake-install
pins is real. The committed rows in `PINS` are real measurements, checked value by value.
No real `claude`, `codex` or other engine, account, provider or network is used.
`tools/ci_gate.py` runs these files since CSL-795 (#334) merged: each `core/**/test_*.py`
is run on its own with `core/` on `PYTHONPATH`.

## Evidence (links only; the research stays in csl-research)

- csl-research PR #1087, `submissions/20261106-csl732-launch-check-floors-nine-engines-01/result.md`
  at [`38208204`](https://github.com/CSL-Laboratories/csl-research/blob/382082045248f9beccace6ff781370a8ab4c89ff/submissions/20261106-csl732-launch-check-floors-nine-engines-01/result.md):
  the prototype these rules come from (refusal codes, wrapper rules, two-slot floor table
  0.2.0, strict parser, authenticated grant). Candidate, unreviewed.
- `submissions/20261101-csl460-wrapper-shape-gate-prototype-01` (pass bar shape),
  `submissions/20261102-csl657-cli-version-floor-grounding-01` (C1-36, C1-38, C1-40).
