# tui: a full-screen client of the control API (CSL-799)

Candidate code for build item 8b. Spec: PRODUCT.md at csl-research `550cca0e4f10bcfc9b7610f104841d5f785defa8`
(SHA-256 `c5730c36c55208e0e54f5e0a0ae00381ba7f665231c86edbe05dd38a4c925ce8`), section 3.6, amendment A36,
lane G8. Operator presentation rules follow Cockpit O7 (`o7.operator.v0`).

## What it is

The TUI is a client of the same control API as the item 8 CLI (`recursion_core/control/`), the way
OpenCode's TUI is an HTTP client of its own server. It takes an injected `api` with
`call(method, params) -> dict` and owns no transport, session, run, approval or plugin state.

- `protocol.py`: the CLI's four commands (`list`, `status RUN`, `stop RUN`,
  `start --tenant --engine --prompt [--client-key]`) and the same `run.*` request each one sends. There is
  no approve, deny, order or mid-run verb. `answer_conditions` turns an answer into text labels:
  `REFUSED`, `UNCERTAIN` (a `pending` stop), `STALE`, `UNRECONCILED` (a `stop_pending` run), `UNAVAILABLE`.
- `app.py`: `TuiApp` (view state, keys, frame) and `Loop` (runs it on a thread for a real terminal).
- `sanitize.py`: engine output is untrusted. A streaming parser removes ESC, CSI, OSC (52 clipboard,
  0/2 title, 8 link), DCS, SOS, PM, APC, C0/C1 controls and bidi or invisible format characters, and
  shows each as a visible marker such as `⟦OSC 52⟧`. Its state carries across chunk boundaries.
- `view.py`: a frame is a list of `(text, style)` rows. It renders as plain text, or as ANSI where
  colour is only decoration and is off under `NO_COLOR`.
- `term.py`: the terminal driver. It draws full screen on a TTY. With `--plain`, `TERM=dumb` or no TTY
  it uses line mode for screen readers: one CLI-grammar command per line, append-only output.

## Behaviour

- Opening reads `run.list`. Attaching (`a`/Enter, or `status RUN`) reads `run.status`. Neither starts
  anything. Closing (`q`, Ctrl-C) sends nothing; runs keep going, and reopening reattaches.
- Stop: `s` fixes the target, and `y` sends `run.stop`. `stop RUN` on the command line also works. The
  screen shows the API's own answer: `status=stopped`, or `status=pending` plus an `UNCERTAIN` line.
  Stop needs no input lease and has its own request lane, so it never waits behind a slow read. Keys are
  handled before queued output each tick.
- Stale (snapshot older than `stale_after_s`, a failed refresh, or an answer marked `stale`) forces
  `watch_only`. Start is refused locally; reads and Stop stay available.
- Every request, answer and error is appended to `transcript`. The latest error stays on its own row
  until a newer error replaces it, so output never redraws it away.
- O7's `surface` allows only `gui` or `cli`, so the TUI labels itself `surface=cli` until O7 adds `tui`
  (A36). The control API has no surface field, and TUI requests are byte-identical to the CLI's.

## Not wired yet

The control API has no event stream. Engine output reaches the TUI through `TuiApp.on_event` /
`Loop.post_event`, which tests feed from a fake driver. It also has no mid-run verbs (steer, queue,
interrupt, resume, fork) and no per-client event scoping. Those gaps are raised on CSL-209; this package
does not add them. No daemon serves the API, and the TUI has no packaged entry point
(`term.main(argv, api_factory)`).

## Tests

Fake driver only; no terminal, process, network or provider. From the repository root:

```
PYTHONPATH=core python3 -B -m unittest discover -s core/recursion_core/tui/tests -t core -v
PYTHONPATH=core python3.14t -X gil=0 -B -m unittest discover -s core/recursion_core/tui/tests -t core -v
```

`tests/fixtures/*.json` (protocol parity, canned answers, the UI-10 / RJ-F21 injection corpus) and
`tests/transcripts/*.txt` (NO_COLOR frames, screen-reader transcript) are plain data, so a later TUI in
another language can reuse them.
