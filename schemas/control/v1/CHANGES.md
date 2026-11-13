# Changes to the control records, version 1

Each schema carries its own version in `x-version`. While a schema is `1.0-draft.N`, any change bumps
`N` and is listed here with what it breaks. A schema becomes `1.0` when every consumer lane named for
it in `README.md` has a passing fixture in `fixtures/`; from then on it changes only by adding, as
`1.1`, `1.2` and so on, and each addition is listed here. When a schema becomes `1.0`, this file
records the SHA-256 of the schema together with everything it refers to, so that a later edit cannot
pass unnoticed.

## 1.0-draft.1 (Nov 12, 2026)

First drafts. Nothing is frozen.

| Schema | Version | Notes |
|---|---|---|
| `common.v1.json` | 1.0-draft.1 | Shared definitions |
| `run.record.v1.json` | 1.0-draft.1 | The twelve stored fields as they are today; nine optional additions; `request_identity` inside `start_key` |
| `run.view.v1.json` | 1.0-draft.1 | Today's answers of `run.start`, `run.status` and `run.list`, captured |
| `stop.result.v1.json` | 1.0-draft.1 | Today's answer, plus optional `attestation`, `containment_level`, `claim` |
| `session.v1.json` | 1.0-draft.1 | |
| `queue.item.v1.json` | 1.0-draft.1 | |
| `event.v1.json` | 1.0-draft.1 | Twenty-one types, one payload shape each |

### Provisional choices in this draft

These are built as recommended and are **not decided**. Each is the owner's decision or another lane's
vocabulary.

| Choice | Built as | Alternative | Whose |
|---|---|---|---|
| Canonical form behind `tool.call.input_digest` (and the approval digest to come) | `csl-cj/v0`, named in the record | RFC 8785 | The owner's |
| Names of the containment levels | `host_integrated`, `engine_user`, `contained` | Any other set | Lane STOP's, on the owner's decision |
| Keys of the Stop sentences | `cgroup_empty_whole_run_unverified`, `cgroup_removed_whole_run_unverified` | Any other set | Lane STOP's, on the owner's decision |

### Where this draft departs from the build plan's field lists

- `request_identity` lives inside `start_key`, not beside it. The plan lists it among the record's
  additions and also says the API stores it in `start_key` and compares it on a retry. One home is
  kept: the start key, which is what a retry is compared with.
- The log of a run without a session is named `run:<run_id>`, so `event.session_id` and
  `cursor.session_id` are a log id, not always a session id.
- Added: `usage.span`, `usage.reasoning_tokens`, `limit.model`, `approval.answered.state`,
  `state.changed.entity_id`, `session.created_at`, `queue.item.turn_id`.
- The four event types the first plan took from the contract's research text
  (`connector.auth_required`, `quota.exhausted`, `effect.outcome_unknown`, `trigger.received`) are not
  in this draft. The second plan's list does not carry them. They are candidates for an addition.
