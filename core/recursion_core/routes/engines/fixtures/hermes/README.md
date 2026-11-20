# Hermes fixtures (synthetic)

The parser in `routes/engines/hermes.py` reads the JSONL stream of
`hermes chat --query-file - --format stream-json`. The files here test it.

- `streams/*.jsonl`: fourteen stdout streams. **Every one is synthetic**: written by hand
  (by `generate_streams.py`) in the shapes the pinned upstream emitter writes
  (`hermes_cli/stream_json.py` at NousResearch/hermes-agent
  `2f7e62bad99052055d1db9213b81eaab26174bc4`), not captured from a real Hermes. `cases.json`
  marks each one and says what it covers; where a case writes something the pinned emitter never
  writes, it says so. A captured stream, when one exists, goes beside these and is marked captured.
- `fake_hermes.py`: a fake `hermes` that replays one stream, for end-to-end tests of the argv
  and the prompt on standard input. It is not Hermes.
- `test_hermes_parser.py`, `test_hermes_fake.py`: the tests. They live here until the path map
  gives this lane a test folder of its own. Run them from the repository root by module name:

```
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.hermes.test_hermes_parser
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.hermes.test_hermes_fake
```

`unittest discover` does not descend here (no `__init__.py`), so a whole-suite discovery run does not
include these tests; the repository's gate runs each `test_*.py` file under `core/` by its path.
