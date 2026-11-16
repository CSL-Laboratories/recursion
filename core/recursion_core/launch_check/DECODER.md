# The child-side decoder: a design (lane LAUNCH-CHECK, first slice)

Design only. Nothing here is code, and nothing calls it. It answers the dependency recorded in
lane STOP's first-wave brief ("LAUNCH-CHECK: its child-side decoder merged before you apply its
line-419 change") and in the second-wave audit of this lane (`audits\wave2-1-codex-gpt-6.md`,
section 4: "isolated child-envelope version, size bounds, recheck and typed refusal channel, with
an acyclic decoder handoff"). Line numbers are master `fb578e31`.

## What happens today

- `daemon/launcher.py` builds the child environment (`child_env`), writes it to a FIFO in the run
  directory as one length-prefixed JSON object of strings, and starts a transient unit whose main
  process is `python -I -S daemon/engine_exec.py <env fifo> -- <executable> <argv...>`
  (`unit_argv`, launcher.py:411-412).
- `daemon/engine_exec.py` (standard library only, nothing from `recursion_core`) reads at most
  `MAX_ENV_BYTES = 60000` bytes, checks the shape, and calls `os.execve(executable, argv, env)`.
  Any failure exits 127 and writes only the exception's class name to the unit's stderr.
- Nothing on today's daemon path calls this package: `LaunchPlan.exec_argv()` is not called at this
  head. Once wired, the parent's last check is the seam S10 hook, and between it and `execve` the
  unit is created and the helper starts: the window of CX-1402. The child has no grant to check
  against and no way to say why it refused.

## Two refusals that must stay apart

| Where | What it means | How it travels |
|---|---|---|
| Parent, seam S10 (`launch_check(ctx, executable)`), before the run directory's files exist | **no unit started** | `start_failed` with `start_error` `{"code": "launch_refused", "detail": "launch_check:binary_changed"}` (the cause in the detail) |
| Child, this decoder, inside the unit, before `execve` | **no engine executed** (the unit did start: the helper is its main process) | the status channel below, then `start_failed` with `start_error.code` `launch_refused` and the child's code in the detail (the exact detail text is for STOP and SPINE to fix) |

A test or a surface never says "no unit started" for the second row. The `code` is a `code_word`
(`^[a-z][a-z0-9_]{0,63}$`, #368 `common.v1.json`), so a colon never goes there: at #370 the
launcher already wraps the S10 diagnostic as `RouteError(LAUNCH_REFUSED, detail)`. Storing
`start_error` in the run record is STOP's work, not this lane's.

## The envelope, version `launch-envelope/1`

The parent writes ONE length-prefixed JSON object to the FIFO that carries the environment today
(the header stays an ASCII decimal length and `\n`; the body stays at most 60000 bytes). A FIFO's
capacity is not guaranteed to hold the frame (it can be lowered per descriptor; #424 round 1 measured
a 4096-byte FIFO taking 4096 of 60006 bytes in one non-blocking write), so neither side assumes one
write: the parent writes in a loop that handles partial writes and `EAGAIN`, under a deadline and
the launch's cancellation, and refuses the launch (`launch_refused`, cause in the detail) if the
whole frame is not written; the child reads until the header's length, end of file or its own
deadline, and refuses a short frame (`envelope_malformed`).

```
{"v": "launch-envelope/1",
 "env": {"NAME": "value", ...},                  # exactly today's environment object
 "exec": {"path": "<realpath>", "size": <int>, "sha256": "<64 hex>"},
 "argv0": "<bare engine name>",
 "grant": {"engine": "...", "floor_table": "...", "pin_table": "...", "refusal_codes": "..."}}
```

- `exec.path` is what `LaunchPlan.exec_argv()[0]` returned: the granted realpath, or for a
  check-only grant the first entry. `size` and `sha256` are the grant's (for an entry, the digest
  the parent computes of that file when it builds the plan). No other field; no untyped field.
- The child refuses, without executing anything, an envelope that: is not valid UTF-8 JSON; is
  over the size bound or truncated; repeats a key at any level; has a key not listed above or
  misses one; holds a value of the wrong JSON type (a number for a string, a float or bool for
  `size`, a `sha256` that is not 64 lowercase hex); or names a version other than
  `launch-envelope/1`. Duplicate keys are found with `object_pairs_hook`, not by trusting
  `json.loads`' last-wins rule.
- The executable argument on the unit's command line must equal `exec.path`; a mismatch is a
  refusal, so the unit's visible command line and the checked file cannot differ.

## The re-check in the child

In this order, all with the standard library:

1. `fd = os.open(exec.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)`; `fstat(fd)` must be a regular file
   of exactly `exec.size` bytes with an execute bit for the unit's user (`binary_missing` /
   `binary_changed`).
2. Hash the whole file through `fd` (never by path again) and compare with `exec.sha256`
   (`binary_changed`).
3. Execute the bytes that were hashed. Three options, one to be chosen with STOP and the Liaison:
   - (a) `os.execve(fd, argv, env)` (Linux `fexecve`): executes the same inode that was hashed. A
     writer can still change that inode between step 2 and the exec; Linux refuses the exec with
     `ETXTBSY` only while a writer holds it open.
   - (b) copy the hashed bytes into a `memfd`, seal it against writes and `fexecve` the memfd:
     executes exactly the bytes hashed. It ends CX-1402 on Linux at the cost of one copy per launch
     (`agy` is 226 MB) and of `/proc/self/exe` naming a memfd.
   - (c) `os.execve(exec.path, ...)` as today: the window stays open, as narrow as the child can make it.
   The recommendation is (b) up to a size bound and (a) above it, each a separately tested path.
   Windows has no unit and no FIFO; its launch path is lane WINDOWS' and is not designed here.
4. Only then `execve`, with `argv0` as `argv[0]` and exactly `env`.

## The status channel

Exit codes cannot carry the refusal: the engine's own exit codes overlap them once it runs. The
parent creates a second FIFO, `launch.status`, beside the environment FIFO, and passes it on the
unit's command line. The child opens it write-only with `O_CLOEXEC` and writes exactly one line:

- `{"v":"launch-status/1","outcome":"refused","code":"<code>"}` then exits 127, where `<code>` is
  from this package's closed refusal set plus one new code, `envelope_malformed`; or
- `{"v":"launch-status/1","outcome":"intent"}` immediately before `execve`: an intent, not a proof.
  `O_CLOEXEC` closes the FIFO when the exec succeeds; if `execve` returns, the helper still holds
  the FIFO and writes `{"v":"launch-status/1","outcome":"exec_failed","errno":<n>}`, then exits 127.

What the parent may conclude, read with a bound:

| Read | Conclusion |
|---|---|
| "refused" | no engine executed; the code is the cause |
| "intent", then "exec_failed" | no engine executed; the errno is the cause |
| "intent", then end of file | **outcome unknown**: the exec succeeded, or the helper died between the line and the exec |
| end of file with no line, or a malformed line | outcome unknown: the helper died before deciding; the launch is refused (`launch_check_failed`) |

"Intent" followed by end of file does not prove execution, and neither the engine's first output
nor its exit status settles it (an engine may print nothing, or exit 127 on its own). An
affirmative observation is needed to record "executed": while the unit's main process is alive,
`/proc/<MainPID>/exe` names the checked file (same device and inode as the descriptor the child
hashed). When that cannot be read in time (an engine that exits at once), the run records the
outcome as unknown, never as a launch that happened. Which observation STOP's launcher takes is
STOP's to settle.

## Who changes what

| Change | File | Keeper |
|---|---|---|
| encoder, decoder and re-check as one standard-library module, tested here | `launch_check/envelope.py` (new) | this lane, every phase |
| the helper calls the decoder (loaded by file path under `-I -S`, so no `recursion_core` import) | `daemon/engine_exec.py` | STOP until wave 2, then this lane (ownership.json:3175-3208) |
| the parent writes the envelope and reads the status FIFO | `daemon/launcher.py` (the payload line STOP's brief cites as 419 at `5ee9cb38`; 453 on master) | STOP |
| `envelope_malformed` in the refusal set | `launch_check/refusals.py` | this lane (a new `REFUSAL_CODES_VERSION`) |

The decoder lands first, called by nothing, with its own tests; STOP applies its line after it
merges, as STOP's brief already orders.

## Tests the decoder needs (written with it, each red first)

Envelope: truncated header, header over 8 digits, body shorter than its header, body over 60000
bytes, invalid UTF-8, a key twice at the top level and inside `exec`, an unknown key, each field
of the wrong JSON type, `size` as a float or a bool, an uppercase or 63-character digest, another
version string. Re-check: a same-size replacement between the parent's check and the child's
(mtime restored), a truncated binary, a deleted one, a path that became a symlink, an execute bit
removed, a binary replaced between the child's hash and its exec (option (b) must execute the
hashed bytes; option (a) must at least refuse on `ETXTBSY`). Channel: the "exec" line followed by
end of file, the "refused" line, end of file with no line, a malformed line, a status FIFO the
parent never opens (the child must not block for ever: it opens with a bound); the helper dying
after "intent"; a failed `execve` after "intent"; a silent engine that exits 0 at once; an engine
that exits 127 on its own; a FIFO lowered to 4096 bytes and a reader that makes no progress (the
parent's bounded write must refuse, not hang). Every binary is a
fake (`tests/fixtures.py`, `tests/fake_binaries.py`); no real engine runs.

## Open, for the Liaison

- Option (a), (b) or (c) for step 3, and the size bound for (b).
- Whether the envelope may hold the digest of a check-only grant's entry, which the grant does
  not carry today (the parent would hash the entry when it builds the plan).
- The status FIFO's name and how the unit's command line passes it (STOP's file).
