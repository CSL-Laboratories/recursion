# OpenCode fixtures (lane ENGINE-E3)

What `opencode run --format json` prints, for the parser in `routes/engines/opencode.py`.

**Every `.jsonl` file here is SYNTHETIC**: written by hand from reads of OpenCode's source at two tags, never
captured from a real run. `index.json` lists each file with `"synthetic": true`, the package line it stands for,
the exit status it would end with, what it shows, and the drafts the parser must give for it; its `sources` list
cites the source lines each shape was read from. No OpenCode binary ran, nothing was signed in and no login store
was read. A run Lucas captures himself gets its own entry with `"synthetic": false`.

| File | What it is |
|---|---|
| `index.json` | the fixtures, their sources and the expected drafts |
| `synthetic-v1-*.jsonl` | `opencode-ai` 1.19.5 (anomalyco/opencode 810c0eba) |
| `synthetic-v2-*.jsonl` | `@opencode/cli` 2.2.6 (anomalyco/opencode dc2ffcf8) |
| `fake_opencode.py` | a stand-in that replays one fixture byte for byte when given the parser's argv, the prompt on standard input and `OPENCODE_DISABLE_AUTOUPDATE=1` |
| `test_opencode_parser.py` | the parser against every fixture, plus single lines |
| `test_opencode_replay.py` | the fake run as a process: the same drafts come back through a pipe |

The two test modules live here only until the ownership map gives this lane a path under
`routes/engines/tests/`. This folder has no `__init__.py` (the parent `fixtures/` is lane DRIVER-CC's), so
`unittest discover` does not find them; the hosted gate does (its glob `core/**/test_*.py` runs each file by its
entry point). Run them by name from the repository root:

```
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_parser
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.opencode.test_opencode_replay
```
