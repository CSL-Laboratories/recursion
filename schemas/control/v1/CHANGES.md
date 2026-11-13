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
| `session.v1.json` | 1.0-draft.2 |
| `queue.item.v1.json` | 1.0-draft.2 |
| `event.v1.json` | 1.0-draft.2 |
| `approval.v1.json` | 1.0-draft.2 |
| `driver.v2.json` | 1.0-draft.2 |
| `mcp.task.v1.json` | 1.0-draft.2 |
| `transcript.line.v1.json` | 1.0-draft.2 |
| `methods.v1.json` and `methods.table.v1.json` | 1.0-draft.2 |
| `session.token.v1.json` | 1.0-draft.1 |

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
(now `1.0-draft.2`), the answer of a `refused_exchange` is typed by its method's rule alone, so that
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
