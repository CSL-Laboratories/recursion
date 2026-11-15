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
