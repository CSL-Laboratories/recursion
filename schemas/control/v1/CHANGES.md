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
| `common.v1.json` | 1.0-draft.2 |
| `run.record.v1.json` | 1.0-draft.2 |
| `run.view.v1.json` | 1.0-draft.2 |
| `stop.result.v1.json` | 1.0-draft.2 |
| `session.v1.json` | 1.0-draft.3 |
| `queue.item.v1.json` | 1.0-draft.3 |
| `event.v1.json` | 1.0-draft.3 |
| `approval.v1.json` | 1.0-draft.3 |
| `driver.v2.json` | 1.0-draft.2 |
| `mcp.task.v1.json` | 1.0-draft.2 |
| `transcript.line.v1.json` | 1.0-draft.2 |
| `methods.v1.json` and `methods.table.v1.json` | 1.0-draft.4 |
| `session.token.v1.json` | 1.0-draft.1 |

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
input sent into a turn in progress is not settled: the options are with the build's coordinator, and
this record's `delivered` keeps its draft-2 wording until they are.

### `methods.v1.json` is `1.0-draft.4`

| Change | What it breaks |
|---|---|
| A refusal's `run_id` is any non-empty string: today's daemon looks up an id that is not well formed and answers `run_unknown` with it, as given. Two such answers are captured (`c18`, `c19`) | Nothing. A reader must not assume an echoed run id is well formed |
| A method's `unsupported` names `not_implemented` or a driver flag its row requires, and nothing else; the table check holds the row's `requires` to exactly those flags | An `unsupported` that names a flag the method does not require |
| `requires` means: without the flag the method is refused, whatever else is true. `session.interrupt` now requires `interrupt`. A method that only behaves differently without a flag does not list it | A caller that expected `session.interrupt` to answer `ok` on a driver that cannot interrupt |
| The payload digest of a command has a named form: SHA-256 of the UTF-8 bytes of the params without the command id, as RFC 8785 writes them | Nothing published |
| Said in the rules, not new: a name with no row is `method_unknown`, a reserved name included, and `methods.list` never lists it; a row without a handler answers `unsupported`, `not_implemented` (31 exchanges now show it, one for each method not built yet). `run.start`'s key reused with another engine or prompt stays `request_invalid` with detail `client_key_reused`, as today (`control/api.py`), and is not turned into a new code | Nothing |

### Not changed yet, on purpose

`mcp.task.v1.json` stays at draft 2. The Task of a run that has no session needs facts the run record
does not keep (why a start failed, after a restart; when the state last changed) and a rule from a
run's result to the Task's. Those cross lanes; the options are with the build's coordinator, and no
lane should treat that half of the Task as agreed. The transcript fixtures are not yet checked as
projections of the events they are paired with; that is next.

### The checks themselves

`conformance/rules.py` and `vectors/` are new (42 vectors: 24 of the call profile, 16 of what an
approval binds, 2 sequences of calls). `wire_client.py` run as a command checks the fixtures and the
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
| Canonical form behind the approval digest and `tool.call.input_digest` | `csl-cj/v0`, named in the record as `digest_form`. Because that form takes ASCII text and whole numbers only, an engine tool's input is carried inside the effect as an escaped JSON string (`input_json`) | RFC 8785, for engine effects or for all | The owner's |
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
