Written by Claude Opus 5.5 (`claude-opus-5-5`), builder lane ENGINE-E6, for Lucas.

# Cursor engine fixtures and tests

The fixtures, the fake `agent` and the tests of `routes/engines/cursor.py` (lane ENGINE-E6, first slice:
the parser, and the unattended switch written as data that is off).

**Every fixture here is synthetic.** None was captured from a signed-in Cursor run: no lane signs in to an
engine, and none has run. Each stream fixture's first line is `{"type": "_fixture", ...}`, naming who wrote
it, that it is synthetic, where its shapes come from and what in it is guessed; `index.json` lists every
fixture with the same facts. The fake `agent` never replays that line. When Lucas runs Cursor attended and
hands over a redacted stdout, it is added here as a captured fixture beside these, and the synthetic ones
it disagrees with are corrected.

| Path | What it is |
|---|---|
| `oneshot/*.jsonl` | one-shot `agent -p --output-format stream-json` streams |
| `oneshot/*.expected.json` | the drafts and parser state each stream must give, for the input named in it |
| `acp/*.jsonl` | `agent acp` transcripts, one `{"dir": "client" or "agent", "msg": <JSON-RPC>}` per line, for lane ENGINE-E5's ACP client to replay |
| `argv.json` | each drive's argv and argv this lane refuses |
| `fake_agent.py` | `FakeLauncher` and two `EngineProcess` doubles that replay the fixtures; no process is started |
| `test_cursor_*.py` | the tests |

The tests live here, not under `routes/engines/tests/`, because that folder is lane DRIVER-CC's; they
move there when the map gives this lane a rule for it. This folder has no `__init__.py` (nor has
`fixtures/`, which is DRIVER-CC's), so `unittest discover` does not find them: run each by name.

```
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.cursor.test_cursor_parser \
    recursion_core.routes.engines.fixtures.cursor.test_cursor_switch \
    recursion_core.routes.engines.fixtures.cursor.test_cursor_argv \
    recursion_core.routes.engines.fixtures.cursor.test_cursor_fixtures \
    recursion_core.routes.engines.fixtures.cursor.test_cursor_fake_agent
```

`test_cursor_fixtures` reads `schemas/control/v1/` (lane SPINE's, #368) from the repository root.

The ACP transcripts follow the ACP v1 schema at `agentclientprotocol/agent-client-protocol`
`13b765828f961812f0c78ae577220e994209fa2f` (`schema/v1/schema.json`, SHA-256 in `index.json`); the core's
interpreter has no JSON Schema library, so `test_cursor_fixtures` checks each method's shape by hand.

## The interface of `routes/engines/cursor.py` (nothing imports it yet; nothing registers it)

| Name | Shape | Notes |
|---|---|---|
| `StreamParser()` | one per worker, one reader, lines in order; no lock | Cursor's one-shot stream-json |
| `StreamParser.wrote(delivery_id, turn_id, text) -> None` | `dlv-` + 1 to 60 of `[A-Za-z0-9_-]`; `turn-` + 1 to 59; non-empty text | `ValueError` for a bad argument; `StreamStateError` for a second write (the one-shot drive takes one input) |
| `StreamParser.parse(line: str) -> list[dict]` | driver.v2 drafts: `tool.call`, `tool.result`, `result`, `input.accepted` | `TypeError` if `line` is not text; never drafts `engine.output`, `usage` or `limit`; the first result is terminal |
| parser state | `engine_session_id`, `model`, `api_key_source` (`env`, `flag`, `login` or None), `result_seen`, `result_code`, `final_text`, `counts` | `final_text` is set only by a successful result whose answer is text |
| `COUNTS` | `not_json`, `unknown_type`, `no_session`, `other_session`, `after_result`, `result_repeated`, `unknown_subtype`, `init_repeated`, `user_unmatched`, `call_invalid`, `tool_shape_unknown`, `call_repeated`, `call_undigestible`, `input_withheld`, `result_unpaired`, `text_invalid` | what a line gave instead of a draft; each name is described in the module |
| `input_digest(value) -> str` | SHA-256 hex of the RFC 8785 text (the core's `control.methods.canonical_text`) | `NoCanonicalText` (a `ValueError`) for a value with no such text |
| `MAX_INPUT_BYTES`, `MAX_RESULT_BYTES` | 65,536 each | this lane's choice (questions.md Q4): a larger input is drafted with `input` null; a longer result text is cut, `truncated` true |
| `oneshot_declaration() -> dict` | `{"engine": "cursor", "driver": 2, "flags": {eight, all false}, "input_ack": false}` | |
| `oneshot_argv(model, *, binary=None) -> list[str]` | `[argv0, "--disable-auto-update", "-p", "--output-format", "stream-json", "--model", model]` | argv0 is `binary` (a plain absolute path) or `agent`; the updater flag stands first on both drives |
| `acp_argv(*, binary=None) -> list[str]` | `[argv0, "--disable-auto-update", "acp"]` | |
| `check_argv(argv, *, binary=None, full_access_acknowledged=False) -> None` | raises `ArgvRefused` | call it on the complete argv, after `--` and the prompt are appended (the bounds hold over what it is given and nothing else); the options are the arguments before the first `--`; a credential option is refused in any spelling (`-vH V` too); `--force`, `-f`, `--yolo`, `--approve-mcps`, Cursor's option that switches its own isolation off (spelled out in `check_argv`), and `--dangerously-skip-permissions`, `--auto-approve` (not Cursor's) only with `full_access_acknowledged=True` (any value but a bool is `full_access_malformed`); argv[1] must be the updater flag and argv[2] a drive; an argument with a NUL or a lone surrogate, or over either bound, is `argv_invalid` |
| `ArgvRefused` | a `ValueError` with `.code == "launch_refused"` and `.detail` one of `full_access_malformed`, `argv_invalid`, `binary_not_absolute`, `model_invalid`, `executable_mismatch`, `credential_in_argv:<option>`, `full_access_not_acknowledged:<option>`, `updater_not_disabled`, `drive_missing` | |
| `MAX_ARG_BYTES` | 131,071 | Linux's one-argument limit less its NUL |
| `MAX_ARGV_BYTES` | 1,048,576 | the whole argv, each argument with its NUL: half of Linux's usual ARG_MAX, the rest left to the environment |
| `DRIVES` | `("-p", "acp")` | what argv[2] must be: the one-shot drive or `agent acp` |
| `unattended_switch() -> dict` | `{"engine": "cursor", "switch": "unattended", "state": "off", "refusal": "cursor_unattended_off", "waits_on": "counsel"}` | a copy of a read-only record; nothing reads it yet |
| `unattended_refusal() -> dict` | `{"code": "cursor_unattended_off", "detail": <text>}` | run.record.v1's `start_error` shape |

`fake_agent.py`: `FakeLauncher(fixture)(argv, *, env, shell=False, **options)` records each launch in
`.launches` and hands out the process in `.processes`; `fixture_lines(name)` and `fixture_header(name)` read
a fixture. The ACP process takes only the transcript's own next client message, compared by `same_json` (a
boolean is never a number; request ids are the client's own), raises `FakeScriptError` for anything else and does not take it, raises `ReadTimeout` while
the agent waits for the client, and gives end of file when the transcript is done. Neither fake runs,
times or answers anything a fixture does not hold.
