# Conformance client

A client of the published control records that knows nothing but the schemas, their fixtures and
their vectors. It is the check that someone outside the product can build against what is published:
it imports the Python standard library and `jsonschema` (Draft 2020-12), and nothing from
`recursion_core`.

**Conformance requires the semantic rules, including x-instant; ordinary Draft 2020-12 validation
alone is insufficient. Every client must run the published positive and negative fixtures.** A schema
validator that does not know the keyword `x-instant` ignores it, as the dialect says of any keyword
it does not know, and then takes a timestamp on a day that does not exist; and no schema states the
rules of `rules.py` at all (that a digest is its effect's, that a page is in the projection that was
asked for, that a binding names the approval it is used against). A client that validates the shapes
and stops there accepts records that this folder's negative fixtures and vectors refuse, and is not
conformant.

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
  standard library alone: the strict reading of a tool's input, the bound form of a number, the text
  of a value as RFC 8785 writes it, the digest of an effect, the lines of an approval,
  `approval.open` and `approval.answer` one call at a time with its deadline, a change of content
  made whole or not at all, and the single consumption against a binding that names the approval,
  what a transcript line must carry because its event says so, what each thing that happens to a
  queued message does to it against the binding committed before the write, the Task of a run and of
  a turn, the identity of a start request, reading a log from a cursor in the projection that was
  asked for, and the digests an event carries. A client in another language implements these and
  checks the same vectors.
- `tests.py`: all of it as tests. Its class `RulesTest` holds what a vector file cannot: the host's
  own state around a rule (the Decimal context a number is read under), a record changed behind a
  model's back, a request the method table would not let into an exchange. A client in another
  language needs the same cases in its own tests; each test's first lines say what it holds.

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
  (seq, time, type, source, trust, state, and the text where it is the event's own); a queued message
  goes through its steps as the rule says, each step leaving exactly the item its vector gives or
  being refused with exactly its code; a Task differs from what its source says in exactly the fields
  its vector lists (none, for a Task that is right). Every rule has vectors that agree and vectors it
  must catch;
- in a queue vector, what the core is told by an event is read from the event: an acceptance that
  delivers an item is tried without its event, with the core's own write in its place, with another
  turn, log, run, `how` or delivery, and after a second write, and each time the vector is refused;
- a start request stores exactly the `start_key` its vector gives, from exactly the preimage its
  vector gives; a cursor is answered as its vector says; a page has exactly the faults its vector
  lists against the request it was asked with (its cursor, and the projection it asked for, which is
  `raw` when it names none), and so has no fault in any good exchange, where the same page under
  the other projection is caught; a transcript page that carries the raw page of the same read is
  the projection of a raw page that is itself right;
- in an approval vector records are compared exactly (1 is not 1.0 and 0 is not -0.0), a refused step
  leaves the record and the private binding as they were, and the binding never changes;
- wherever a good fixture or a vector holds an event, a `state.changed` carries the digest of its
  post-image and a `tool.call` the digest of its input, and in the good fixtures a `replay.gap` is a
  span filed under its last position;
- every field typed as a timestamp holds a moment that exists, with seconds from 00 to 59 (the
  keyword `x-instant`, which this client adds to its validator; the check stands without the
  pattern);
- in the method table, the seven mandatory built-ins are exactly the rows that cannot answer
  `unsupported`; every row can answer `unavailable`; a built-in that waits on no service answers it
  only for a closing daemon (`run.start` also for a start registrant that refused, `plugin.list` also
  with today's own words when no plugin host is composed); a row that names no service, which today
  is every row, can never answer `service_starting`; and each row's `falls_back` and `service` are
  the ones pinned in `tests.py`;
- a Task vector agrees with the rule for every state a run's view can show and every state a turn can
  be in, and every status a Task can have is reached from each source;
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
folder. `tools/test_wire_conformance.py` is the bridge: it loads every test class of `tests.py` where
the gate's own runner collects them, and its one check of its own is that it carries all of them (a
class added to `tests.py` once ran by hand and not under the gate, because the bridge named its
classes one by one). The gate is not edited. This was the owner's decision of Nov 13, 2026, marked
for follow-up research.

The test file here is still named `tests.py`, not `test_*.py`: the gate fails when it finds a
test-named file outside the folders it runs, and `unittest discover` finds `tests.py` by its default
pattern all the same.
