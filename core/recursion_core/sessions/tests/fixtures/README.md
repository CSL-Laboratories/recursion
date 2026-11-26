Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.

# Fixtures of lane SESSIONS

Records this lane's own code writes, taken at fixed points of one scenario by `../make_fixtures.py`, which
`../test_fixtures.py` runs again on every test run: a file here must be exactly what the code writes, and on an
interpreter with `jsonschema` it must validate against the schema `index.json` names (SPINE's, #368).

- `session.v1/`: the session record at each step of a turn, a Stop pending and settled, and a turn whose unit
  could not start.
- `queue.item.v1/`: a steer and a next-turn message from queued to delivered, an edit, a withdrawal, an outcome
  unknown after a restart, and two private delivery bindings (`b01`, `b02`).
- `event.v1/`: the payload of each event type the lane appends.

The ids and times are the tests' fixed ones, not real ones. A JSON file cannot carry the model line, so it is
here and in `index.json`. To write them again: `PYTHONPATH=core python -m recursion_core.sessions.tests.make_fixtures`.
