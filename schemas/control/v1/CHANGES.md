# Changes to the control records, version 1

Each schema carries its own version in `x-version`. While a schema is `1.0-draft.N`, any change bumps
`N` and is listed here with what it breaks. A schema becomes `1.0` only when every lane that consumes
it (the table at the end) has a passing fixture in `fixtures/`; from then on it changes only by adding,
as `1.1`, `1.2` and so on, and each addition is listed here. When a schema becomes `1.0`, this file
records the SHA-256 of the schema together with everything it refers to, so that a later edit cannot
pass unnoticed.

Nothing is frozen yet.

## Where each schema stands

| Schema | Version |
|---|---|
| `common.v1.json` | 1.0-draft.3 |
| `run.record.v1.json` | 1.0-draft.4 |
| `run.view.v1.json` | 1.0-draft.4 |
| `stop.result.v1.json` | 1.0-draft.2 |
| `session.v1.json` | 1.0-draft.3 |
| `queue.item.v1.json` | 1.0-draft.5 |
| `event.v1.json` | 1.0-draft.6 |
| `approval.v1.json` | 1.0-draft.5 |
| `driver.v2.json` | 1.0-draft.4 |
| `mcp.task.v1.json` | 1.0-draft.4 |
| `transcript.line.v1.json` | 1.0-draft.3 |
| `methods.v1.json` and `methods.table.v1.json` | 1.0-draft.7 |
| `session.token.v1.json` | 1.0-draft.1 |

## After another model family read the whole set a second time (Nov 13, 2026)

That read (of commit `d79c7a2c`) judged the drafts not yet ready to build against and wrote eight
corrections, each with its replacement wording. The wording is in the schemas as it was written; the
rules, the fixtures and the vectors follow it. Nothing here reopens the owner's two decisions.

| Correction | What changed | What it breaks |
|---|---|---|
| R1. Numbers | Admission no longer depends on how a number is spelled. An integral value outside -(2^53 - 1) .. 2^53 - 1 is refused with a fraction or an exponent as well as in digits (`9007199254740993.0`, `1e20`, `1e21`, `1.5e300`), and so is a number whose nearest binary64 is such a value. What is bound is normalised (`1.0` is `1`, minus zero is `0`) and that value is what is dispatched. An admitted call read again from its own canonical text is admitted with the same bytes: every taken vector is checked for it. `approval.v1` draft 5 | A producer that took those numbers; the earlier vector that showed `1e21` and `1.5e300` as taken is changed |
| R2. An approval's deadline, content and consumption | Before a fresh answer the server's time is compared with `expires_at`: at or after it the request is `expired` with no answer and the answer is `conflict`, `already_resolved`. Content changes only while pending and each change raises the revision. An approved effect is consumed once, against exactly its approval, revision, digest and private binding. A replayed answer is history. The rule (`ApprovalModel`) has all of it; eight vectors are new | An implementation that lets a scheduler decide expiry, or carries an effect out from a cached answer |
| R3. The lines of an approval | The session a call is bound to is on a line. Every line of an engine's or a plugin's text, the directory included, goes through one encoder, with the label counted toward the line. A record whose own session or run is not its effect's is refused (`envelope`). `rendered_form` keeps its name: nothing was published under it | Every rendered engine approval gains a line; a directory longer than 3,987 characters is two lines, not one line the schema refuses |
| R4. Delivery of a queued message | `queue.item.v1` draft 5: a private, normative `delivery_binding` (delivery, item, log, run, worker incarnation, target turn, how) is committed with `delivering` before the write. At most one write for one binding. An acceptance delivers only when its delivery, log, run, turn and how are the binding's; a mismatch changes nothing. `outcome_unknown` is worded as it is: the message may not have been sent. A late acceptance is kept as evidence. The rule is `DeliveryModel` (it replaces `queue_step`) | A producer that matches an acceptance by its delivery id alone, or allocates a new turn's id after the dispatch |
| R5. The Task's edges | `run.view.v1` draft 4: a result's `code` is a code word, so a view can no longer carry one a Task would refuse. A view that says `stop_pending` sets `cancel_requested` whether or not it carries a stop record | A view whose result code ends with a line break |
| R6. The identity of a start | `run.record.v1` draft 4: the intent is the engine as supplied (or null), the prompt's digest and only the DECLARED extensions that are present and not null, `session_id` and `turn_id` among them; it is taken before any registrant runs. The earlier fixed list of six members is gone. `start_key.engine` may be null, and only with an intent. `principal` and `launch` come together or not at all, on the record and on the view. A retry reuses a selected route entry, and a null one keeps the configured lookup. Fourteen vectors give preimages byte for byte | A producer of the fixed-list intent; a record with a principal and no launch |
| R7. Pages and cursors | A page continues its cursor: only positions after it, in order, none skipped except inside a gap; the answer's cursor is the last position represented. A cursor from a future epoch is `request_invalid`, `cursor_epoch_ahead`; one past the end is `cursor_ahead`; an unknown log is `session_unknown`. Three good exchanges that broke this are repaired, and every good page is now checked against its cursor. `common.v1` draft 3 | A reader that expects a projection to drop positions |
| R8. The method table | `methods` draft 7. Every row can answer `unavailable`: `run.start`, `run.status`, `run.list`, `run.stop`, `route.list` and `methods.list` gain it, for a daemon that is closing (`daemon_closing`), and `run.start` also for a start registrant that refused before a run existed (`start_registrant_failed`). The six legacy methods and `methods.list` are mandatory built-ins and are exactly the rows that cannot answer `unsupported`. An interrupt that fell back to Stop must carry the Stop's answer. Steering without `mid_turn_input` is queued for the next turn and does not interrupt. A receipt's tenant is derived from the authorized target. Each row's `falls_back` and `service` are pinned by a check | A reader with a fixed list of codes for the six legacy rows |

The read also took ten rules away at random and two stayed green. Each now has a fixture that
refuses: a session token grant that names a method twice, and a Task that waits for an approval and
carries a result.

Two of its follow-ups are done here because they are small. Every `post_image_sha256` and
`input_digest` in a positive example is the computed digest, over the UTF-8 bytes of the value as RFC
8785 writes it (`event.v1` draft 6), and a check holds every one to it. Every field typed as a
timestamp is a real UTC calendar instant: the keyword `x-instant` says so, the conformance client
checks it, and a validator that does not know the keyword must make the check itself. A Task's
`created_at` is null when the log it is derived from is a retained tail or has a gap before the first
event of the run or the turn (`mcp.task.v1` draft 4).

### The seven choices of the commit before, each kept with a qualification

| Choice | Kept as |
|---|---|
| A first read uses seq 0 and epoch 1 | Epoch 1 is the minimum/bootstrap token, not a claim that the client previously observed the log |
| The name `epoch_advanced` | It is true exactly when a successful continuation replaces an older requested epoch with the current one. It is not a reset-to-zero instruction |
| A late acceptance changes nothing | PROVISIONAL, the conservative rule. Never resend or silently promote a quarantined item merely because an unbound late packet arrived. Preserve late evidence; a later explicit reconciliation protocol may promote only after proving the durable binding and absence of a conflicting dispatch |
| The code `result_invalid` | It truthfully says the retained result cannot be projected; it must not invent an engine failure reason |
| `unknown` is working, `lost_reboot` is failed | working for unknown means no terminal outcome is known, not observed progress. failed/lost_reboot reports loss of the run's continuity, not proof that an external effect failed or did not happen |
| `input.accepted` and `steer.sent` are filed under a turn | A concrete write/acceptance must name its target, and it must be the target of the durable binding |
| `turn.started` and `steer.queued` keep more fields | These are copies of the committed turn/queue state at that event, not separately editable state |

### Open, in the reading family's words

Not done in these drafts. A lane must not read a schema-valid object as proof of any of them.

- **Shape validation is not cross-record integrity.** Explicitly require session.active_turn_id
  membership, coherent worker/run/turn links, unique/ordered turn indices, and equality between event
  envelope IDs and their typed payload/post-image identities. The shapes in session.v1/event.v1/
  approval.v1 contain both copies but cannot alone establish the joins. Add semantic vectors or
  document the validated producer preconditions for each projection.
- **Keep declared deferrals honest.** Stored Stop proof=removed and launch_manifest remain reserved;
  driver live-handle ownership remains a runtime concern; session-token transport/admission is not
  proven by grant shape; plugin API version/revocation policy remains provisional. Signature
  verification, whole-package file-map/digest coverage and cross-reader byte ordering still need
  executable artifact vectors before a plugin lane claims end-to-end interoperability. State the
  conversion from session.workspace.path to launch.workspace.source_path explicitly; they currently
  have different field names. None of these draft files is an implementation of the deferred runtime.
- **The origin of a transcript's time.** Task/transcript origin comes from retained creation metadata
  or the actual creation event. A retained tail or replay.gap is not a creation time. The Task rule
  follows this; the transcript rule still counts from the first event it is given, so a transcript
  made from a retained tail has no stated origin yet.
- The owner's two questions stay his: whether steering is offered on an engine that gives no typed
  acceptance mid-turn, and the final limit of an effect's size and what a prompt does with one that is
  too long to show.

## The rest of the build plan's third revision (Nov 13, 2026)

The section after this one brought three schemas in line with the build plan's third revision and
listed what was left. This section is what was left: the launch object, the delivery of a queued
message, the Task, and the cursor. Two rules that a schema cannot state came with it
(`conformance/rules.py`: `queue_step`, and `task_of_run` with `task_of_turn`), each with vectors.
What still differs from the plan is at the end of this section.

### `run.record.v1.json` and `run.view.v1.json` are `1.0-draft.3`

| Change from draft 2 | What it breaks |
|---|---|
| The record's `session_id`, `turn_id`, `workspace`, `route`, `route_decision`, `owner` and `budget` are gone from the top level. They are ONE object, `launch`, stored as the launch was asked: `route_entry` (the route as it was loaded: `route`, `deadline_s`, `binary`), `route_decision`, `workspace` (`mode`, `source_path`, `base_ref`, `worktree_path`), `containment_level`, `session_id`, `turn_id`, `owner`, `budget`. Every field of it is always present; one that does not apply is null. The record's `principal` is NOT inside it: it stays beside it, set by the host, so that a reader takes who asked from the trusted field and never from what was asked | Every producer and reader of the seven fields |
| `start_error {code, detail}` is new and RESERVED. No producer writes it yet: the lane that owns Stop writes it, with its fix for a start error that can belong to a later attempt. A reader uses it when it is there and falls back when it is not | Nothing |

A record written before this change has no `launch`; both fields are optional for that reason.

### `queue.item.v1.json` is `1.0-draft.4` and `event.v1.json` is `1.0-draft.5`

How the core learns that an engine took a message is settled, by the plan: the engine says so, in a
typed event, and nothing else counts.

| Change | What it breaks |
|---|---|
| `input.accepted {delivery_id, turn_id, how}` is new: the ENGINE's typed acceptance of an input, from a driver's parse and only from the engine's own typed signal, never read out of free text. `how` is `new_turn` or `mid_turn`. Its source is always `engine` | Nothing published; a producer that marked an item delivered without it |
| `steer.sent {item_id, delivery_id}` replaces `steer.delivered {item_id, delivery_id, how}`. It records the WRITE into a turn in progress and is not acceptance | Every producer and reader of `steer.delivered` |
| `turn.started` is written by the core, when `input.accepted` with `how: new_turn` arrives; draft 4 let a client's request be its source | A producer that writes `turn.started` when a turn is asked for |
| A queue item is `delivered` only when an `input.accepted` names its delivery id; `turn_id` is then the turn that took it. An item still `delivering` at a restart, or at the end of the turn it was written into, is `outcome_unknown` | A producer that treats the write, or the engine's printed text, as delivery |
| `effect.outcome_unknown.why` gains `turn_ended` | A reader with a fixed list of reasons |
| `state.changed.coalesced` is the number of marks an image stands for, given only when it was measured (1 or more), and null when it was not. Draft 4 used 0 for "exactly one". Today's producer keeps a set and no count, and always gives null | Every producer and reader of the number |

The commits, in order, are in the description of `queue.item.v1.json`. `conformance/rules.py`
(`queue_step`, since replaced by `DeliveryModel`) states what each happening does to an item, and nine vectors walk it: a crash before
the write and a crash after it end the same way (`outcome_unknown`, never sent again by itself), and a
step that is an acceptance must carry its `input.accepted` event. PROVISIONAL, for the lane that owns
the queue: an acceptance that arrives after an item became `outcome_unknown` changes nothing by itself.

`turn.started` still carries `index` and `text` beside `delivery_id`, and `steer.queued` still carries
`order`, `delivery`, `revision` and `state` beside `item_id`. The plan names fewer fields for both. The
transcript's line for a turn's input takes its text from `turn.started`, so the fields stay until the
lane that emits the event sends a fixture that says otherwise.

### `driver.v2.json` is `1.0-draft.3`

| Change from draft 2 | What it breaks |
|---|---|
| A declaration carries `input_ack`, true or false: whether the engine gives a typed acceptance of an input sent into a turn in progress. It records a fact about the engine and nothing else. Whether steering is offered on an engine that gives none is NOT decided | Every declaration: the field is required |
| A driver's parse may draft `input.accepted`. It still may not draft `turn.started` or an approval request | Nothing |

### `mcp.task.v1.json` is `1.0-draft.3`

| Change from draft 2 | What it breaks |
|---|---|
| The record names its `source`, `turn` or `run`, and carries `session_id`, `run_id` and `turn_id` at the top level. Draft 2's `of` object is gone | Every producer and reader of `of` |
| A Task of a run started with no session is derived from the run's view, field by field. Starting, running, a Stop pending and a unit that cannot be observed are `working` (`input_required` while an approval is pending); a Stop asked and not confirmed sets `cancel_requested` and leaves the status; `stopped` is `cancelled`; ended with a result is `completed` or `failed` by the result's code (a result that says ok and carries a code is `failed` with it; one that says error and carries none is `failed` with `result_invalid`), and `result_ref` is the run id; ended with no result kept is `failed` with code `result_unavailable`; a start that never launched is `failed` with code `start_failed`; lost at a reboot is `failed` with code `lost_reboot` | A server that keeps a Task in a store of its own |
| The detail of a failed start is the code of the record's `start_error` when the record has one, and null when it does not. Nothing durable says why a start failed today, so a Task never gives a reason | A Task that repeats the reason the start call was answered with |
| `created_at` and `updated_at` are the times the core LOGGED: the first event of the run or the turn, and its latest `state.changed`. Each is null when the log holds no such event | A server that stamps its own clock |
| A result's `detail` may be null | A reader that expects a string |
| Cancelling a turn's Task is an interrupt of that turn, not a Stop: it is `cancelled` when the turn's outcome is `interrupted`. Cancelling a run's Task is `run.stop` | Nothing published |

`conformance/rules.py` (`task_of_run`, `task_of_turn`) is the derivation as code. Every good Task
fixture is made by it from a source record and a log, with its status, flag, times and result also
written by hand and compared. Thirty-two vectors pair a source with a Task: twenty-four that agree,
for every state of a run's view and every state of a turn, and eight that are caught, each for its own
field. A Task of a run does not expire: PROVISIONAL until a retention rule exists.

### `methods.v1.json` is `1.0-draft.6`

| Change from draft 5 | What it breaks |
|---|---|
| `events.read` and `events.follow` TAKE the cursor they return (`cursor {epoch, session_id, seq}`), not a log id and a number. A first read sends seq 0 and the lowest epoch, 1 | Every caller of both |
| Every page answers `epoch_advanced`: true when the cursor named an older epoch than the daemon's and the read went on by seq all the same | Every producer of a page |
| A method that takes a `command_id` may answer `unsupported` with detail `store_absent`: it needs a receipt, and the daemon was composed with no event store to keep one in. No other method may | A reader with a fixed list of `unsupported` details |
| Said in the rules: a method whose row names a service answers `unavailable`, `service_starting`, until that service is ready. A plugin holding `run` may stop only a run whose `launch.owner` is that plugin (PROVISIONAL, with the rest of who may Stop) | Nothing |

### Still different from the plan

| In the plan | Here | Why |
|---|---|---|
| `turn.started {delivery_id}`, `steer.queued {item_id}` | More fields, as above | The emitting lanes reshape them with a fixture |
| A turn with outcome `done` or `candidate_done` cannot carry an error code | Neither can one with `waiting` or `paused` | Stricter, kept because it agrees with the projection of an ended turn to a completed Task. SESSIONS and MCP must change the source turn constraint, Task projection and producer/consumer vectors together before allowing error codes on waiting/paused |
| A session's `workspace` and the launch object's `workspace` | The session's says `path`, the launch object's `source_path` | The session record was not in this revision's list; one name for both is a breaking change to `session.v1` and waits for the lane that owns it |

## Brought in line with the build plan's third revision (Nov 13, 2026)

The build plan was revised again while the drafts below were being written, and it now carries the
owner's two decisions. Where it differs from these drafts it wins. Three things changed for it here;
the rest of its list was under "Not aligned yet" at the end of this section, and is now the section
above.

### `approval.v1.json` is `1.0-draft.4`

| Change from draft 3 | What it breaks |
|---|---|
| An engine-tool effect is flat: `kind`, `engine`, `session_id`, `run_id`, `tool`, `cwd`, `input` (an object). Draft 3's `call` and `call_form` are gone; the digest form in the record (`rfc8785/v1`) names and versions the form, strict profile included | Every producer and reader of draft 3's engine effect, and every digest of one |
| `revision` counts changes to the request's CONTENT (its effect, its rendered lines, its deadline). An opening does not change it, and neither does an answer: an answer changes the state. Draft 3 raised it at an answer | A producer that raises the revision when a request is answered, carried out, vetoed or expires. `approval.answered` carries the same revision as the request |
| The size rule is worded as a refusal with a typed code (`too_large`, `too_many_lines`), never a truncation. Still PROVISIONAL | Nothing |

### `event.v1.json` is `1.0-draft.4`

An interrupt's `outcome` is `delivered`, `fell_back_to_stop` or `no_live_turn`. Draft 3 had
`unsupported` where `fell_back_to_stop` now stands: a driver that cannot interrupt does not leave the
turn running, the run is asked to stop and the session resumes. What it breaks: a producer or reader of
`unsupported` there.

### `methods.v1.json` is `1.0-draft.5`

| Change from draft 4 | What it breaks |
|---|---|
| A row gains `falls_back`: the driver flags whose absence changes how the method works instead of refusing it, each with what it does then (`session.interrupt` without `interrupt`: `stop_then_resume`; `session.steer` without `mid_turn_input`: `next_turn`). The answer says which path was taken | A reader of rows that lists a fixed set of columns |
| `session.interrupt` requires no flag again. Draft 4 made it require `interrupt`; the plan says it falls back to Stop | A caller that expected `unsupported` from it on a driver that cannot interrupt |
| A row gains `service`: the service the method needs, or null. Until a named service is ready, that method alone answers `unavailable` with detail `service_starting`. No row names one yet | The same readers of rows |

**Reserved method names.** These names have no row yet and are answered `method_unknown`; they are
listed here so that nobody uses one for something else: `device.pair_begin`, `device.list`,
`device.revoke` (the lane that builds pairing), and `config.scan`, `config.offers`, `config.apply`,
`prefs.export`, `prefs.import` (the lane that builds import). `methods.list` does not list them.

### Not aligned yet with that revision (at the time; aligned since, in the section above)

These were in the plan's list and not in the drafts when this section was written. Each has landed
since, as its own entry in the section above. `stop.requested` already named a scope and the driver's
`user_message` already named its turn in draft 2.

| In the plan | In the drafts then |
|---|---|
| The run record's `route`, `route_decision`, `workspace`, `owner`, `budget`, `session_id` and `turn_id` are ONE `launch` object, with `containment_level` and the route entry as it was loaded; `start_error` is reserved on the record | Separate optional fields; no `start_error` |
| `steer.sent` replaces `steer.delivered`; `input.accepted` is the engine's typed acceptance of an input; a queue item is `delivered` only on `input.accepted` | `steer.delivered`; no `input.accepted`; the driver's parse could not draft one |
| `events.read` and `events.follow` take the cursor they return | They took a log id and a number |
| The Task names its source (`turn` or `run`) and the run source is given field by field | `mcp.task.v1` draft 2 |
| `state.changed.coalesced` may be null | A number, 0 for "exactly one" |
| A plugin holding `run` may stop only a run whose `launch.owner` is that plugin (provisional) | Not stated |

## After the second cross-family read of these drafts (Nov 13, 2026)

Another model family read these drafts at their second commit and judged them not yet a contract that
lanes can build against. This section is what changed for it, and two decisions the owner made the
same night.

### Decided by the owner, and marked for follow-up research (Nov 13, 2026)

Both are decided. The owner asked that both be marked as decisions that may be looked at again.

| Decision | What it is |
|---|---|
| The digest form of an engine's call | RFC 8785 for engine effects only (`rfc8785/v1`). Plugin effects and every signature that exists keep `csl-cj/v0`; nothing signed is migrated. The form's name carries its version, so a later change is a new value of `digest_form` |
| Hosted CI runs the conformance checks | One new file, `tools/test_wire_conformance.py`, loads the conformance suite where the gate's own runner collects it. The gate is not edited |

### `approval.v1.json` is `1.0-draft.3`

The read found that the good fixtures showed one action and bound another: a Write to a menu file
whose lines said a build folder would be removed; every digest the SHA-256 of no bytes; an opening
recorded at a revision the request had passed. The fixtures were wrong because nothing made them
right. Now one rule makes them (`conformance/rules.py`), vectors check it, and every approval inside
any good fixture is checked against it.

| Change | What it breaks |
|---|---|
| An engine's call is `call` (the value `{cwd, input, tool}`) with `call_form` `csl-call/v1`; `tool`, `input_json` and `cwd` are gone from the effect | Every producer and reader of an engine approval's effect |
| `digest_form` is `rfc8785/v1` for an engine's call and `csl-cj/v0` for a plugin's effect, fixed by the source | Every digest of an engine approval. A directory or a tool name outside ASCII is no longer refused |
| `rendered_form` is new and required; the lines follow `csl-approval-lines/v0`: every field of the effect, nothing else, nothing cut, engine text drawn as JSON text with unsafe characters escaped | Any lines a producer composed its own way; a plugin effect drawn as a summary |
| A call larger than 262,144 bytes, or needing more than 200 lines, is denied without asking | An approval of a larger call (PROVISIONAL) |
| `revision` is the request's own: an answer raises it, a showing does not; an opening carries the revision shown; `approval.answer` checks, in order, the caller, the receipt, already resolved, the revision, the showing, the digest | A producer that raised the revision at `approval.open`. A pending request is at revision 1 |
| `$defs/call` is new | Nothing |

### `session.v1.json` and `event.v1.json` are `1.0-draft.3`

A turn that ended without failing carries a result with no error code (`session.v1` turn, `event.v1`
`turn.ended`). The Task of a turn already refused that; the producer's records allowed it. What it
breaks: a record of a `done`, `candidate_done`, `waiting` or `paused` turn that carries a code.

### `queue.item.v1.json` is `1.0-draft.3`

`delivering` is the daemon's COMMITTED intent to deliver, and that commit comes before anything is
written toward the engine, so a crash between the two can never lead to a second send. What it breaks:
an implementation that writes first and records after. How the core learns that an engine took an
input sent into a turn in progress was not settled then: this record's `delivered` kept its draft-2
wording. It is settled since (`input.accepted`; draft 4, at the top of this file).

### `methods.v1.json` is `1.0-draft.4`

| Change | What it breaks |
|---|---|
| A refusal's `run_id` is any non-empty string: today's daemon looks up an id that is not well formed and answers `run_unknown` with it, as given. Two such answers are captured (`c18`, `c19`) | Nothing. A reader must not assume an echoed run id is well formed |
| A method's `unsupported` names `not_implemented` or a driver flag its row requires, and nothing else; the table check holds the row's `requires` to exactly those flags | An `unsupported` that names a flag the method does not require |
| `requires` means: without the flag the method is refused, whatever else is true. `session.interrupt` now requires `interrupt`. A method that only behaves differently without a flag does not list it | A caller that expected `session.interrupt` to answer `ok` on a driver that cannot interrupt |
| The payload digest of a command has a named form: SHA-256 of the UTF-8 bytes of the params without the command id, as RFC 8785 writes them | Nothing published |
| Said in the rules, not new: a name with no row is `method_unknown`, a reserved name included, and `methods.list` never lists it; a row without a handler answers `unsupported`, `not_implemented` (31 exchanges now show it, one for each method not built yet). `run.start`'s key reused with another engine or prompt stays `request_invalid` with detail `client_key_reused`, as today (`control/api.py`), and is not turned into a new code | Nothing |

### `transcript.line.v1.json` is `1.0-draft.3`

The read found that the transcript fixtures were not projections of the events the index paired them
with: four of five relative times were wrong, a line showed a sentence where its event carried a whole
line of the engine's stream, and one line gave a usage event a source its event did not have. Each side
validated on its own, so nothing noticed. Now `conformance/rules.py` (`transcript_fields`) states what a
line must carry because its event says so (its seq, time, type, source, trust, state, and its text
where the text is the event's own), the good transcript fixtures are made from their event fixtures by
that rule, and nine vectors pair logs with their lines: two that agree and seven that do not, each
caught for its own field.

| Change | What it breaks |
|---|---|
| `time_rel_ms` never runs backwards: when the clock steps back, a line keeps the time of the line before it | A producer that subtracts and takes what it gets (it could go below the line before, or below 0) |
| A timestamp that names a day that does not exist (the 31st of February) is refused by the rule; the pattern cannot refuse it | A log that holds one is not projected |
| The text of an `engine.output` line is the payload's text, not a sentence read out of it | The first draft's own fixture |

The sanitizer that engine text goes through is not part of these records; the rule gives an engine's
text only when it is plain, which a sanitizer leaves alone. The sentences for the other event types,
what is announced and where input goes are the renderer's and are not checked.

### Not changed then, on purpose (changed since: draft 3, at the top of this file)

`mcp.task.v1.json` stayed at draft 2 in that commit. The Task of a run that has no session needed
facts the run record does not keep (why a start failed, after a restart; when the state last changed)
and a rule from a run's result to the Task's. Those cross lanes, so the options went to the build's
coordinator first. The plan's third revision answers them, and draft 3 is built to it.

### The checks themselves

`conformance/rules.py` and `vectors/` are new (51 vectors: 24 of the call profile, 16 of what an
approval binds, 2 sequences of calls, 9 of the transcript). `wire_client.py` run as a command checks the fixtures and the
vectors and says in its last line that the consistency checks are only run by the unittest command.
No fixture may be checked against a schema that refuses nothing.

## Fixtures that prove every required field, list of values and fixed value (Nov 13, 2026)

The closed-object check of the next section is now one of four. Each `additionalProperties: false`,
each name in a `required` list, each `enum` and each `const` is taken away, one at a time, and a bad
fixture must notice: a fixture that names that keyword at that place, and that is accepted, or refused
for some other reason, once the rule is gone. What stands inside an `if` or a `not` is a condition or a
prohibition, not a rule of these kinds, and is not counted. In the two folders together:

| Kind of rule | Rules | Proved by a fixture before | Proved now |
|---|---|---|---|
| Closed object (`additionalProperties: false`) | 210 | 210 | 210 |
| Required field (one name in a `required` list) | 719 | 29 of 720 | 719 |
| List of allowed values (`enum`) | 160 | 33 | 160 |
| Fixed value (`const`) | 116 | 24 | 116 |

For it there are now 1,085 generated bad fixtures (182 before). Each is a good fixture with one change,
and its name says which: `u-` a field nobody defined added, `m-` a required field removed, `v-` a
value replaced by one outside the list, `f-` a fixed value replaced. 11 good fixtures and 4 bad ones
were written by hand so that every rule is reached: an approval answered with an approval, an
interrupted turn, a launch snapshot with a key helper, a queue update of the delivery alone, refusals
of `run.start`, `session.interrupt` and `session.stop`, the rows of `run.start` and `run.status`, a
start error whose detail carries colons, and a plugin's run in a worktree.

### `methods.v1.json` is `1.0-draft.3`: the sweep found a hole, and it is closed

`start_failed` and `start_unconfirmed` are `run.start`'s own answers, typed by `run_start_answer`: a
failed start names the run id and says why, and an unconfirmed start names the run. Both codes were
also in `run.start`'s list of bare refusals, so an answer of `{"code": "start_unconfirmed"}` that names
no run was accepted, and so was a bare `{"code": "start_failed"}`.

| Change | What it breaks |
|---|---|
| `start_failed` and `start_unconfirmed` are no longer bare refusals of `run.start`; they are only its typed answers | An answer to `run.start` that carries one of the two codes without the run id (or the run). The daemon gives no such answer (`control/api.py`, lines 268 to 310: every one names the run), and its captured answers validate unchanged |
| `start_unconfirmed` is no longer in the refusal codes at all: no other method gives it | A refusal of any other method with that code |
| The table check refuses a code that is both typed by a method's own answer and listed as its bare refusal | Nothing published; it keeps the hole from coming back |
| `refused_exchange.answer` no longer says `required: ["code"]` | Nothing: every method's own answer requires a code, and the rule was stated twice |

The row of `run.start` lists the same codes as before. A start error's `code` stays a plain word and
its `detail` stays free text (it may carry colons); a fixture now holds each of the two.

## Fixtures that prove every closed object closed (Nov 13, 2026)

A cross-family rerun found that three `additionalProperties: false` in `stop.result.v1.json` could be
set to `true` with every check still green: no bad fixture carried an unknown field there, and the
check that objects are closed only looked for the keyword, not for its value. Both are corrected, for
every schema and not only that one:

- The check now requires `additionalProperties` to be `false` (or a schema for a map's values).
- A new check flips each `additionalProperties: false` to `true`, one at a time, and requires that a
  bad fixture notices. There are 210 such objects in the two folders. Before, 27 of them were proved
  by a fixture; now all 210 are.
- 182 bad fixtures were added for it, named `u-<schema>.<where>.json`: each is a good fixture with one
  field nobody defined (`x-nobody-defined-this`) added at exactly one object. 33 good fixtures were
  added so that every object is reached by one (a successful answer for each method that is new, a
  run with a member, a run view with a typed limit, and a few definitions on their own).

One schema changed with it, and only in a definition used to check fixtures: in `methods.v1.json`
(`1.0-draft.2` with that change), the answer of a `refused_exchange` is typed by its method's rule alone, so that
no object is closed twice over the same value. Nothing a lane builds against moved.

## 1.0-draft.2 (Nov 13, 2026)

The build plan was revised, and read twice by another model family, while draft 1 was being written.
Draft 2 takes in what those reads asked of the records. **If you built against draft 1, these are the
changes that break you:**

| Schema | Change | What it breaks |
|---|---|---|
| `common` | `route.connection` is `{kind, id}`, not the string `kind:id`. The string form stays as `connection_id` for meters and limits | Anything that wrote `route.connection` as a string |
| `common` | `budget` is `{cap_usd}` only, 0 by default. `reservation_id` is gone: the reservation lives in the quota ledger under the run's id | Anything that wrote or read `budget.reservation_id` |
| `run.record` | `request_identity` in `start_key` is replaced by `intent_version` and `intent_sha256`, which come together. It is the only home of a request's identity | The draft 1 name |
| `run.record` | The stored `stop_attestation` is today's shape only (`proof: "empty"`). Draft 1 also allowed `removed` there; that is now reserved for the lane that owns Stop | A store that wrote `removed` |
| `run.record` | `route` is the launch snapshot (`launch_route`): engine, model, connection, and the route contract's own fields when the entry had them | A record with `model: null` in `route` |
| `stop.result` | `attestation` has a new shape (`proof`, `cgroup`, `cgroup_identity`, `time`). `recorded` is only ever `false` | The draft 1 attestation fields in a Stop answer; `recorded: true` |
| `session` | A turn carries `outcome`, `result` and `expires_at`, always. `budget` is always an object | Turn objects without the three fields; `budget: null` |
| `queue.item` | New fields `order` and `delivery_id`, always present. New state `outcome_unknown`. Editable only while `queued` | Items without the two fields |
| `event` | `state.changed` carries a typed post-image (`post_image`, `post_image_sha256`, `reason`, `coalesced`) and no longer `from`, `to` or `registry_seq`. Its entities are run, session and turn | Every draft 1 `state.changed` payload |
| `event` | `usage`: each measurement is `{value, quality, provenance}`. The flat numbers and the single `quality` and `provenance` are gone | Every draft 1 `usage` payload |
| `event` | `approval.requested` carries the request as it was (the `approval.v1` record), not only its id. `approval.answered` carries `answer`, `answered_by` and `revision` | Both draft 1 payloads |
| `event` | `turn.started` adds `delivery_id`. `turn.ended` is `{outcome, result}`. `steer.queued` adds `order`. `steer.delivered` adds `delivery_id`. `interrupt` is `{turn_id, requested_by, outcome}`. `stop.requested` is `{run_id, requested_by, scope}` with scopes `run`, `session`, `subtree`. `limit` adds `bucket`. `route.decision` is `{chosen, reason, candidates, hand_pick}`. `fork.at_turn_id` is never null | Each draft 1 payload of those types |
| `approval` | New field `rendered`, always present. An `opened_by` entry carries the `revision` that was shown, and only an owner or a device can be in it | Draft 1 approval records |
| `driver.v2` | A `user_message` input carries `delivery_id`. `fork.at` is always a turn | Draft 1 inputs |
| `mcp.task` | New field `outcome`. `result` is the turn's result (`code`, `detail`, `result_ref`); `text` is gone. A Task of a run has no session | Draft 1 Task records |

Added without breaking anything:

- `common`: `delivery_id`, `code_word`, `connection`, `owner`, `hand_pick`, `route_decision`,
  `cgroup_identity`.
- `run.record` and `run.view`: optional `route_decision`, `owner`, `cgroup_identity`.
- `stop.result`: a third answer, `ended`, for a run that had already ended (the answer says it ended,
  never that it was stopped); a `fence` fact; a third sentence key for `ended`.
- `event`: types `workspace.grant`, `workspace.revoke` and `effect.outcome_unknown`.
- `approval`: the `summary` that `approval.list` returns.
- New files: `methods.v1.json`, `methods.table.v1.json`, `session.token.v1.json`.

## 1.0-draft.1 (Nov 12 and 13, 2026)

First drafts of `common`, `run.record`, `run.view`, `stop.result`, `session`, `queue.item`, `event`,
then of `approval`, `driver.v2`, `mcp.task` and `transcript.line`.

## Provisional choices

Built as recommended and **not decided**. Each is the owner's decision or another lane's vocabulary.

| Choice | Built as | Alternative | Whose |
|---|---|---|---|
| How large an approved call may be, and what happens to one that is too long to show | Refused with a typed code (`too_large` above 262,144 bytes, `too_many_lines` above 200 lines), never truncated. (The canonical form itself is decided: see "Decided by the owner", above) | Other limits; a prompt that shows a part | The owner's |
| Steering on an engine that gives no typed acceptance mid-turn | Not built either way: `driver.v2` records `input_ack`, and a message sent mid-turn on such an engine ends `outcome_unknown` | Refuse `session.steer` there; or turn it into a next-turn message | The owner's |
| A late acceptance of an item that is already `outcome_unknown` | Changes nothing by itself | Let it make the item `delivered` | The lane that owns the queue |
| Expiry of a Task of a run | null: it does not expire | A retention rule | The lane that builds the MCP face, against the Tasks conformance run |
| Who may Stop | `run.stop` needs a held `run` scope; an owner or device with it may stop any run; a session may stop only its own subtree; the daemon's recovery Stop is not a client's call | Any authenticated caller may stop any run | The owner's |
| Approvals on the MCP face | `approval.list` is a read tool; `approval.open` and `approval.answer` are never tools | No approval tool at all; or answering through MCP with an approve-scoped token | The owner's |
| Names of the containment levels | `host_integrated`, `engine_user`, `contained` | Any other set | The lane that owns Stop, on the owner's decision |
| Keys of the Stop sentences | `cgroup_empty_whole_run_unverified`, `cgroup_removed_whole_run_unverified`, `ended_before_stop_removal_unverified` | Any other set | The lane that owns Stop, on the owner's decision |
| MCP marks of the methods that are new | By effect: `interrupt`, `session.stop` and `workspace.restore` like Stop; `session.start`, `turn`, `steer`, `queue`, `fork`, `resume` like start | Other marks | The owner's |
| What a session token may be granted | Eleven methods; never an approval method, never one that needs `manage` | A wider or narrower list | The lanes that own scopes and delegation |

## Candidates, not in any draft

Event types the contract's research text names and no lane emits yet: `connector.auth_required`,
`quota.exhausted`, `trigger.received`. Each is added when a lane sends a fixture for it. Methods other
lanes' plans mention that the table does not carry: pairing a device (`device.*`), reading and applying
another tool's settings (`config.*`, `prefs.*`).

## Who consumes what

A schema leaves draft when every lane in its row has a fixture here that passes.

| Schema | Lanes |
|---|---|
| `run.record`, `run.view` | CONTROL, STOP, SESSIONS, EVENTS, TUI, PAGE, MCP, QUOTA, ROUTING, WORKSPACE, PLUGIN-HOST |
| `stop.result` | STOP, CONTROL, TUI, PAGE, MCP, DELEGATION |
| `session`, `queue.item` | SESSIONS, TUI, PAGE, MCP, DELEGATION, DRIVER-CC, DRIVER-CX, LOOP |
| `event` | EVENTS, DRIVER-CC, DRIVER-CX, LOOP, APPROVALS, QUOTA, ROUTING, WORKSPACE, TUI, PAGE, MCP, STOP, PLUGIN-HOST |
| `approval` | APPROVALS, DRIVER-CC, DRIVER-CX, LOOP, TUI, PAGE, PLUGIN-HOST |
| `driver.v2` | DRIVER-CC, DRIVER-CX, LOOP, SESSIONS, and each later engine lane |
| `mcp.task` | MCP |
| `transcript.line` | TUI, EVENTS |
| `methods` | CONTROL, MCP, PAGE, TUI, SESSIONS, EVENTS, APPROVALS, WORKSPACE, QUOTA, ROUTING, KEYS, INSTALL, PLUGIN-HOST, DELEGATION |
| `session.token` | CONTROL, DELEGATION, STOP |
