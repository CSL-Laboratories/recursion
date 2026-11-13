# Control records, version 1 (drafts)

These are the records the daemon, the terminal client, the page, the Control MCP server, the engine
drivers and plugins share. Each is a JSON Schema (Draft 2020-12) with fixtures. A lane builds against
the schema and its fixtures, not against another lane's code.

**Status: draft.** Every schema here is tagged `1.0-draft.N` in its `x-version`. A draft may still
change; each change is listed in `CHANGES.md` with what it breaks. A schema becomes `1.0` when every
consumer lane named for it below has a fixture in this folder that passes. From `1.0` on, a schema
changes only by adding.

Nothing here adds behaviour to the daemon. The first drafts describe what the daemon answers today
(the run records) and what the lanes that are about to start will write (sessions, events, queue items).

## What is in this folder

| File | Record | What it is for | Who writes it | Who must send a fixture |
|---|---|---|---|---|
| `common.v1.json` | (definitions) | Ids, times, digests, principal, workspace, route, budget, lease, cursor. Not a record. | | |
| `run.record.v1.json` | storage | The run record as the registry stores it. Its twelve required fields are today's and do not change. | control API, registry | CONTROL, STOP, SESSIONS |
| `run.view.v1.json` | public | A run as `run.start`, `run.status` and `run.list` answer it: the stored record plus what the daemon observed. | daemon | CONTROL, TUI, PAGE, MCP |
| `stop.result.v1.json` | public | What a Stop answers: `stopped` or `pending`, with the evidence. | control API | STOP, CONTROL, TUI, PAGE, MCP |
| `session.v1.json` | public and storage | One session and its turns. | SESSIONS | SESSIONS, TUI, PAGE, MCP, DRIVER-CC, DRIVER-CX |
| `queue.item.v1.json` | public and storage | One queued message of a session. | SESSIONS | SESSIONS, TUI, PAGE |
| `event.v1.json` | public and storage | One event of a session's log, with one payload shape per type. | EVENTS and every lane that emits | EVENTS, DRIVER-CC, DRIVER-CX, APPROVALS, QUOTA, ROUTING, WORKSPACE, LOOP, TUI, PAGE, MCP, STOP |
| `approval.v1.json` | public | One approval request as a client reads it, and (described, never sent) the daemon's private binding to the engine's own request. | APPROVALS | APPROVALS, DRIVER-CC, DRIVER-CX, TUI, PAGE, PLUGIN-HOST |
| `driver.v2.json` | internal | What an engine driver declares (its flags), what `send` takes, what `start` gives back, and the event drafts `parse` returns. | each engine lane | DRIVER-CC, DRIVER-CX, LOOP, SESSIONS |
| `mcp.task.v1.json` | public | An MCP Task, derived from a turn or a run at the moment it is asked for. | MCP | MCP |
| `transcript.line.v1.json` | public | One line of a session's transcript: a projection of an event for a linear reader. | EVENTS (projection), TUI | TUI, EVENTS |

Still to come in this folder: `methods.v1` (the method table) and `session.token.v1`. The plugin
manifest and `install.json` are under `schemas/plugin/v1/`.

**Not yet folded in.** The build plan was revised while these drafts were being written, and a second
cross-family read of the plan made further points about the records. Draft 1 does not have them yet:
on the run record a `cgroup_identity`, a `launch_manifest` and an `owner`; on the Stop result a third
answer, `ended`, and a fence fact; on `usage` separate quality and provenance for tokens and for cost;
`bucket` on `limit`; an `outcome` on `turn.ended`; a `route.decision` payload of chosen, reason,
candidates and hand pick; `rendered` lines on an approval; what `approval.requested` carries so that a
log can be replayed without a lookup; an `effect.outcome_unknown` event type; and how a surface is
recorded as having shown an approval. Each will be a `1.0-draft.2` with its line in `CHANGES.md`.

`fixtures/good/<schema>/` holds records that must validate; `fixtures/bad/<schema>/` holds records
that must be refused. `fixtures/index.json` lists every one and says what it shows, or which rule it
breaks and which schema keyword refuses it. Files named `c*` were captured from the daemon's own
answers by `fixtures/capture_today.py`; files named `h*` and `b*` were written by hand.

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
- A session is `idle`, `live`, `stop_pending` or `ended`. `idle` with a worker that is alive is a
  persistent worker waiting for input: `idle` does not mean "no unit".
- `stopped` is answered and stored only with an attestation. A client that meets a `proof` it does not
  know treats the run as **not stopped**.
- Each session has one event log. `seq` starts at 1 and has no holes. A span that cannot be given is
  one `replay.gap` event, never a silent skip.
- A number that is not known is `null`, never `0`.
- No token, cookie, key or credential appears in any record. A `principal` is a kind and a label.
  A `connection` is `<kind>:<label>`. A `budget` names a reservation and a cap.

## Rules for every schema here

- **Closed.** Every object lists its properties and refuses any other. Consumer data never rides in an
  untyped `payload`, `labels` or `metadata`. If you need a field, a type or a code, ask lane SPINE
  (through the Liaison); do not add it yourself. Four objects are open on purpose, because they hold an
  engine's, an adapter's or a plugin's own data that no consumer reads typed fields out of: a tool
  call's `input`, the route result's `events[].data` and `limit_notice` in the run view, and a plugin
  effect's `payload`. The conformance client lists them and fails if another appears.
- **Present, with null.** In the records that are new in version 1, every field is always present and
  uses `null` for "none". The one exception is `run.record.v1`, whose additions are absent or have a
  value, because records written before version 1 do not have them.
- **Times.** New records use RFC 3339 UTC with exactly three decimal places and `Z`
  (`2026-11-13T07:40:00.000Z`). Only the stored run record keeps Unix seconds (`started_at`,
  `stop.requested_at`), because that is what the registry has always written.
- **Ids.** `run_id` is what the daemon accepts today. Ids minted by the core for new records carry a
  prefix: `ses-`, `turn-`, `qi-`, `apr-`.
- **Patterns** are written for ECMA 262, and each is paired with a `not` that refuses a line break,
  because some regular expression engines let `$` match before a final newline.
- **Readers.** A reader that meets an event type it does not know keeps the envelope, shows nothing
  from the payload, and reads on. A reader never guesses a missing field.

## What may still change (build against it, expect a note)

These are in draft 1 and a consumer lane may still move them. Each has a fixture, so a change will
show up as a failing fixture, not as a surprise.

| Item | Where | Why it may move |
|---|---|---|
| The log of a run that has no session is named `run:<run_id>` | `common.v1.json` `log_id`; `event.session_id`; `cursor.session_id` | Taken from lane EVENTS' plan. EVENTS and SESSIONS must both confirm it |
| Payloads of `turn.started`, `turn.ended`, `steer.queued`, `steer.delivered`, `interrupt`, `stop.requested`, `diff`, `checkpoint`, `fork`, `route.decision` | `event.v1.json` | Lane SPINE drafted their fields. The lane that emits each may reshape it by sending a fixture |
| `usage.span` and `usage.reasoning_tokens` | `event.v1.json` | Added by SPINE from the vault records (a running total must not be summed; one engine reports reasoning tokens). QUOTA and the driver lanes confirm. `bucket` is QUOTA's to define |
| `approval.answered.state` | `event.v1.json` | Added by SPINE so a transcript can say what happened. APPROVALS confirms |
| `containment_level` names and `claim` keys | `stop.result.v1.json` | They are lane STOP's vocabulary and wait on a decision. Marked PROVISIONAL in the schema |
| `created_at` on a session, `turn_id` on a queue item | `session.v1.json`, `queue.item.v1.json` | Added by SPINE. SESSIONS confirms |
| `epoch` is a whole number that rises | `common.v1.json` | EVENTS confirms it can keep one durably |
| `causation_id` is a loose reference | `event.v1.json` | It may be tightened to a command id or a sequence number |
| Money (`cost_usd`, `cap_usd`) is a JSON number | `event.v1.json`, `common.v1.json` | One vault record proposes whole numbers only. QUOTA confirms |
| The kinds of `principal` | `common.v1.json` | Lane CONTROL owns scopes and may add a kind |
| An engine tool's input rides in the approval's effect as `input_json`, an escaped JSON string | `approval.v1.json` | The recommended canonical form takes ASCII text and whole numbers only, and tool inputs hold any text and any number. This keeps one canonical form. The alternative is a second form for engine effects. It goes with the digest decision |
| `surface` (who showed an approval) is `cli`, `tui`, `page` or `phone` | `approval.v1.json` | APPROVALS confirms; how a surface is recorded as having shown a request is not in draft 1 |
| The transcript's `state`, `speech` and `focus` | `transcript.line.v1.json` | Lane SPINE wrote their meanings from the contract's one sentence. TUI owns them |
| The Task's `expires_at`, `poll_interval_ms` and the status derived for `unknown` and `lost_reboot` runs | `mcp.task.v1.json` | MCP confirms against the protocol versions it speaks |
| `driver.v2`: the shapes of `start`, `handle` and `fork` | `driver.v2.json` | DRIVER-CC lands the interface; these are its data |
| Id prefixes for new records | `common.v1.json` | Firm unless a consumer objects before the first slice lands |

What will not move without a new major version: the twelve stored run fields; the fields and states
of the run view (they are today's answers, captured); the event envelope's field names; the timestamp
and digest forms; and that every object is closed.

## What is not decided

- **Recommended, not decided:** `tool.call.input_digest` is taken over the canonical form that the
  approval record will name. The recommended form is `csl-cj/v0` (the one `plugin_security/canonical.py`
  already implements), named in the record so that a later form is a new name. The alternative is
  RFC 8785. This is the owner's decision and has not been made.
- **Recommended, not decided:** the names of the containment levels and the keys of the Stop sentences
  (see above).
- Which scope a Stop needs, and whether the plugin contract is frozen as version 1 now, are also the
  owner's decisions. They belong to `methods.v1` and the plugin schemas, which are not in this slice.
- The contract's research text calls the approval verb `approval.respond`; the build plan calls it
  `approval.answer`. The method table will carry one name.

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

The client in `conformance/` reads only these files. It imports nothing from the product.

## Where these drafts came from

- The run records are the code at the candidate's head: `control/registry.py`, `control/api.py`,
  `daemon/control_adapter.py`. One thing the registry's own description leaves out is in the schema:
  the stored `stop` object also carries the `cgroup` and `cgroup_marker` the cleanup request was sent to.
- The session, turn, queue, event and Stop-result fields are the build plan's (the contract's D3, D4,
  F7 and F8 lanes: a versioned control API, a wire vocabulary published as a schema with fixtures, an
  append-only event store, keyed and leased runs).
- From the research records: one typed protocol behind every surface, and a method table kept as data;
  command, receipt and outbox in one transaction, with a payload digest on the receipt; closed objects
  with `null` for absence and cross-field rules written as `if`/`then`; a closed list refuses the
  first record that carries a new value, so the lists here are short and each is owned by a lane; an
  engine's own stream carries no version and its untyped fields change, so the daemon's validation is
  the authority and engine text stays untrusted; an unknown event type is kept and skipped by a reader;
  and a model-driven client does not mint unique keys, which the method table has to answer.
- Set aside from the same records, with the reason: "no wall clock in the log" (the transcript the
  contract proposes needs a time, and the plan asks for one); whole numbers only for every field (cost
  is reported by engines as a fraction); a cursor with no epoch (the contract asks for the daemon's
  epoch on the cursor).
