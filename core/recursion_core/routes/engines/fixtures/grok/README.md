# Grok fixtures (lane ENGINE-E4)

Everything here is lane ENGINE-E4's (`ownership.json`: `routes/engines/fixtures/grok/` in every phase).

## What is here

| Path | What it is |
|---|---|
| `oneshot/*.json` | One run of the one-shot drive each: standard output line by line, the exit code, and what the parser must make of it. |
| `fake_grok.py` | A fake `grok` executable for the one-shot drive. It checks its arguments and replays one case file byte for byte. |
| `test_grok_oneshot.py` | The tests of `routes/engines/grok.py`, against these cases and the fake. |

The tests live here, not in `routes/engines/tests/`, because that folder is lane DRIVER-CC's until the
ownership map gives this lane a rule there (START.md, "FOR YOUR LANE"). This folder has no
`__init__.py` (`routes/engines/fixtures/__init__.py` is DRIVER-CC's), so `unittest discover` does not
find the tests; run them by module name from the repository root:

```
PYTHONPATH=core python -m unittest recursion_core.routes.engines.fixtures.grok.test_grok_oneshot -v
```

## Every case is SYNTHETIC

No Grok binary was run and nothing was captured. Each case says `"synthetic": true` and where its lines
come from in `"provenance"`. The line shapes are those of xAI's public mirror `xai-org/grok-build` at
`2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8` (Sep 29, 2026): `headless/reducer/acp.rs` for the
`streaming-json` lines, `headless.rs` for when `end` and `error` are written and how the process
exits, `extensions/notification.rs` for the spend fields, and the headless guide
(`docs/user-guide/14-headless-mode.md`). The shipped build (1.0.77 on Nov 5, 2026) is newer than that
mirror. A capture from Lucas's attended run replaces these as evidence when it exists; the brief's
first proof asks for that replay.

## Case file

```
{"synthetic": true, "provenance": "...", "drive": "oneshot",
 "stdout": [<line>, ...], "exit_code": <int>,
 "expect": {"code": <null or a code>, "drafts": [<draft type>, ...], "text": "<the answer>"}}
```

A `<line>` that is a JSON object is written as compact JSON (no spaces, UTF-8 as is) and a newline; a
`<line>` that is a string is written exactly as it stands, newline included. `expect.drafts` is the
type of every draft the parser makes, in order, the last one the `result` of `finish`.
