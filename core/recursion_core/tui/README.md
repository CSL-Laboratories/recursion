# tui: a full-screen client of the control API (CSL-799)

Candidate code for build item 8b. Spec: PRODUCT.md at csl-research `550cca0e4f10bcfc9b7610f104841d5f785defa8`
(SHA-256 `c5730c36c55208e0e54f5e0a0ae00381ba7f665231c86edbe05dd38a4c925ce8`), section 3.6, amendment A36,
lane G8. Operator presentation rules follow the operator plugin's O7 (`o7.operator.v0`).

## What it is

The TUI is a client of the same control API as the item 8 CLI (`recursion_core/control/`), the way
OpenCode's TUI is an HTTP client of its own server. It takes an injected `api` with
`call(method, params) -> dict` and owns no transport, session, run, approval or plugin state.

- `protocol.py`: the CLI's commands (`list`, `status RUN`, `stop RUN`,
  `start --tenant --engine --prompt [--client-key]`, `conditions`) and the same `run.*` request each
  request command sends. There is no approve, deny, order or mid-run verb. `keyed_conditions` turns an
  answer into text labels: `REFUSED`, `UNCERTAIN` (a `pending` stop, or a `start_unconfirmed` launch,
  which may be running), `STALE`, `UNRECONCILED` (a
  run with an unconfirmed Stop: `stop_pending`, or any state but `stopped` that carries a stop record,
  such as a `starting` run whose Stop the API kept as intent with `cgroup_unknown`), `UNAVAILABLE`.
- `events.py`: the one ordered event log (requests, answers, condition edges, engine output chunks,
  notices, errors) and its JSON Lines form. Toolkit-neutral.
- `app.py`: `TuiApp` (view state, keys, frame, inline live rows, all fed through the log) and `Loop`
  (runs it on a thread for a real terminal).
- `sanitize.py`: engine output is untrusted. A streaming parser removes ESC, CSI, OSC (52 clipboard,
  0/2 title, 8 link), DCS, SOS, PM, APC, C0/C1 controls, bidi controls, and other invisible characters
  (Unicode format, private-use and unassigned, the line and paragraph separators U+2028 and U+2029,
  variation selectors, and the four blank Hangul fillers), and shows each as a visible marker such as
  `⟦OSC 52⟧`. Its state carries across chunk boundaries; bytes that end inside a UTF-8 character are
  shown as U+FFFD at the end of the stream, not dropped.
- `keys.py`: terminal input to key names. A stateful parser (state carries across reads) yields every
  printable character in order, CSI and SS3 keys by name (arrows, Home, End, Page Up/Down, Insert,
  Delete, Shift+Tab, F1 to F12, with Ctrl/Alt/Shift modifiers), `alt+<key>` for ESC and a key,
  `ctrl+<letter>` for control characters, a lone Esc after a short wait (`ESC_WAIT_S`, 50 ms), and a
  bracketed paste as one `Paste` event. A sequence that names no key is dropped, never read as Esc.
- `keymap.py`: one table of action ids (`ACTIONS`), default bindings by context (`global`, `runs`,
  `output`, `command`, `help`), a matcher with a leader (`Ctrl+X`) and no timers (within a context no
  binding is a prefix of another; a pending sequence waits for its next key, Esc cancels it), and the
  binding file. Each action names its `--plain` command, or why the linear renderer needs none.
- `sessions.py`: the session list and a session's transcript: the request frames (SPINE's recorded ones),
  the rows a transcript line prints (its own text, sanitized as engine output is), and the cursor a read
  goes on from. No request is sent from here, and no event is projected or parsed.
- `view.py`: a frame is a list of `(text, style)` rows. It renders as plain text, as one full-screen
  ANSI repaint, or through `InlinePainter`. Colour is only decoration and is off under `NO_COLOR`.
- `term.py`: the terminal driver. Its three modes all read the same log:
  - default on a TTY: full screen on the alternate screen. The terminal size is polled every tick, so a
    resize repaints at the new size; there is no minimum size. Every current condition stays on screen,
    and a condition's text or run id wraps onto more rows instead of being cut at the width: each
    condition in full when they fit, else one row per label with every subject (run id) named whole on
    the rows below it, else every label's row with as many names as fit (UNCERTAIN first) and a row
    saying how many are not named, else one row naming every label. Output, runs, the notice, the
    header and the status row give up their rows first.
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
  anything. Closing (`q`, Ctrl-C, or the terminal ending input) sends no new request and exits 0: a
  request still queued and not begun is not sent, except a Stop you confirmed, which is sent while the close's
  bound lasts. When `q` or Ctrl-C comes while a confirmed Stop has no answer yet, the TUI waits for that answer so
  the transcript shows it; any other key while it waits says so again, once, and a second Ctrl-C closes at once.
  The whole close, the wait and the end of the TUI's threads together, ends within ONE bound (`CLOSE_BOUND_S` in
  `app.py`, 5 s as shipped; it is to become a row of the settings file). The close line says what became of each
  Stop: answered and confirmed; answered and NOT confirmed (a failed call, `pending`, or an answer for another run:
  the run may still be running); sent with no answer yet; or not begun, which is sent only if the Stop ahead of it
  ends within what is left of the bound, and else is not sent. Each run whose Stop is not confirmed is named with
  `run status` as what to do. In full screen the close line is printed again after the screen is left. It also
  names every other request not sent; other runs keep going, and reopening reattaches. An API client that cannot be built is `control_api_unavailable` on
  stderr with exit 1, as in the CLI.
- Keys (all from the keymap, so a binding file can change them; `?` lists the ones in force, and the
  prompt row's hints follow the keymap):
  - run list: `j`/`k` or arrows select, `a`/Enter attach, `r` refresh, `s` stop (asks `y`), `Tab` the
    output pager, `?` keys, `:` or `/` the command line, `q` or `Ctrl+D Ctrl+D` close (runs keep going);
  - leader `Ctrl+X`, then: `k` stop (asks `y`), `r` refresh, `n` the command line with `start `, `c` the
    conditions again, `s` the sessions, `q` close. While a sequence waits, the prompt row lists what can follow; Esc
    cancels it, and an unbound key after `Ctrl+X` is said and does nothing;
  - output pager (it counts display rows, so every row of a long wrapped line can be reached, and a
    scrolled view stays on its rows while output arrives, a partial line begun or finished included): `j`/`k`
    a row, `Ctrl+D`/`Ctrl+U` or Page Down/Up half a page, `g g`/Home the oldest row kept, `G`/End the
    newest (and follow); another run (attached, or by `status RUN`) starts at its newest rows; `/` search older lines (case-insensitive unless the
    pattern has a capital), `n`/`N` the next older or newer match, `Tab`, Esc or `q` back to the run list;
  - Esc only cancels local UI (a line, the help, a pending sequence, the pager focus, a Stop
    confirmation); it never sends anything. No key approves anything, there is no `Ctrl+Y`, and no global
    `Ctrl+P`. Ctrl-C is not the keymap's: it ends the TUI from any mode, and no binding can take it. A
    binding file can bind Esc only alone and only to the cancel of its context (`line.cancel`, `help.close`,
    `focus.runs`), and cannot remove it there, not even with `[safety] never_bind` (a file that lists Esc
    there is not used); with a sequence pending, Esc cancels it and then does that;
  - the keys (`?`): `j`/`k` a row, Page Down/Up or `Ctrl+D`/`Ctrl+U` a page, `g g`/Home the first, `G`/End
    the last, so every entry can be read in full screen (on a short terminal the keys take rows from the run
    list until three entries show; with no row for an entry the header says so instead of naming a range,
    and with no row for the header either the prompt row does, on any terminal of two rows or more); Esc,
    `q` or `?` closes them.
- Commands (`:` or `/`, and `--plain`): the CLI's `list` (also `runs`), `status RUN`, `stop RUN`,
  `start ...`, `conditions`, and the TUI's own `help` (also `keys`) and `quit`, which send nothing. A
  leading `/` is accepted. `sessions` reads `session.list`; `transcript SESSION` reads that session's
  transcript.
- Sessions and transcripts (build step 4). `sessions` (or `Ctrl+X s`) reads `session.list` with SPINE's
  recorded request (`state: "live"`, `limit: 20`) and logs one row per session in the record's own words;
  in full screen the output pane shows that list (or "no sessions listed", or what the daemon answered
  instead), and the pager pages and searches it.
  `transcript SESSION` reads `events.read` with `projection: "transcript"` from the start (epoch 1, seq 0),
  follows `more` page by page (at most 50 reads in a row, the first included; then it says how to read on),
  and logs each line
  once, in seq order, as the projection gives it: its text, sanitized as engine output is (engine lines are
  logged as untrusted text), and a `replay.gap` line like any other. The TUI never projects or parses an
  event, and a line never makes it send anything. Full screen shows the lines in the output pane (the
  pager pages and searches them); `--no-alt-screen` and `--plain` say each as `transcript session=S: <row>`,
  so all three print the same rows. The same command again reads on, after a daemon restart too (the
  answer's newer epoch is said and kept), and checks first that the log still holds the last line shown
  (below). Attaching a run returns the pane to the run's output.
  Until the verb lanes' CLI twins exist, the parity tests compare these requests with SPINE's recorded ones.
  Each page is checked before anything of it is taken (rule (7), as SPINE's `conformance/rules.py` states it
  for a reader): a page in another projection, of another log, with a line out of order or of the wrong
  shape, a cursor that is not its last position, or an epoch that goes back is said as an error and changes
  nothing, neither rows nor cursor, and that page is not asked for again (a right page that says `more` is
  followed, up to the 50 reads). A transcript never loses a line in silence and never shows one twice in
  silence: see "What a transcript relies on". If the daemon answers that the epoch held is ahead of its own,
  the next read goes on from epoch 1 by its seq, and the check decides; a daemon that refuses even the start
  of its log is said as an error. An answer that cannot be read at all is said as an error. The plain copy
  every frame reads is made before the TUI's lock is taken; the answer itself is still read under the lock,
  as slice 1's reads always were, so an answer whose reads do not return holds the lock until they do
  (`call_api` hands in plain objects). A control API call that raises never stops the TUI.
- The binding file: `$XDG_CONFIG_HOME/recursion/tui-keys.toml` (or `~/.config/...`), or `--keys FILE`;
  `--print-keys` prints the defaults to start from. `version = 1`, an optional `leader`, a table per
  context under `[bindings]` (a key sequence written with spaces, `<leader>` for the leader, the action
  id as the value, `"none"` to remove a default), and `[safety]`, which can only make the fixed rules
  stricter: it may add keys to `never_bind` and restate `confirm_stop`, `esc_never_sends`,
  `plugins_under_leader_p` and `no_key_approves` as `true`. Plugin actions (`plugin.<plugin_id>.<command>`,
  in SPINE's names: api.v1's `plugin_id`, which may hold dots, and `name`, which holds none) are bound only
  under `<leader> p`; until a plugin host exists, pressing one says so and sends nothing.
  A key name means the key a terminal sends: `ctrl+h` is Backspace, `ctrl+i` is Tab, `ctrl+j` and `ctrl+m`
  are Enter (and `ctrl+alt+` likewise), so a leader written `ctrl+i` is judged as Tab; a name no key arrives
  as (`ctrl+enter`, `alt+esc`) is refused. Alt+Space is written `alt+space`. A binding for the same keys as a
  default replaces that default. Two spellings of one key in one
  table (`ctrl+i` and `tab`) are said, and the second is skipped; a leader that a binding in force uses where
  a leader is typed makes the file not used. Neither a binding nor the leader can be `ctrl+c`. The leader cannot be a key the default bindings of the
  run list, the pager or `global` use (`tab`, the arrows, Home, End, Page Up/Down, `ctrl+d`, `ctrl+u`, ...),
  and no binding there takes the leader alone, since either would shadow the leader's sequences. A
  file that cannot be read, or that loosens `[safety]`, is not used at all; a refused binding is skipped.
  Either way the reason is shown as an error when the TUI opens.
- The command line (`:` or `/`, `Ctrl+X n`, and the pager's `/` search) has a cursor: left, right, Home and End move it (it is drawn as `▏` when it is
  not at the end), Backspace deletes before it and Delete at it, Esc leaves the line, Enter sends it.
  Bracketed paste is on while the TUI runs: a paste goes into the command line whole, at the cursor,
  with each line break or tab as a space and every other control or invisible character dropped, so a
  paste never sends the line. A paste on the run list or at a Stop confirmation is refused (the
  confirmation is cancelled). A paste keeps at most 65,536 characters and says how many it dropped.
- Unbracketed paste (a heuristic, not a proof): a key is "burst" when it came in one read with other
  input: another key, part of another key, a paste begun beside it (an unfinished sequence that turns
  into a paste opener included), or a sequence that ended without a key (dropped by the Esc wait or the
  end of input, unknown, overlong, or broken off). A lone Esc, one key whose bytes the wait joins, and one
  paste however it is split (inside a UTF-8 character too), are each one event and not a burst. A typed
  UTF-8 character split over two reads is marked: its first bytes were held from an earlier read. A key
  sequence is burst when any of its keys is. A burst `y`, or any `y` after a Stop armed by a burst `s` or `Ctrl+X k` (either key of it),
  cancels the Stop; a burst Enter, or the first Enter on a line opened, typed or pasted into with burst
  input, does not send it (the next Enter on its own does). The TUI says so each time. Input that arrives one key per read cannot be told from
  typing, so a slow unbracketed paste still acts as keys; bracketed paste is the protection.
- The terminal is in raw input mode while the TUI runs (output processing unchanged): no input
  translation (an inherited IGNCR or INLCR is turned off, then restored) and no control character acts
  by itself, so every byte of a paste, CR, LF and Ctrl-C included, arrives as sent. Ctrl-C arrives as a
  key and ends the TUI from any mode; Ctrl-Z no longer suspends it.
- The end: a hangup ends the TUI with exit 0 through restoring the mode and the last writes. The
  hung-up terminal's EIO is expected there, whether a read saw the end of input or the terminal reports
  POLLHUP (a `q` already read, then the hangup); any other error is raised. Ending takes the app's state
  lock, so a state change already under way finishes first and none starts after it; the event log
  appends the final event and seals itself in one step, so an append another thread had already begun
  is refused; and once `Loop.shutdown()` returns no frame is painted. The final event is the last. The
  error lines go to stderr when the TUI leaves the alternate screen, also after the screen hung up, since
  stderr may be another stream that still works (a log file); an EIO on stderr itself is the same hangup
  and is taken quietly, and any other error on it is raised.
- Stop: `s` fixes the target, and `y` sends `run.stop`. `stop RUN` on the command line also works. The
  screen shows the API's own answer: `status=stopped`, or `status=pending` plus an `UNCERTAIN` line.
  Stop needs no input lease and has its own request lane, so it never waits behind a slow read. Keys are
  handled before queued output each tick.
- Stale (snapshot older than `stale_after_s` or of an age that is no number, infinite or below zero,
  a failed refresh, or an answer marked `stale`) forces `watch_only`. A `run.list` or `run.status` marked `stale` keeps it until
  a later answer of the same method is not; an answer to another method (a Stop) does not end it. Start
  is refused locally; reads and Stop stay available.
- Every request, answer, condition edge, engine output chunk, notice and error is an event in
  `TuiApp.log` before any renderer shows it. The full-screen frame and the linear renderer both read
  that log; neither renders from the other's frame or strings. `transcript` is the log's text.
- A condition is said only when it appears, changes or clears (`UNRECONCILED: ...`,
  `UNRECONCILED changed: ...`, `UNRECONCILED cleared, was: ...`), not again after every command. A
  usage error says only the error. `UNCERTAIN` for a run stays until the API attests the stop: it lists
  that run as `stopped`, or answers `stopped` to a Stop of it. A run listed with no stop record does not
  clear it, since a pending Stop whose registry write failed leaves exactly that. A `run.list`, `run.status` or `run.start`
  answer asked before a Stop was first confirmed does not show that run running again. A start's `UNCERTAIN` stays
  until a later list or status answer takes its run: a status counts when it is merged, and a list run counts
  when that read was not asked before the Stop. An answer this call does not take does not end the warning (a run
  already on screen, which a retry of the same key repeats, does not end it). The `conditions` command says the
  current set again on demand. The CLI has the same
  command (`recursion-control conditions`), which reads `run.list` because it keeps no state. No control
  API method was added.
- The latest error stays on its own row (and in the inline live region) until a newer error replaces
  it, so output never redraws it away. `NO_COLOR` frames and the linear renderer carry the same words.
- O7's `surface` allows only `gui` or `cli`, so the TUI labels itself `surface=cli` until O7 adds `tui`
  (A36). The control API has no surface field, and TUI requests are byte-identical to the CLI's.
- Approve and deny are not here. They wait for O11 and a later PR.

## What a transcript relies on

The rule (the Liaison's ruling on review 5 of #405): an operator's transcript never loses a line in silence
and never shows one twice in silence. The user sees every line once, or a marker line that says the view
was rebuilt.

What the TUI relies on from the daemon about positions, as the store and the cursor state it:

- A log's positions are gapless from 1, and a line at a position never changes: the event store appends one
  row per event at the next seq and refuses every update and delete of the log in the file itself (lane
  EVENTS, #373, merged: `core/recursion_core/events/db.py` lines 115-124 and 349-352, in this tree since the
  merge of master; cited first at #373's `31ce13eb`).
- The epoch rises by one each time the store is opened, and a cursor carries it (the same file, lines 55-56
  and 574-576). An older epoch goes on with the same persistent log; `epoch_advanced` is not a reset; a seq
  past the high-water mark is `cursor_ahead`, and a seq at it is a valid empty read; a position that
  retention skipped is a `replay.gap` line (SPINE's cursor, `schemas/control/v1/common.v1.json` line 245).
- So, in an epoch and across the epochs of one store, the high-water mark never goes down and a position
  shown never holds another line. Lane EVENTS' #403 (`45eedbcf`) lists `events.read` and `events.follow` as
  read rows (`core/recursion_core/control/methods/__init__.py` line 206, there and in this tree) and
  changes none of this; its change reader, which is not in this tree, keeps the digest of the last image it
  logged for the same reason (`core/recursion_core/events/changes.py` lines 11-14 on #403's branch at
  `45eedbcf`).

What it checks: every read after a line was shown asks from the position before the last line shown, for
one line more, so the answer begins with that line, and it must be the line shown (every field but its
position). A `transcript.line.v1` line has no id, so the line itself is what is compared. When it is
another line, the answer goes past it or ends before it, or the daemon has no position there any more
(`cursor_ahead`), the promise above was broken: a marker line, `-- view rebuilt: <why>; the log is read
again from its start, below --`, is printed as a transcript row in every renderer, and the log is read again from
its start and shown below it. What was shown stays above the marker. When the open has no read left (50
reads), the marker says that reading the log again is pending and that the command reads it; a read that
cannot be sent is said as an error, and it too is pending (the transcript's `owed`). A gap line over the last line shown is
retention: it is shown when it covers more than that line, and the lines after it are shown. A gap line over
positions already shown or said since the last line shown is not shown again, and the next page of the
same open reads on after gap lines instead of checking again, so what comes after them is reached. A page
with more lines than the read asked for is refused before anything of it is taken. A gap line says where its gap
ends, not where it starts (SPINE's transcript line leaves out the raw event's `from`), so every gap line shown
is taken to cover everything from the last line shown up to it, and a marker row follows it: `-- lines may be
missing before this point: a gap line says where its gap ends, not where it starts --`. The text before and
after a gap is never joined as if nothing lay between. One transcript may be driven
by two threads: it takes its own lock. For a lane that calls it: `Transcript.apply` returns None when the log
no longer holds the last line shown; the caller asks `broken`, rebuilds the view (`restart()`) and says so,
and never iterates None.

The cases, what the user sees, and the test that holds each (in `tests/test_review_405_r5.py`, or
`tests/test_review_405_r6.py` where named):

| Case | What the user sees | Test |
|---|---|---|
| Cursor ahead before any line was shown (a gap line past the log) | the gap line once; the next read goes from the start; each line once | `CursorAheadTest` |
| Cursor ahead after lines were shown | a marker, then the log from its start | `SameEpochTruncationTest` |
| Cursor behind: two reads in flight, a cursor put back, a read or refusal sent before a rebuild | each line once; a stale answer is not taken | `CursorBehindTest`, `InFlightAcrossARebuildTest` |
| Epoch changed: a restart over the same log | the new lines once | `EpochChangedTest` |
| Epoch changed: a log that did not persist, or an epoch that went back | a marker, then the new log | `EpochChangedTest` |
| Same-epoch truncation: nothing new, the daemon goes on, grown past, only the last line | a marker, then the log from its start | `SameEpochTruncationTest` |
| Same-epoch rewrite of the last line shown | a marker, then the log | `SameEpochRewriteTest` |
| Same-epoch rewrite of an earlier line | not seen (below) | `SameEpochRewriteTest` (pins the limit) |
| An empty stream | nothing; later lines once | `EmptyStreamTest` |
| A reconnect in the middle of a burst | each line once; a marker when the log was lost meanwhile | `BurstTest` |
| Retention over the last line shown | the gap line when it covers more, then its gap marker row; no rebuild marker | `RetentionTest` |
| Many gap lines after the last line shown, read again | each once | r6 `GapLinesTest` |
| A rebuild with no read left in the open | a marker that says the command reads the log again | r6 `BudgetTest` |
| A rebuild whose read cannot be sent | the marker, and an error that says so | r6 `RebuildReadTest` |
| Two threads on one transcript | each line once | r6 `TwoCallersTest` |
| Any gap line, one that starts late included | the gap line, then a marker that lines may be missing before it | r6 `GapStartTest` |

What it cannot see: a line rewritten in place before the last line shown, with the last line left as it
was. A reader that resumes after a position sees nothing of what lies before it; the store refuses such a
write, and noticing it would take a value per line that covers the log up to it (a field of SPINE's
`transcript.line.v1`, filled from lane EVENTS' store).

## Not wired yet

The control API has no event stream. Engine output reaches the TUI through `TuiApp.on_event` /
`Loop.post_event`, which tests feed from a fake driver. It also has no mid-run verbs (steer, queue,
interrupt, resume, fork) and no per-client event scoping. Those gaps are raised on CSL-209; this package
does not add them.

## Running it against the daemon

`python -m recursion_core.control.tui [--plain | --screen-reader] [--no-alt-screen] [--no-color] [--keys FILE] [--print-keys]` hands
`term.main` the control package's socket client (`control/client.py` `SocketAPI`, the daemon's owner-only
Unix socket), the same factory the CLI uses, so `start`, `list`, `status` and `stop` act on the running
daemon. The entry point lives in `control/` because this package's import boundary allows no transport.
With the daemon down every call answers `control_api_unavailable` and the TUI shows `UNAVAILABLE`; nothing
raises, and a Stop with no answer is never shown as stopped.

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
