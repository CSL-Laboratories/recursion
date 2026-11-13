# Conformance client

A client of the published control records that knows nothing but the schemas and their fixtures. It
is the check that someone outside the product can build against what is published: it imports the
Python standard library and `jsonschema` (Draft 2020-12), and nothing from `recursion_core`.

```
python3.12 -m unittest discover -s conformance      # one test per fixture, plus consistency checks
python3.12 conformance/wire_client.py               # one line per fixture; exit 1 on any failure
```

Run it on Python 3.12 with `jsonschema` installed (`requirements-ci.txt`). The daemon's own
interpreter does not have `jsonschema`, and nothing under `core/` may import it.

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
- `fixtures/index.json` and the fixture folders list exactly the same files;
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

## Why the test file is named `tests.py`

The hosted gate (`tools/ci_gate.py`) fails when it finds a file named `test_*.py` outside the folders
it runs, and it does not run this folder. `unittest discover` finds `tests.py` by its default pattern,
and the gate does not count it. So today these checks run where someone runs the commands above; the
hosted gate does not run them. Making the gate run them is a change to the gate, which is frozen.
