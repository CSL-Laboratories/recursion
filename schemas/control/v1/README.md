# Control records, version 1 (drafts)

These are the records the daemon, the terminal client, the page, the Control MCP server, the engine
drivers and plugins share. Each is a JSON Schema (Draft 2020-12) with fixtures. A lane builds against
the schema and its fixtures, not against another lane's code.

**Status: draft. Nothing here is frozen.** Every schema is tagged `1.0-draft.N` in its `x-version`.
A draft may still change; each change is listed in `CHANGES.md` with what it breaks. A schema becomes
`1.0` only when every lane that consumes it (the list is in `CHANGES.md`) has a fixture in this folder
that passes. From `1.0` on, a schema changes only by adding.

Nothing here adds behaviour to the daemon. The drafts describe what the daemon answers today (the
run records, the six methods it has) and what the lanes that are about to start will write.

**The hosted gate does not check these files.** The checks in `conformance/` run where someone runs
them (see "Checking the fixtures").

## What is in this folder

| File | Record | What it is for | Who writes it |
|---|---|---|---|
| `common.v1.json` | (definitions) | Ids, times, digests, principal, owner, workspace, route, route decision, budget, lease, cgroup identity, cursor. Not a record | |
| `run.record.v1.json` | storage | The run record as the registry stores it. Its twelve required fields are today's and do not change | control API, registry |
| `run.view.v1.json` | public | A run as `run.start`, `run.status` and `run.list` answer it: the stored record plus what the daemon observed | daemon |
| `stop.result.v1.json` | public | What a Stop answers. Today's answer, exactly, plus additions the lane that owns Stop will make | control API |
| `session.v1.json` | public and storage | One session and its turns, each turn with its own outcome and result | SESSIONS |
| `queue.item.v1.json` | public and storage | One queued message of a session | SESSIONS |
| `event.v1.json` | public and storage | One event of a log, with one payload schema for each of twenty-four types | EVENTS and every lane that emits |
| `approval.v1.json` | public | One approval request as a client reads it; the summary `approval.list` returns; and (described, never sent) the daemon's private binding | APPROVALS |
| `driver.v2.json` | internal | What an engine driver declares (its flags), what `send` takes, what `start` gives back, and the event drafts `parse` returns | each engine lane |
| `mcp.task.v1.json` | public | An MCP Task, derived from a turn (or, for `run.start`, from the run) at the moment it is asked for | MCP |
| `transcript.line.v1.json` | public | One line of a session's transcript: a projection of an event for a linear reader | EVENTS, TUI |
| `methods.v1.json` | public | The method table's schema: a row for each of thirty-eight methods, and the params and every answer of each | CONTROL and each lane that owns a method |
| `methods.table.v1.json` | data | The table itself: for each method its scope, whether it mutates, its key and precondition, the driver flags it needs, its page and MCP marks, every code it can answer | lane SPINE, on request |
| `session.token.v1.json` | private | The grant the daemon keeps for a session token, and the form of the token. Never sent | CONTROL |

`fixtures/good/<schema>/` holds records that must validate; `fixtures/bad/<schema>/` holds records
that must be refused. `fixtures/index.json` lists every one and says what it shows, or which rule it
breaks and which schema keyword refuses it. Files named `c*` were captured from the daemon's own
answers by `fixtures/capture_today.py`; files named `h*`, `r*` and `b*` were written by hand. Files
named `u-*`, `m-*`, `v-*` and `f-*` were generated: each is a good fixture with one change (a field
nobody defined added, a required field removed, a value replaced by one outside its list, a fixed
value replaced), one for every closed object, required field, list of allowed values and fixed value,
so that none of them can be taken away unnoticed.

## Storage records and public records are different records

The registry stores `run.record.v1`. A client never reads that. It reads `run.view.v1`, which adds:

- `observed`: what the daemon last saw of the unit (or null when no unit was ever recorded);
- three states the registry never stores: `ended` (the run finished on its own; nobody stopped it),
  `lost_reboot` (it belonged to an earlier boot) and `unknown` (the unit could not be observed), with
  `recorded_state` keeping the stored word;
- `result`, the route result once there is one (whole on `run.status`, cut to `status`, `code` and
  `detail` on `run.list`);
- `output`, the tail of the engine's output file, on `run.status` only.

A store that writes `ended` is wrong, and a client that expects `recorded_state` on every run is wrong:
the bad fixtures show both.

## Meanings that are frozen with these records

- A **run** is one supervised unit with its own Stop.
- A **turn** is one input and the engine's work until it yields.
- With a driver that declares `multi_turn`, several turns of a session share one `run_id`, because one
  worker stays alive between turns. With any other driver each turn has its own run.
- **Stop acts on a run. Interrupt acts on a turn.**
- **A turn's result is stored with the turn.** A run's result file is the run's, not a turn's. An MCP
  Task of a session is derived from a turn, never from the run's last result.
- A session is `idle`, `live`, `stop_pending` or `ended`. `idle` with a worker that is alive is a
  persistent worker waiting for input: `idle` does not mean "no unit".
- **Stop.** `stopped` means the run's removal was attested. `ended` means the run had already ended
  before this Stop and this call stopped nothing; it never implies a verified removal. `pending` means
  neither is established. A client that meets a code or a proof it does not know treats the run as
  **not stopped**.
- Each log is ordered. `seq` starts at 1, has no holes and is never used twice. A span that cannot be
  given is one `replay.gap` event, never a silent skip.
- A state change carries the record **as it is after the change**, so a log can rebuild state without
  another read; and it says whether the change was seen as it was made or found by a later scan.
- A number that is not known is `null` with quality `unknown`, never `0`. Each measurement carries its
  own quality and provenance.
- **Delivery to an engine is not exactly once.** A queued message that was being delivered when the
  daemon restarted, with no event from the engine naming its delivery, is `outcome_unknown`: shown to
  the user, never sent again by itself.
- **Approvals.** The core draws every approval, from `rendered`, and a surface composes no approval
  text. Listing is not showing: a surface calls `approval.open` when it displays the prompt, and an
  answer from a surface with no open record for the current revision is refused `prompt_not_shown`.
  Only the owner or a paired device may open or answer; an engine, a plugin and a session token never
  can. Expired means denied.
- No token, cookie, key or credential appears in any record. A `principal` is a kind and a label, and
  is derived by the server, never read from a request. A `budget` is a cap, 0 by default.

## Rules for every schema here

- **Closed.** Every object lists its properties and refuses any other. Consumer data never rides in an
  untyped `payload`, `labels` or `metadata`. If you need a field, a type or a code, ask lane SPINE
  (through the Liaison); do not add it yourself. A few objects are open on purpose, because they hold an
  engine's, an adapter's or a plugin's own data that no core lane reads typed fields out of: a tool
  call's `input`, the route result's `events[].data` and `limit_notice` in the run view, a launch
  snapshot's `profile`, a plugin effect's `payload`, and the operator plugin's and a plugin command's
  own arguments and results. `conformance/tests.py` lists every one and fails if another appears.
- **Present, with null.** In the records that are new in version 1, every field is always present and
  uses `null` for "none". The one exception is `run.record.v1`, whose additions are absent or have a
  value, because records written before version 1 do not have them.
- **Times.** New records use RFC 3339 UTC with exactly three decimal places and `Z`
  (`2026-11-13T07:40:00.000Z`). Only the stored run record keeps Unix seconds (`started_at`,
  `stop.requested_at`), because that is what the registry has always written.
- **Ids.** `run_id` is what the daemon accepts today. Ids minted by the core for new records carry a
  prefix: `ses-`, `turn-`, `qi-`, `dlv-`, `apr-`.
- **Patterns** are written for ECMA 262, and each is paired with a `not` that refuses a line break,
  because some regular expression engines let `$` match before a final newline.
- **Readers.** A reader that meets an event type it does not know keeps the envelope, shows nothing
  from the payload, and reads on. A reader never guesses a missing field. A reader compares digests; it
  does not recompute them.

## The method table

`methods.table.v1.json` is data: one row per method. `methods.v1.json` gives, for every method, the
schema of its params and of everything it can answer, and states the rules (in its description):

- A name that is not in the table is answered `method_unknown`. A name that is in the table and has no
  handler in the running daemon is answered `unsupported` with detail `not_implemented`.
- Every mutating method except `run.start` and `run.stop` takes a `command_id`. One identity domain:
  (tenant, method, `command_id`), durable across restarts. `run.start` keeps `client_key`. `run.stop`
  takes neither a key nor a revision.
- Order of checks: authorize; look the receipt up (same id and same payload digest returns the first
  answer, even if its revision is stale by now; same id and another digest is `conflict`,
  `command_id_reused`); then the revision or turn (`conflict`, `stale_revision` or `stale_turn`); then
  `busy`; then a missing driver flag is `unsupported`, before any side effect.
- One vocabulary for refusals: `conflict` with a detail, `busy`, `unsupported` with a detail,
  `prompt_not_shown`, `scope_refused`, and `<thing>_unknown`.

Today's daemon answers six of the thirty-eight: `run.start`, `run.status`, `run.list`, `run.stop`,
`route.list`, `plugin.list`. Seventeen whole exchanges of those six, refusals included, are captured
under `fixtures/good/methods.v1/` and validate against the table.

## Reserved

Refused by the schemas until the lane that owns Stop defines them: a `launch_manifest` on the run
record, and a stored `stop_attestation` whose proof is `removed`. The Stop answer's `attestation`,
`containment_level`, `claim` and `fence` are defined here as PROVISIONAL shapes; no answer carries them
until that lane adds them in a step of its own.

## What may still change (build against it, expect a note)

Each of these has a fixture, so a change shows up as a failing fixture, not as a surprise.

| Item | Where | Whose to settle |
|---|---|---|
| Payloads of `turn.started`, `turn.ended`, `steer.queued`, `steer.delivered` | `event.v1.json` | The lanes that emit them (the driver lanes, LOOP, SESSIONS), by sending a fixture |
| Payloads of `diff`, `checkpoint`, `fork`, `workspace.grant`, `workspace.revoke`; the `diff` answer | `event.v1.json`, `methods.v1.json` | WORKSPACE |
| `route_decision` (chosen, reason, candidates, hand pick) and the answer of `route.explain` | `common.v1.json`, `methods.v1.json` | ROUTING, QUOTA |
| `effect.outcome_unknown` (which effects, which reasons) | `event.v1.json` | EVENTS |
| `usage.bucket`, the `budget.show` row | `event.v1.json`, `methods.v1.json` | QUOTA |
| `containment_level` names, `claim` keys, the `attestation` and `fence` shapes | `stop.result.v1.json` | The lane that owns Stop, on a decision that is still open |
| The launch snapshot (`route` on the run record): which of the route contract's fields it keeps | `run.record.v1.json` | DRIVER-CC, ROUTING |
| An engine tool's input rides in the approval's effect as `input_json`, an escaped JSON string | `approval.v1.json` | Goes with the digest decision (below) |
| `surface` (who showed an approval) is `cli`, `tui`, `page` or `phone` | `approval.v1.json` | APPROVALS |
| The transcript's `state`, `speech` and `focus` | `transcript.line.v1.json` | TUI |
| The Task's `poll_interval_ms`, and the status derived for `unknown` and `lost_reboot` runs | `mcp.task.v1.json` | MCP |
| `driver.v2`: the shapes of `start`, `handle` and `fork` | `driver.v2.json` | DRIVER-CC |
| Scopes, page marks and MCP marks of the thirty-two methods that are new | `methods.table.v1.json` | CONTROL, PAGE, MCP |
| The delegable methods of a session token | `session.token.v1.json` | CONTROL, DELEGATION |
| The doctor report's checks | `methods.v1.json` | INSTALL |
| The kinds of `principal` | `common.v1.json` | CONTROL |
| `causation_id` is a loose reference; money is a JSON number | `event.v1.json`, `common.v1.json` | EVENTS, QUOTA |

What will not move without a new major version: the twelve stored run fields; the fields and states
of the run view and today's Stop answer (captured); the event envelope's field names; the timestamp
and digest forms; `run:<run_id>` as the log of a run without a session; the id prefixes; and that
every object is closed.

## What is not decided

Each is built as recommended, marked PROVISIONAL in its schema, and is the owner's decision.

- **The digest's canonical form.** Recommended: `csl-cj/v0` (the restricted canonical JSON that
  `plugin_security/canonical.py` implements), named in the record as `digest_form` so that a later form
  is a new name. That form takes ASCII text and whole numbers only, and an engine tool's input holds any
  text and any number, so the effect carries the input as `input_json`, a string the core serialised
  with every non-ASCII character escaped; the digest binds that string and the prompt is drawn from it.
  The alternative is RFC 8785, for engine effects or for everything.
- **Who may Stop.** Recommended: `run.stop` needs a currently held `run` scope; an owner or a device
  holding it may stop any run, including runs from before version 1; a session principal may stop only
  its own session's runs and its descendants'; the daemon's own recovery Stop is not a client's call.
  The alternative: any authenticated caller may stop any run.
- **Approvals on the MCP face.** Built: `approval.list` is offered as a read tool; `approval.open` and
  `approval.answer` are never offered.
- The names of the containment levels and the keys of the Stop sentences.
- The contract's research text calls the approval verb `approval.respond`; the table has
  `approval.answer`.
- The contract's research text asks for a command id "with a per-boot part". The table has one durable
  identity domain with no part that changes at a restart, so that a retry after a restart is still
  recognised; a client makes its ids unique itself.

## How to use this from a lane

1. Read the schema and the fixtures for the record you produce or consume.
2. Write your code so that what it produces validates, and what it consumes is one of the good fixtures.
3. Send lane SPINE one fixture of what your lane really produces or really needs. That fixture is what
   moves the schema from draft to `1.0`.
4. If a fixture of yours is refused, or a field is missing, say which, with the record. Additive
   requests are normal. A request that changes or removes something published gets an answer that says
   what it would break.

`jsonschema` is not installed in the daemon's own interpreter (free-threaded CPython 3.14), so nothing
under `core/` may import it. Validate in tests that run on Python 3.12, or check records with plain code.

## Checking the fixtures

```
python3.12 -m unittest discover -s conformance
python3.12 conformance/wire_client.py
```

The client in `conformance/` reads only these files. It imports nothing from the product. The hosted
gate does not run it: the gate's file patterns do not reach `conformance/`, and the gate is frozen.

## Where these drafts came from

- The run records, the Stop answer and the six methods are the code at the candidate's head:
  `control/registry.py`, `control/api.py`, `daemon/control_adapter.py`, `daemon/plugins.py`. Two things
  the code's own descriptions leave out are in the schemas: the stored `stop` object also carries the
  `cgroup` and `cgroup_marker` the cleanup request was sent to; and in a Stop answer `repeat: true`
  means a Stop had already been asked, not that the run had already stopped.
- The rest is the build plan's, for the contract's D3, D4, F7 and F8 lanes (a versioned control API, a
  wire vocabulary published as a schema with fixtures, an append-only event store, keyed and leased
  runs), as corrected by two cross-family reads of the plan.
- From the research records: one typed protocol behind every surface, and a method table kept as data;
  command, receipt and outbox in one transaction, with a payload digest on the receipt; closed objects
  with `null` for absence and cross-field rules written as `if`/`then`; a closed list refuses the
  first record that carries a new value, so the lists here are short and each is owned by a lane; an
  engine's own stream carries no version and its untyped fields change, so the daemon's validation is
  the authority and engine text stays untrusted; an unknown event type is kept and skipped by a reader;
  and a model-driven client does not mint unique keys, so on such a surface the server mints them.
- Set aside from the same records, with the reason: "no wall clock in the log" (the transcript the
  contract proposes needs a time, and the plan asks for one); whole numbers only for every field (cost
  is reported by engines as a fraction); a cursor with no epoch (the contract asks for the daemon's
  epoch on the cursor).
