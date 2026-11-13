# Conformance client

A client of the published control records that knows nothing but the schemas, their fixtures and
their vectors. It is the check that someone outside the product can build against what is published:
it imports the Python standard library and `jsonschema` (Draft 2020-12), and nothing from
`recursion_core`.

```
python3.12 -m unittest discover -s conformance      # EVERYTHING: one test per fixture and per vector, and the consistency checks
python3.12 conformance/wire_client.py               # the fixtures and the vectors only, one line each; exit 1 on any failure
```

The second command is the quick one and is not the whole check: it does not run the consistency checks
listed below (that the index and the folders agree, that every reference resolves, that every rule is
proved by a fixture, that the method table agrees with its schema). It says so in its last line.

Run it on Python 3.12 with `jsonschema` installed (`requirements-ci.txt`). The daemon's own
interpreter does not have `jsonschema`, and nothing under `core/` may import it.

Three files:

- `wire_client.py`: reads the schemas, the fixtures and the vectors, and checks one of each.
- `rules.py`: the rules a JSON Schema cannot state, as plain functions over plain values, importing the
  standard library alone: the strict reading of a tool's input, the text of a value as RFC 8785 writes
  it, the digest of an effect, the lines of an approval, `approval.open` and `approval.answer` one
  call at a time, and what a transcript line must carry because its event says so. A client in another language implements these and checks the same vectors.
- `tests.py`: all of it as tests.

## What it checks

For each folder of schemas (`schemas/control/v1`, and `schemas/plugin/v1` once it exists):

- every schema is valid Draft 2020-12, carries a version tag, and has its file name as its `$id`;
- every `$ref` written in a schema resolves;
- every object is closed (`additionalProperties` is `false`, or a schema for a map's values), except
  the few that are open on purpose, which are listed by name;
- every rule of four kinds is proved by a fixture: each `additionalProperties: false`, each name in a
  `required` list, each `enum` and each `const` is taken away, one at a time, and a bad fixture must
  notice (one that names that keyword at that place, and that is accepted, or refused for another
  reason, once the rule is gone). So no object can be opened, no required field made optional, and no
  list of allowed values or fixed value dropped without a fixture turning red. Rules inside an `if` or
  a `not` are conditions and prohibitions and are not counted. The generated fixtures exist for this:
  each is a good fixture with one change (`u-*` a field nobody defined added, `m-*` a required field
  removed, `v-*` a value replaced by one outside the list, `f-*` a fixed value replaced). The root of
  the method table's schema is proved from the table file itself, changed the same way;
- the method table validates against its schema, and for every method the row, the params schema and
  the answer schema agree (the same codes, a `command_id` exactly where the row says, no `principal` or
  `owner` among the params), no code is both typed by a method's own answer and allowed as its bare
  refusal, and only `run.stop` mutates without a key;
- every vector agrees with its rule (`vectors/index.json`): a tool call becomes exactly the text that is
  bound for it, or is refused with exactly its code; an approval record of the right shape has exactly
  the problems its vector lists (none, or a digest that is not its effect's, lines that are not its
  effect's, a call outside its profile); a sequence of `approval.open` and `approval.answer` calls is
  answered step by step as the protocol says; the lines of a transcript carry what their events say
  (seq, time, type, source, trust, state, and the text where it is the event's own). Every rule has
  vectors that agree and vectors it must catch;
- every approval inside a good fixture, wherever it sits, has the digest of its effect and the lines
  of its effect;
- no fixture is checked against a schema that refuses nothing (the root of a file that only holds
  definitions, say);
- `fixtures/index.json` and the fixture folders list exactly the same files, and so do
  `vectors/index.json` and the vector folders;
- every file under `fixtures/good/` validates;
- every file under `fixtures/bad/` is refused **for its own rule**: the index names the schema keyword
  that refuses it and the place in the fixture where it does, and the client checks that this very
  error is among the validator's errors. A bad fixture that is refused for some other reason fails;
- every schema has at least three good and three bad fixtures;
- every event type has exactly one payload rule and at least one good fixture;
- the public run view carries every field of the stored run record;
- no file in this folder imports the product.

## The fixture index

```
{"file": "good/event.v1/h03-engine-output.json", "schema": "event.v1.json", "shows": "..."}
{"file": "bad/event.v1/b03-engine-content-marked-trusted.json", "schema": "event.v1.json",
 "rule": "Content from an engine is always 'untrusted'.", "expect": {"keyword": "const", "at": "/trust"}}
```

A good transcript line also carries `of`: the event fixtures it is the projection of (the first event
of the log, then its own event), and the suite checks that the line carries what those events say.

`schema` may carry a fragment (`common.v1.json#/$defs/cursor`). `at` is a JSON Pointer into the fixture;
the empty string is the whole value. `expect` is what this client checks with one validator; a client
in another language checks the verdict (accepted or refused) and may report the error differently.

## Adding a fixture

Put the record under `fixtures/good/<schema file without .json>/` or `fixtures/bad/...`, add its line
to `fixtures/index.json`, and run the two commands above. A consumer lane sends its fixtures to lane
SPINE, which adds them here.

A schema change that adds a closed object, a required field, an `enum` or a `const` needs a bad
fixture that breaks exactly that rule: the test names every rule that has none. Take a good fixture
that reaches the rule, make the one change, and list it with the keyword and the place.

## How the hosted gate runs this

The hosted gate (`tools/ci_gate.py`) runs every file named `tools/test_*.py` and does not reach this
folder. `tools/test_wire_conformance.py` is the bridge: it loads the test cases of `tests.py` where
the gate's own runner collects them, and adds no check of its own. The gate is not edited. This was
the owner's decision of Nov 13, 2026, marked for follow-up research.

The test file here is still named `tests.py`, not `test_*.py`: the gate fails when it finds a
test-named file outside the folders it runs, and `unittest discover` finds `tests.py` by its default
pattern all the same.
