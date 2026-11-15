# Recursion desktop app (beta, first slice)

The desktop form of Recursion's one GUI: a bundled frontend in a Tauri shell whose only way to the daemon is
a Rust-side proxy onto the owner-only control socket. This folder holds its first slice, which builds and
packages nothing:

- `tests/fixtures/`: one golden control request per desktop action, frame vectors for the socket's codec,
  the answer for each way a request can end, and the CLI's and TUI's own reading rules as vectors;
- `tests/`: a stand-in daemon on a real socket and the checks that hold every fixture true to the core;
- `ui/`: an inert frontend skeleton in plain HTML, CSS and JavaScript modules, with no framework and no
  package.

There is no `src-tauri/` yet: the Rust proxy is the next slice. Nothing was installed to make this one, and
no Rust, Tauri or WebKitGTK toolchain was run; the JavaScript checks ran on a Node already on the machine,
which builds nothing. Nothing here is a desktop build, and nothing claims to be one.

## What it holds to

- **The CLI's requests.** Each action sends `{"op": "control", "method", "params"}`, the frame the CLI's
  socket client writes. For the four run verbs the golden request is proven equal to the CLI's own request
  and to the frame its client writes on a real socket. The other actions take their params from SPINE's
  draft fixtures (pull request #368 at `16dea486`), and `actions.json` names every key in which the
  desktop's request differs from the fixture and why (its own `command_id`, its own `surface`). A builder
  refuses input its method's schema refuses (ids by pattern, cursors from epoch 1, limits from 1 to 1000,
  approval states by name), so nothing is sent that the daemon must refuse for its shape.
- **Only its own methods.** The window may ask for the nineteen methods its actions use and no other; each
  has a row in the draft table that the page may call and that is not of the manage scope.
- **Nothing it cannot say.** A Stop that was not answered is never shown as stopped; a run method's answer
  shows the same conditions (REFUSED, UNCERTAIN, STALE, UNRECONCILED, UNAVAILABLE) with the same words as the
  CLI and the TUI, and every run is read across `run.list`'s pages as they read it. The CLI has no command for
  the other methods, so their answers are read by the desktop's own rule with the same labels and no run-only
  words (`ui/js/model.js` `conditionsFor`): a pending session Stop or interrupt names its session, why, and
  every run whose Stop result is not settled, with that run's state and reason.
- **A Stop is confirmed first.** The window sends a run's or a session's Stop only after the operator
  confirms it, and never one that was cancelled (`tests/js/check_app.mjs` runs `ui/js/app.js` itself against
  a stand-in of `ui/index.html`).
- **SPINE's answers.** `tests/fixtures/answers.json` holds the answers of SPINE's fixtures, copied unedited by
  `sync_spine.py`, and every view is checked drawing them.
- **The core draws approvals.** The approval region is outside the themed workspace, uses only the core's
  styles, sits above everything, and draws only the `rendered` lines of the request `approval.open`
  answered. An answer is built only from that record's digest and revision, never from a summary.
- **Engine text is text.** Every value is set as text, after the TUI's sanitizer rules, so escapes, bidi
  controls and invisible characters show as markers.
- **Closing stops nothing.** The window registers nothing to run when it closes, and its page policy allows
  no connection at all: in this build every call answers `control_api_unavailable`, not delivered.

## Running the checks

From the repository root, with `TMPDIR` outside your home folder:

    PYTHONPATH=core python -X utf8 -B -m unittest discover -s desktop/tests -t desktop/tests -p 'check_*.py'
    node --test desktop/tests/js/check_actions.mjs desktop/tests/js/check_model.mjs \
        desktop/tests/js/check_sanitize.mjs desktop/tests/js/check_views.mjs desktop/tests/js/check_answers.mjs \
        desktop/tests/js/check_app.mjs

The Python checks need Linux (they open a real AF_UNIX socket) and nothing beyond the core. The JavaScript
checks need any current Node and no package. The check files are named `check_*`, not `test_*`, because the
repository's gate fails on a test-named file outside its own folders; so hosted CI runs neither set.

To check the golden requests against SPINE's schemas (needs `jsonschema`):

    python -X utf8 -B desktop/tests/sync_spine.py <checkout of #368 at the head actions.json names>
    python -X utf8 -B desktop/tests/sync_spine.py <checkout> --write    # after reading what changed

It reports `GAP` for `approval.open` and `approval.answer`: the draft's approval surfaces are `cli`, `tui`,
`page` and `phone`, with no `desktop`. Until that is settled the desktop builds those two requests and sends
neither. A gap waives only the one error it names (the `enum` at `surface` refusing `desktop`); any other
error fails beside it, and `check_sync_spine.py` holds that rule and the fixture checks to cases without a
checkout.

## What the next slice must keep

The Rust proxy (`src-tauri/`, Tauri 2.11.6 or later) is the window's only way to the daemon:

- one command, `control_call(method, params)`, and one follow channel; it passes only the methods in
  `ui/js/actions.js` `METHODS`, checks each request against its method's params, and frames and size-checks
  as `tests/fixtures/frames.json` says;
- it refuses a socket whose directory is not a real 0700 directory of the user's, checks the socket is a
  socket owned by the user, and checks the daemon's uid on the connection, as the core's client does;
- a long `events.follow` never holds the connection a Stop needs;
- no HTTP, shell or file-system plugin, no updater, a strict content policy and the smallest capability set;
  the daemon never runs inside the app's process, and closing every window sends nothing.
