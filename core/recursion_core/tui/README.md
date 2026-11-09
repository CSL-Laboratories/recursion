# tui: a full-screen client of the control API (CSL-799)

Candidate code for build item 8b. Spec: PRODUCT.md at csl-research `550cca0e4f10bcfc9b7610f104841d5f785defa8`
(SHA-256 `c5730c36c55208e0e54f5e0a0ae00381ba7f665231c86edbe05dd38a4c925ce8`), section 3.6, amendment A36,
lane G8. Operator presentation rules follow Cockpit O7 (`o7.operator.v0`).

## What it is

The TUI is a client of the same control API as the item 8 CLI (`recursion_core/control/`), the way
OpenCode's TUI is an HTTP client of its own server. It takes an injected `api` with
`call(method, params) -> dict` and owns no transport, session, run, approval or plugin state.

- `protocol.py`: the CLI's commands (`list`, `status RUN`, `stop RUN`,
  `start --tenant --engine --prompt [--client-key]`, `conditions`) and the same `run.*` request each
  request command sends. There is no approve, deny, order or mid-run verb. `keyed_conditions` turns an
  answer into text labels: `REFUSED`, `UNCERTAIN` (a `pending` stop), `STALE`, `UNRECONCILED` (a
  `stop_pending` run), `UNAVAILABLE`.
- `events.py`: the one ordered event log (requests, answers, condition edges, engine output chunks,
  notices, errors) and its JSON Lines form. Toolkit-neutral.
- `app.py`: `TuiApp` (view state, keys, frame, inline live rows, all fed through the log) and `Loop`
  (runs it on a thread for a real terminal).
- `sanitize.py`: engine output is untrusted. A streaming parser removes ESC, CSI, OSC (52 clipboard,
  0/2 title, 8 link), DCS, SOS, PM, APC, C0/C1 controls and bidi or invisible format characters, and
  shows each as a visible marker such as `⟦OSC 52⟧`. Its state carries across chunk boundaries.
- `view.py`: a frame is a list of `(text, style)` rows. It renders as plain text, as one full-screen
  ANSI repaint, or through `InlinePainter`. Colour is only decoration and is off under `NO_COLOR`.
- `term.py`: the terminal driver. Its three modes all read the same log:
  - default on a TTY: full screen on the alternate screen. The terminal size is polled every tick, so a
    resize repaints at the new size; there is no minimum size. Every current condition stays on screen:
    one row each when they fit, else one row per label naming each subject, else one row naming every
    label. Output, runs, the notice, the header and the status row give up their rows first.
  - `--no-alt-screen`: inline mode, preserving terminal scrollback history. Finished output is appended
    once as plain lines, so it lands in scrollback and can be copied. Only a small live region at the
    bottom (status, current condition labels, the latest error, notice, input) is redrawn. Keys work as
    in full screen. It never uses the full-screen painter, which homes the cursor and clears below.
  - `--plain` or `--screen-reader` (the same mode), `TERM=dumb`, or no TTY: the linear renderer for
    screen readers. One CLI-grammar command per line; it says each logged event's `speech` once, in
    order, with no redraw or cursor movement.
  - `--no-color` turns colour off in any mode, like `NO_COLOR`. Usage errors and a missing API client
    go to stderr; stdout carries only the screen or the transcript.

  The toolkit is this package's own small driver. No dependency is added; prompt_toolkit stays the
  candidate and Ratatui the fallback while O7 `cli-implementation-stack` is open.

## Behaviour

- Opening reads `run.list`. Attaching (`a`/Enter, or `status RUN`) reads `run.status`. Neither starts
  anything. Closing (`q`, Ctrl-C) sends nothing; runs keep going, and reopening reattaches.
- Stop: `s` fixes the target, and `y` sends `run.stop`. `stop RUN` on the command line also works. The
  screen shows the API's own answer: `status=stopped`, or `status=pending` plus an `UNCERTAIN` line.
  Stop needs no input lease and has its own request lane, so it never waits behind a slow read. Keys are
  handled before queued output each tick.
- Stale (snapshot older than `stale_after_s`, a failed refresh, or an answer marked `stale`) forces
  `watch_only`. Start is refused locally; reads and Stop stay available.
- Every request, answer, condition edge, engine output chunk, notice and error is an event in
  `TuiApp.log` before any renderer shows it. The full-screen frame and the linear renderer both read
  that log; neither renders from the other's frame or strings. `transcript` is the log's text.
- A condition is said only when it appears, changes or clears (`UNRECONCILED: ...`,
  `UNRECONCILED changed: ...`, `UNRECONCILED cleared, was: ...`), not again after every command. A
  usage error says only the error. `UNCERTAIN` for a run stays until the API shows that run is no longer
  `stop_pending`. The `conditions` command says the current set again on demand. The CLI has the same
  command (`recursion-control conditions`), which reads `run.list` because it keeps no state. No control
  API method was added.
- The latest error stays on its own row (and in the inline live region) until a newer error replaces
  it, so output never redraws it away. `NO_COLOR` frames and the linear renderer carry the same words.
- O7's `surface` allows only `gui` or `cli`, so the TUI labels itself `surface=cli` until O7 adds `tui`
  (A36). The control API has no surface field, and TUI requests are byte-identical to the CLI's.
- Approve and deny are not here. They wait for O11 and a later PR.

## Not wired yet

The control API has no event stream. Engine output reaches the TUI through `TuiApp.on_event` /
`Loop.post_event`, which tests feed from a fake driver. It also has no mid-run verbs (steer, queue,
interrupt, resume, fork) and no per-client event scoping. Those gaps are raised on CSL-209; this package
does not add them. No daemon serves the API, and the TUI has no packaged entry point
(`term.main(argv, api_factory)`).

## Tests

Fake driver only; no terminal, process, network or provider. From the repository root:

```
# CPython with the GIL
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/tui/tests -t core -v
# (a) free-threaded 3.14t as installed: no -X gil=0, PYTHON_GIL unset
env -u PYTHON_GIL PYTHONPATH=core python3.14t -B -m unittest discover -s core/recursion_core/tui/tests -t core -v
# (b) free-threaded 3.14t with the GIL forced off
PYTHONPATH=core python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/tui/tests -t core -v
```

Run (a) as well as (b): `-X gil=0` forces the GIL off, so it can never show a C extension turning it
back on. On a free-threaded build `test_imports` also starts a fresh interpreter with neither setting
and with `-W error::RuntimeWarning`, loads the TUI and the CLI, and checks the GIL is still off.

`tests/fixtures/*.json` (protocol parity, canned answers, the UI-10 / RJ-F21 injection corpus) and
`tests/transcripts/` are plain data, so a later TUI in another language can reuse them. The transcripts
hold one JSON Lines event log per condition (`refusal`, `uncertainty`, `stale_watch_only`,
`unreconciled`, `unavailable`, `error_stays`) with the fields `seq`, `time_rel_ms` (fake clock),
`event_type`, `source` (`operator`, `recursion`, `engine`), `trust` (`untrusted` for engine text),
`state`, `text`, `speech` and `focus`, plus the screen-reader transcript and the `NO_COLOR` frames, both
rendered from the same kind of log. They are the CI form of O7's TUI evidence; real Orca captures are
still owed before the TUI ships.
