"""The conformance checks as tests: ``python -m unittest discover -s conformance``.

One test per fixture, plus checks that the published files are consistent with each other. Nothing here
imports the product. The file is named ``tests.py`` so that ``unittest discover`` finds it by its default
pattern; see README.md for why it is not named ``test_*.py``.
"""
from __future__ import annotations

import ast
import copy
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import wire_client as client  # noqa: E402

VERSION = re.compile(r"1\.0(-draft\.[1-9][0-9]*)?")
# What the method table says beyond its codes, pinned here so that a row cannot lose it unnoticed.
MANDATORY = ("run.start", "run.status", "run.list", "run.stop", "route.list", "plugin.list", "methods.list")
CLOSING_ONLY = {"run.start": ["daemon_closing", "start_registrant_failed"], "run.status": ["daemon_closing"],
                "run.list": ["daemon_closing"], "run.stop": ["daemon_closing"], "route.list": ["daemon_closing"],
                "methods.list": ["daemon_closing"]}
# Every 'unavailable' detail a mandatory built-in can give. plugin.list keeps today's own refusal, word for word.
UNAVAILABLE = dict(CLOSING_ONLY, **{"plugin.list": ["daemon_closing", "no plugin host is composed"]})
FALLS_BACK = {"session.steer": [{"flag": "mid_turn_input", "then": "next_turn"}],
              "session.interrupt": [{"flag": "interrupt", "then": "stop_then_resume"}]}
SERVICES = (None,)      # the services a row may name. None yet: a lane that registers one asks for its name here


class PublishedFilesTest(unittest.TestCase):
    """The schemas, the index and the fixture folders agree with each other."""

    @classmethod
    def setUpClass(cls):
        cls.sets = {folder: client.SchemaSet(folder) for folder in client.folders()}

    def test_there_is_something_to_check(self):
        self.assertTrue(self.sets, "no schema folder found")
        for folder, schemas in self.sets.items():
            self.assertTrue(schemas.schemas, f"{folder}: no schema")

    def test_every_schema_carries_a_version_tag(self):
        for schemas in self.sets.values():
            for name, schema in schemas.schemas.items():
                with self.subTest(schema=name):
                    self.assertRegex(schema.get("x-version", ""), VERSION)
                    self.assertTrue(VERSION.fullmatch(schema["x-version"]))

    def test_every_ref_resolves(self):
        for schemas in self.sets.values():
            for name, ref in schemas.refs():
                with self.subTest(schema=name, ref=ref):
                    schemas.resolve(ref, base=name)

    def test_every_object_schema_is_closed(self):
        """An object schema that lists properties refuses every other property.

        It does so itself (``additionalProperties``), or in every one of its forms: each branch of its
        ``oneOf``, or each ``then`` of the ``if`` rules in its ``allOf``. A sub-schema inside a rule
        (``allOf``, ``if``, ``then``, ``else``, ``not``) refines an object that is closed where it is
        defined, so it is not asked again. A few objects are open on purpose and are listed here, each with its reason
        in the schema: they hold an engine's, a vendor's or a plugin's own data, and no consumer reads
        typed fields out of them.
        """
        open_on_purpose = {
            ("event.v1.json", "/properties/payload"),                     # typed per type by the rules below it
            ("event.v1.json", "/$defs/state.changed/properties/post_image"),  # typed per entity by the rules below it
            ("event.v1.json", "/$defs/tool.call/properties/input"),       # the tool's own input, untrusted
            ("run.record.v1.json", "/$defs/launch_route/properties/profile"),  # the engine adapter's own object
            ("run.view.v1.json", "/$defs/result/oneOf/1/properties/events/items/properties/data"),  # the adapter's
            ("run.view.v1.json", "/$defs/result/oneOf/1/properties/limit_notice"),                  # the adapter's
            ("methods.v1.json", "/$defs/request/properties/params"),      # typed per method by the rules below it
            ("methods.v1.json", "/$defs/exchange/properties/params"),     # typed per method by the rules below it
            ("methods.v1.json", "/$defs/exchange/properties/answer"),     # typed per method by the rules below it
            ("methods.v1.json", "/$defs/refused_exchange/properties/params"),  # a malformed request, on purpose
            ("methods.v1.json", "/$defs/refused_exchange/properties/answer"),  # typed per method by the rules below it
            ("methods.v1.json", "/$defs/params.operator.read/properties/command"),          # the operator plugin's
            ("methods.v1.json", "/$defs/answer.operator.read/anyOf/0/properties/body"),     # the operator plugin's
            ("methods.v1.json", "/$defs/params.plugin.call/properties/arguments"),          # the plugin's own
            ("methods.v1.json", "/$defs/answer.plugin.call/anyOf/0/properties/result"),     # the plugin's own
            ("api.v1.json", "/$defs/registry/properties/panels/items"),   # a panel's shape is the page's
            ("api.v1.json", "/$defs/registry/properties/required"),       # the page's
            ("api.v1.json", "/$defs/tool/properties/input_schema"),       # a JSON Schema, the plugin's own
            ("approval.v1.json", "/properties/effect"),                   # typed per source by the rules below it
            ("approval.v1.json", "/$defs/plugin_effect/properties/payload"),  # the plugin's own data, never read
            ("approval.v1.json", "/$defs/engine_tool_effect/properties/input"),  # the tool's own input, never interpreted
            ("driver.v2.json", "/$defs/event_draft/properties/payload"),  # typed per type by the rules below it
        }
        rules = ("allOf", "if", "then", "else", "not")

        def is_object(node):
            types = node.get("type")
            return types == "object" or (isinstance(types, list) and "object" in types)

        def closed(node):
            """additionalProperties is false, or a schema for the values of a map. True closes nothing."""
            value = node.get("additionalProperties", True)
            return value is False or isinstance(value, dict)

        def closes_itself(node):
            if "additionalProperties" in node:
                return closed(node)
            branches = node.get("oneOf")
            if branches and all(isinstance(b, dict) and closed(b) for b in branches):
                return True
            ifs = [rule for rule in node.get("allOf", ()) if isinstance(rule, dict) and "if" in rule]
            return bool(ifs) and all(closed(rule.get("then", {})) for rule in ifs)

        def walk(node, path, refining):
            if isinstance(node, dict):
                if is_object(node) and not refining and not closes_itself(node):
                    yield path
                for key, value in node.items():
                    if key in ("properties", "$defs") and isinstance(value, dict):
                        for name, sub in value.items():
                            yield from walk(sub, f"{path}/{key}/{name}", refining and key != "$defs")
                    else:
                        yield from walk(value, f"{path}/{key}", refining or key in rules)
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    yield from walk(item, f"{path}/{i}", refining)

        seen = set()
        for schemas in self.sets.values():
            for name, schema in schemas.schemas.items():
                found = {(name, path) for path in walk(schema, "", False)}
                seen |= found
                self.assertEqual(sorted(found - open_on_purpose), [], f"{name}: object schemas that are not closed")
        if any("event.v1.json" in s.schemas for s in self.sets.values()):
            self.assertEqual(sorted(open_on_purpose - seen), [], "listed as open on purpose but not found: update the list")

    # A rule that is only written down is not proved. The four tests below take every rule of one kind away,
    # one at a time, and require that a bad fixture notices: with the rule gone, a fixture that names that
    # keyword at that place is accepted, or is no longer refused for its own rule. So no closed object can
    # be opened, no required field made optional, and no list of allowed values or fixed value dropped
    # without a fixture turning red. Rules inside an ``if`` or a ``not`` are conditions and prohibitions,
    # not rules of these kinds. The method table's own root is proved from the table file (see
    # ``wire_client.data_file_notices``).
    _unproved: dict = {}

    def unproved(self, kind):
        """{folder: [rule, ...]} of the rules of ``kind`` that nothing proves; every folder has such rules."""
        if not self._unproved:
            for folder, schemas in self.sets.items():
                self._unproved[folder] = (client.rules_of(schemas), client.unproved_rules(schemas))
        found = {}
        for folder, (rules, unproved) in self._unproved.items():
            self.assertTrue(any(rule.kind == kind for rule in rules), f"{folder}: no {kind} rule found")
            found[folder] = [str(rule) for rule in unproved if rule.kind == kind]
        return found

    def test_every_closed_object_is_proved_closed_by_a_fixture(self):
        """Turn each ``additionalProperties: false`` to true, one at a time: a bad fixture must notice."""
        for folder, unproved in self.unproved("additionalProperties").items():
            self.assertEqual(unproved, [], f"{folder}: closed objects that no bad fixture proves closed")

    def test_every_required_field_is_proved_required_by_a_fixture(self):
        """Drop each name from each ``required`` list, one at a time: a bad fixture must notice."""
        for folder, unproved in self.unproved("required").items():
            self.assertEqual(unproved, [], f"{folder}: required fields that no bad fixture proves required")

    def test_every_list_of_allowed_values_is_proved_by_a_fixture(self):
        """Delete each ``enum``, one at a time: a bad fixture must notice."""
        for folder, unproved in self.unproved("enum").items():
            self.assertEqual(unproved, [], f"{folder}: lists of allowed values that no bad fixture proves")

    def test_every_fixed_value_is_proved_by_a_fixture(self):
        """Delete each ``const``, one at a time: a bad fixture must notice."""
        for folder, unproved in self.unproved("const").items():
            self.assertEqual(unproved, [], f"{folder}: fixed values that no bad fixture proves")

    def test_index_and_fixture_folders_list_the_same_files(self):
        for folder, schemas in self.sets.items():
            listed = [entry["file"] for entry in client.load_index(folder)]
            self.assertEqual(len(listed), len(set(listed)), f"{folder}: a fixture is listed twice")
            self.assertEqual(set(listed), client.fixture_files(folder), f"{folder}: index and folders differ")

    def test_every_fixture_names_a_schema_that_exists(self):
        for folder, schemas in self.sets.items():
            for entry in client.load_index(folder):
                with self.subTest(fixture=entry["file"]):
                    schemas.resolve(entry["schema"])
                    # A fixture lives in the folder named after the schema file it is checked against.
                    self.assertEqual(entry["file"].split("/")[1] + ".json", entry["schema"].split("#")[0])

    def test_every_transcript_fixture_is_the_projection_of_its_events(self):
        """A good transcript line names, in the index, the event fixtures it is the projection of (the
        first event of the log, then its own event), and carries what those events say."""
        seen = 0
        for folder in self.sets:
            for entry in client.load_index(folder):
                if entry["schema"] != "transcript.line.v1.json" or not entry["file"].startswith("good/"):
                    continue
                with self.subTest(fixture=entry["file"]):
                    self.assertIn("of", entry, "a good transcript line names the events it is the projection of")
                    events = [client.read_json(folder / "fixtures" / name) for name in entry["of"]]
                    line = client.read_json(folder / "fixtures" / entry["file"])
                    wanted = client.rules.transcript_fields(events)[-1]
                    self.assertEqual({key: line.get(key) for key in wanted}, wanted)
                    seen += 1
        self.assertGreater(seen, 5, "too few transcript fixtures to mean anything")

    def test_no_fixture_is_checked_against_a_schema_that_refuses_nothing(self):
        """A good fixture proves nothing against a schema that takes every value (the root of a file that
        only holds definitions, say): no fixture may name such a target."""
        anything = (None, False, 0, "", [], {})
        for folder, schemas in self.sets.items():
            for target in sorted({entry["schema"] for entry in client.load_index(folder)}):
                with self.subTest(schema=target):
                    validator = schemas.validator(target)
                    self.assertTrue(any(client.leaf_errors(validator, value) for value in anything),
                                    f"{target} refuses nothing, so a fixture checked against it proves nothing")

    def test_every_record_schema_has_enough_fixtures(self):
        for folder, schemas in self.sets.items():
            counts = {}
            for entry in client.load_index(folder):
                kind = entry["file"].split("/", 1)[0]
                counts.setdefault(entry["schema"].split("#")[0], {"good": 0, "bad": 0})[kind] += 1
            for name in schemas.schemas:
                with self.subTest(schema=name):
                    have = counts.get(name, {"good": 0, "bad": 0})
                    self.assertGreaterEqual(have["good"], client.MIN_GOOD, f"{name}: good fixtures")
                    self.assertGreaterEqual(have["bad"], client.MIN_BAD, f"{name}: bad fixtures")

    def test_every_event_type_has_a_payload_schema_and_a_good_fixture(self):
        for folder, schemas in self.sets.items():
            event = schemas.schemas.get("event.v1.json")
            if event is None:
                continue
            types = event["properties"]["type"]["enum"]
            ruled = {rule["if"]["properties"]["type"]["const"]: rule["then"]["properties"]["payload"]["$ref"]
                     for rule in event["allOf"] if "type" in rule["if"].get("properties", {})}
            self.assertEqual(sorted(ruled), sorted(types), "each type has exactly one payload rule")
            for name, ref in ruled.items():
                if ref.startswith("#/$defs/"):
                    self.assertEqual(ref, f"#/$defs/{name}")
            shown = {client.read_json(folder / "fixtures" / entry["file"]).get("type")
                     for entry in client.load_index(folder)
                     if entry["schema"] == "event.v1.json" and entry["file"].startswith("good/")}
            self.assertEqual(sorted(set(types) - shown), [], "event types with no good fixture")

    def test_the_method_table_agrees_with_its_schema(self):
        """methods.table.v1.json is the table; methods.v1.json describes it and every method's params and answers."""
        for folder, schemas in self.sets.items():
            schema = schemas.schemas.get("methods.v1.json")
            if schema is None:
                continue
            table = client.read_json(folder / "methods.table.v1.json")
            errors = client.leaf_errors(schemas.validator("methods.v1.json"), table)
            self.assertEqual(errors, [], "the table does not validate against the schema's root")
            defs = schema["$defs"]
            names = [row["name"] for row in table["methods"]]
            self.assertEqual(len(names), len(set(names)), "a method has two rows")
            self.assertEqual(sorted(names), sorted(defs["method_name"]["enum"]), "rows and method names differ")
            self.assertEqual(table["table_version"], schema["x-version"])
            mutating_without_key = []
            for row in table["methods"]:
                name = row["name"]
                with self.subTest(method=name):
                    self.assertEqual(row["params"], f"#/$defs/params.{name}")
                    self.assertEqual(row["result"], f"#/$defs/answer.{name}")
                    params, answer = defs[f"params.{name}"], defs[f"answer.{name}"]
                    # Every code the row lists is one the answer schema allows, and the other way round: the
                    # codes the method's own answer types, and the bare refusals it may give.
                    refusals = answer["anyOf"][1]["allOf"][1]["properties"]["code"]["enum"]
                    own = answer["anyOf"][0]
                    if "$ref" in own:
                        own = schemas.resolve(own["$ref"], base="methods.v1.json")
                    code = own["properties"]["code"]
                    typed = code["enum"] if "enum" in code else [code["const"]]
                    self.assertEqual(sorted(row["codes"]), sorted(typed + refusals))
                    self.assertLessEqual(set(refusals), set(defs["refusal"]["properties"]["code"]["enum"]))
                    # A code the method's own answer types is never also a bare refusal: if it were, an
                    # answer could carry the code and skip every field the typed answer requires.
                    self.assertEqual(sorted(set(typed) & set(refusals)), [], "a typed code is also a bare refusal")
                    # 'unsupported' names not_implemented or a flag the row requires, and the row requires
                    # exactly the flags the answer can name. store_absent (no event store to keep a receipt
                    # in) can be named only by a method that has a receipt: one keyed by command_id.
                    limits = answer["anyOf"][1]["allOf"][1]
                    if "unsupported" in refusals:
                        needs_receipt = ["store_absent"] if row["idempotency"] == "command_id" else []
                        self.assertEqual(limits["then"]["properties"]["detail"]["enum"],
                                         ["not_implemented"] + needs_receipt + row["requires"])
                    else:
                        self.assertEqual(row["requires"], [], "a row requires a flag and cannot answer 'unsupported'")
                    # The seven mandatory built-ins are exactly the rows that cannot answer 'unsupported'.
                    self.assertEqual("unsupported" not in refusals, name in MANDATORY)
                    # Every method can be refused by a daemon that is closing. One that waits on no service of
                    # its own says 'unavailable' for that and for nothing a service would say.
                    self.assertIn("unavailable", row["codes"])
                    pins = [(pin["if"], pin["then"]["properties"]["detail"]) for pin in limits["allOf"]]
                    unavailable = {"required": ["code"], "properties": {"code": {"const": "unavailable"}}}
                    if name in UNAVAILABLE:
                        self.assertEqual(pins, [(unavailable, {"enum": UNAVAILABLE[name]})])
                    else:
                        # service_starting is the readiness of the service a row names. A row that names none can
                        # never say it; what else its 'unavailable' may come to say is not guessed here.
                        self.assertIsNone(row["service"], "a row names a service: say here what its 'unavailable' may be")
                        self.assertEqual(pins, [(unavailable, {"not": {"const": "service_starting"}})])
                    self.assertNotIn("service_starting", UNAVAILABLE.get(name, ()))
                    # What a row says of fallbacks and of its service is data nothing else checks: pinned here.
                    self.assertEqual(row["falls_back"], FALLS_BACK.get(name, []))
                    self.assertIn(row["service"], SERVICES)
                    # What the row says about keys and revisions is what the params schema requires.
                    required = set(params.get("required", ()))
                    self.assertEqual("command_id" in required, row["idempotency"] == "command_id")
                    self.assertEqual(row["precondition"] in required, row["precondition"] != "none")
                    self.assertNotIn("principal", params.get("properties", {}))
                    self.assertNotIn("owner", params.get("properties", {}))
                    if row["mutating"] and row["idempotency"] == "none":
                        mutating_without_key.append(name)
                    if row["mcp"]["exposed"]:
                        self.assertEqual(row["mcp"]["tool"], "recursion_" + name.replace(".", "_"))
            self.assertEqual(mutating_without_key, ["run.stop"], "only an emergency Stop may mutate without a key")
            requests = {client.read_json(folder / "fixtures" / entry["file"])["method"]
                        for entry in client.load_index(folder)
                        if entry["schema"].startswith("methods.v1.json#/$defs/") and entry["file"].startswith("good/")
                        and entry["schema"].split("/")[-1] in ("request", "exchange")}
            self.assertEqual(sorted(set(names) - requests), [], "methods with no good request or exchange fixture")

    def test_the_public_run_view_carries_every_field_of_the_stored_record(self):
        for schemas in self.sets.values():
            record, view = schemas.schemas.get("run.record.v1.json"), schemas.schemas.get("run.view.v1.json")
            if record is None or view is None:
                continue
            self.assertLessEqual(set(record["properties"]), set(view["properties"]))
            self.assertLessEqual(set(record["required"]), set(view["required"]))

    def test_this_client_imports_nothing_from_the_product(self):
        """Nothing here imports the product, and the rules import the standard library alone."""
        allowed = {"jsonschema", "referencing", "wire_client", "rules"} | set(sys.stdlib_module_names)
        for path in sorted(HERE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for name in names:
                    self.assertIn(name.split(".")[0], allowed, f"{path.name} imports {name}")
                    if path.name == "rules.py":
                        self.assertIn(name.split(".")[0], sys.stdlib_module_names, f"rules.py imports {name}")

    def test_vector_index_and_vector_folders_list_the_same_files(self):
        for folder in self.sets:
            listed = [entry["file"] for entry in client.load_vectors(folder)]
            self.assertEqual(len(listed), len(set(listed)), f"{folder}: a vector is listed twice")
            self.assertEqual(set(listed), client.vector_files(folder), f"{folder}: vector index and folders differ")

    def test_every_rule_has_a_vector_that_agrees_and_one_that_is_caught(self):
        """A rule with only agreeing vectors proves nothing: each rule also has a vector it must catch."""
        seen = {}
        for folder in self.sets:
            for entry in client.load_vectors(folder):
                vector = client.read_json(folder / "vectors" / entry["file"])
                expect = vector.get("expect")
                if entry["rule"] == "call.profile":
                    kinds = {"caught" if "refused" in expect else "agrees"}
                elif entry["rule"] in ("approval.binding", "transcript.projection", "task.derivation", "event.page"):
                    kinds = {"caught" if expect else "agrees"}
                elif entry["rule"] == "start.intent":
                    kinds = {"caught" if expect.get("retry") == "client_key_reused" else "agrees"}
                elif entry["rule"] == "event.cursor":
                    kinds = {"agrees" if expect["code"] == "ok" else "caught"}
                elif entry["rule"] == "queue.delivery":
                    kinds = ({"agrees"} if any("item" in step for step in vector["steps"]) else set()) \
                        | ({"caught"} if any("refused" in step for step in vector["steps"]) else set())
                else:
                    codes = {step["answer"]["code"] if "answer" in step else step["result"] for step in vector["steps"]}
                    kinds = ({"agrees"} if "ok" in codes else set()) | ({"caught"} if codes - {"ok"} else set())
                seen.setdefault(entry["rule"], set()).update(kinds)
        self.assertEqual({rule: sorted(kinds) for rule, kinds in seen.items()},
                         {rule: ["agrees", "caught"] for rule in client.VECTOR_RULES})

    def test_a_queue_item_is_delivered_only_by_an_acceptance_that_agrees_with_its_binding(self):
        """In a queue.delivery vector, take each step where an acceptance delivers the item, and change the
        acceptance: no event at all, the core's own write in its place, another turn, an envelope filed
        under another turn, another log, another run, the other 'how', another delivery. Then write the
        delivery a second time just before it. Each time the vector is refused: what the step claims is no
        longer what the rule gives from the binding that was committed before the write."""
        changed = 0
        for folder, schemas in self.sets.items():
            for entry in client.load_vectors(folder):
                if entry["rule"] != "queue.delivery":
                    continue
                vector = client.read_json(folder / "vectors" / entry["file"])
                self.assertIsNone(client.judge_vector(schemas, "queue.delivery", vector), entry["file"])
                for number, step in enumerate(vector["steps"]):
                    if step["happening"] != "accepted" or "item" not in step:
                        continue
                    event = step["event"]
                    payload = event["payload"]
                    other_how = "new_turn" if payload["how"] == "mid_turn" else "mid_turn"
                    variants = {
                        "no event": None,
                        "the core's write": dict(event, type="steer.sent", source="core", trust="trusted",
                                                 payload={"item_id": vector["item"]["item_id"], "delivery_id": payload["delivery_id"]}),
                        "another turn": dict(event, turn_id="turn-ffff", payload=dict(payload, turn_id="turn-ffff")),
                        "an envelope of another turn": dict(event, turn_id="turn-ffff"),
                        "another log": dict(event, session_id="ses-ffffffffffffffff"),
                        "another run": dict(event, run_id="run-" + "f" * 32),
                        "the other how": dict(event, payload=dict(payload, how=other_how)),
                        "another delivery": dict(event, payload=dict(payload, delivery_id="dlv-ffff")),
                    }
                    for what, replaced in variants.items():
                        doctored = copy.deepcopy(vector)
                        if replaced is None:
                            del doctored["steps"][number]["event"]
                        else:
                            doctored["steps"][number]["event"] = replaced
                        with self.subTest(vector=entry["file"], step=number + 1, change=what):
                            self.assertIsNotNone(client.judge_vector(schemas, "queue.delivery", doctored))
                        changed += 1
                    writes = [index for index, earlier in enumerate(vector["steps"][:number])
                              if earlier["happening"] == "written" and "item" in earlier]
                    if writes:
                        doctored = copy.deepcopy(vector)
                        doctored["steps"].insert(number, copy.deepcopy(vector["steps"][writes[-1]]))
                        with self.subTest(vector=entry["file"], step=number + 1, change="a second write"):
                            self.assertIsNotNone(client.judge_vector(schemas, "queue.delivery", doctored))
                        changed += 1
        self.assertGreater(changed, 20, "too few acceptance steps to mean anything")

    def test_every_page_in_a_good_exchange_continues_its_cursor(self):
        """A good exchange of events.read or events.follow that answers a page: the page is in the projection
        the request asked for (raw when it names none) and is the continuation of the cursor it was asked
        with. (Each side validates alone; this is the join.) The same exchange asked in the OTHER projection
        still fits the schema, and is caught here."""
        found, projections, unnamed = 0, set(), 0
        for folder, schemas in self.sets.items():
            for entry in client.load_index(folder):
                if not entry["file"].startswith("good/"):
                    continue
                value = client.read_json(folder / "fixtures" / entry["file"])
                if not (isinstance(value, dict) and value.get("method") in ("events.read", "events.follow")):
                    continue
                answer = value.get("answer")
                if isinstance(answer, dict) and answer.get("code") == "ok":
                    found += 1
                    projections.add(answer["projection"])
                    unnamed += "projection" not in value["params"]
                    with self.subTest(fixture=entry["file"]):
                        self.assertEqual(client.rules.page_problems(value["params"], answer), [])
                        other = "transcript" if answer["projection"] == "raw" else "raw"
                        asked_otherwise = dict(value, params=dict(value["params"], projection=other))
                        self.assertEqual(client.leaf_errors(schemas.validator("methods.v1.json#/$defs/exchange"), asked_otherwise), [],
                                         "the schema alone does not join the request's projection to the answer's")
                        self.assertEqual(client.rules.page_problems(asked_otherwise["params"], answer), ["projection"])
        self.assertGreaterEqual(found, 4, "too few pages to mean anything")
        self.assertEqual(projections, {"raw", "transcript"}, "a good page in each projection")
        self.assertGreaterEqual(unnamed, 1, "a good page for a request that names no projection: the default")

    def test_every_event_in_a_good_file_carries_real_digests_and_real_spans(self):
        """Wherever a good fixture or a vector holds an event: a state.changed carries the digest of its
        post-image, a tool.call the digest of its input. And in the good fixtures a replay.gap is a span
        (from at most to, filed under to); the vectors hold broken spans on purpose, each caught by name."""
        found = {"state.changed": 0, "tool.call": 0, "replay.gap": 0}
        for folder, schemas in self.sets.items():
            good = [(folder / "fixtures" / entry["file"], True) for entry in client.load_index(folder)
                    if entry["file"].startswith("good/")]
            vectors = [(folder / "vectors" / entry["file"], False) for entry in client.load_vectors(folder)]
            for path, fixture in good + vectors:
                for event in client.events_in(client.read_json(path)):
                    kind, payload = event["type"], event["payload"]
                    with self.subTest(file=path.name, seq=event["seq"], type=kind):
                        if kind == "state.changed" and "post_image" in payload:
                            found[kind] += 1
                            self.assertEqual(payload.get("post_image_sha256"), client.rules.post_image_digest(payload["post_image"]))
                        elif kind == "tool.call" and payload.get("input") is not None:
                            found[kind] += 1
                            self.assertEqual(payload.get("input_digest"), client.rules.input_digest(payload["input"]))
                        elif kind == "replay.gap" and fixture:
                            found[kind] += 1
                            self.assertFalse(client.rules.gap_problem(event))
        self.assertGreater(found["state.changed"], 20)
        self.assertGreater(found["tool.call"], 0)
        self.assertGreater(found["replay.gap"], 0)

    def test_a_task_is_derived_for_every_state_of_each_source(self):
        """A Task comes from a run or from a turn. For every state a run's view can show, and for every state
        a turn can be in, there is a vector whose Task agrees with the rule; and every status a Task can
        have is reached from each source that can reach it."""
        states, statuses = {"run": set(), "turn": set()}, {"run": set(), "turn": set()}
        control = None
        for folder, schemas in self.sets.items():
            for entry in client.load_vectors(folder):
                if entry["rule"] != "task.derivation":
                    continue
                control = schemas
                vector = client.read_json(folder / "vectors" / entry["file"])
                if vector["expect"]:
                    continue
                source = vector["source"]
                statuses[source].add(vector["task"]["status"])
                if source == "run":
                    states[source].add(vector["view"]["state"])
                else:
                    states[source].add(next(turn["state"] for turn in vector["session"]["turns"]
                                            if turn["turn_id"] == vector["turn_id"]))
        self.assertIsNotNone(control, "no task.derivation vector")
        self.assertEqual(sorted(states["run"]), sorted(control.schemas["run.view.v1.json"]["$defs"]["public_state"]["enum"]))
        self.assertEqual(sorted(states["turn"]), sorted(control.schemas["session.v1.json"]["$defs"]["turn_state"]["enum"]))
        every = sorted(control.schemas["mcp.task.v1.json"]["properties"]["status"]["enum"])
        self.assertEqual(sorted(statuses["run"]), every)
        self.assertEqual(sorted(statuses["turn"]), every)

    def test_every_approval_in_a_good_fixture_shows_what_it_binds(self):
        """Wherever a good fixture holds an approval (on its own, in an event, in an answer), its digest is
        the digest of its effect and its lines are the lines of its effect."""
        found = 0
        for folder, schemas in self.sets.items():
            for entry in client.load_index(folder):
                if not entry["file"].startswith("good/"):
                    continue
                for record in client.approvals_in(client.read_json(folder / "fixtures" / entry["file"])):
                    found += 1
                    with self.subTest(fixture=entry["file"]):
                        self.assertEqual(client.rules.approval_problems(record), [])
        self.assertGreater(found, 10, "the walk found too few approvals to mean anything")


class RulesTest(unittest.TestCase):
    """What a vector file cannot hold: the host's own state around a rule, a record changed behind a model's
    back, a request that the method table would not let into an exchange. The rules are ``rules.py``'s."""

    OWNER = {"kind": "owner", "id": "local"}

    @classmethod
    def setUpClass(cls):
        cls.rules = client.rules
        cls.folder = next(folder for folder in client.folders() if (folder / "vectors" / "approval.lifecycle").is_dir())
        cls.schemas = client.SchemaSet(cls.folder)

    def vector(self, name):
        return client.read_json(next((self.folder / "vectors").glob(name)))

    def approved(self, vector="approval.lifecycle/s07-*.json", **binding):
        """A model of that vector's request with its binding (changed as given), shown and approved."""
        found = self.vector(vector)
        kept = dict(found["binding"], **binding)
        model = self.rules.ApprovalModel(found["record"], kept)
        record = model.record
        opened = model.call(self.OWNER, "approval.open", {"command_id": "t.1", "approval_id": record["approval_id"], "surface": "tui"},
                            record["requested_at"])
        answered = model.call(self.OWNER, "approval.answer",
                              {"command_id": "t.2", "approval_id": record["approval_id"], "surface": "tui",
                               "expected_revision": record["revision"], "digest": record["digest"], "answer": "approve"},
                              record["requested_at"])
        self.assertEqual((opened["code"], answered["code"], model.record["state"]), ("ok", "ok", "approved"))
        return model, kept

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(self.rules.Refused) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    # ---- numbers
    NUMBERS = {"1e999999": "integer_out_of_range", "1e1000000": "integer_out_of_range", "-1e1000000": "integer_out_of_range",
               "1.5e1000000": "integer_out_of_range", "1e9999999999999999999": "integer_out_of_range",
               "9007199254740992": "integer_out_of_range", "9007199254740991.0": 9007199254740991, "-9007199254740991": -9007199254740991,
               "0e1000000": 0, "-0e1000000": 0, "1e-1000000": 0, "0e9999999999999999999": 0, "-0.0": 0, "1.0": 1, "1e2": 100,
               "1.5": 1.5, "1e400": "integer_out_of_range", "1.5e400": "integer_out_of_range", "0.1e-400": 0}

    def read(self, token):
        try:
            return self.rules.read_strict('{"x": %s}' % token)["x"]
        except self.rules.Refused as refusal:
            return refusal.code

    def test_a_number_is_admitted_the_same_whatever_the_hosts_decimal_context(self):
        """The exact range check does not depend on the host's Decimal context, and nothing but a typed
        refusal comes out of it: under a narrow precision, a small exponent range, every trap set and no trap
        set, each number is admitted or refused exactly as under the default context."""
        import decimal
        for token, wanted in self.NUMBERS.items():
            with self.subTest(number=token, context="default"):
                found = self.read(token)
                self.assertEqual((found, type(found)), (wanted, type(wanted)))
        hosts = {"one digit": decimal.Context(prec=1), "a small exponent range": decimal.Context(prec=5, Emax=9, Emin=-9),
                 "every trap set": decimal.Context(traps=list(decimal.Context().flags)),
                 "no trap set": decimal.Context(traps=[])}
        for name, context in hosts.items():
            with decimal.localcontext(context):
                for token, wanted in self.NUMBERS.items():
                    with self.subTest(number=token, context=name):
                        found = self.read(token)
                        self.assertEqual((found, type(found)), (wanted, type(wanted)))

    def test_a_held_number_that_is_not_in_its_bound_form_is_seen_and_put_right(self):
        """An integral float and minus zero are what ``unnormalised`` finds, at any depth, and what
        ``normalised`` turns into the whole number; nothing else is touched."""
        rules = self.rules
        for value in (1.0, -0.0, 0.0, [1, {"a": 2.0}], {"a": [{"b": -0.0}]}, (3.0,)):
            with self.subTest(value=value):
                self.assertTrue(rules.unnormalised(value))
                self.assertFalse(rules.unnormalised(rules.normalised(value)))
                self.assertEqual(rules.text_of(rules.normalised(value)), rules.text_of(value))
        kept = {"a": 1.5, "b": [0, -3, "1.0", None, True], "c": {"d": 1e-7}}
        self.assertFalse(rules.unnormalised(kept))
        self.assertEqual(client._exact(rules.normalised(kept)), client._exact(kept))
        self.assertEqual(client._exact(rules.normalised({"n": 1e20})), client._exact({"n": 1e20}), "outside the range: left for check_value")
        # And check_value refuses it for what it is, wherever a value is digested, not only in an approval: an
        # integral value outside the range, whatever type holds it.
        for held in (1e20, -1e20, float(2**53), 9007199254740993):
            with self.subTest(held=held):
                self.refused("integer_out_of_range", rules.check_value, {"n": [held]})
                self.refused("integer_out_of_range", rules.post_image_digest, {"state": "running", "n": held})
                self.refused("integer_out_of_range", rules.input_digest, {"n": held})
                self.refused("integer_out_of_range", rules.start_intent_text, {"prompt": "p", "budget": held}, ["budget"])
        for held in (float("inf"), float("nan")):
            self.refused("number_out_of_range", rules.check_value, held)
        rules.check_value({"n": [float(2**53 - 1), -9007199254740991, 1.5, 1.5e-300]})

    def test_a_bound_call_that_holds_a_number_not_in_its_bound_form_is_refused_everywhere(self):
        """A call that holds 1.0 or minus zero writes the same text as the normalised call, so its digest and
        its lines would be the same. It is refused as a call, it has no bytes to digest, and no lines."""
        rules = self.rules
        record = self.vector("approval.binding/g01-*.json")["record"]
        self.assertEqual(rules.approval_problems(record), [])
        for spelled in (1.0, -0.0):
            held = copy.deepcopy(record)
            held["effect"]["input"]["count"] = spelled
            whole = copy.deepcopy(record)
            whole["effect"]["input"]["count"] = int(spelled)
            with self.subTest(number=spelled):
                self.assertEqual(rules.text_of(held["effect"]), rules.text_of(whole["effect"]), "one text for both")
                self.refused("not_normalised", rules.check_call, rules.call_in(held["effect"]))
                self.refused("not_normalised", rules.effect_bytes, "rfc8785/v1", held["effect"])
                self.refused("not_normalised", rules.approval_lines, "engine_tool", held["effect"])
                self.assertEqual(rules.approval_problems(held), ["call", "digest", "rendered"])

    def test_an_effect_has_exactly_the_members_of_its_source(self):
        """A member more in an effect is bound by the digest and drawn on no line: the owner would approve
        something that was never shown. With the digest made again over it, nothing else in the record
        disagrees; the rule names the effect. So for a member more in a plugin effect's scope, and for a member
        less. A change to such an effect is refused and leaves the request as it was."""
        rules = self.rules
        call = self.vector("approval.binding/g01-*.json")["record"]
        order = self.vector("approval.binding/g03-*.json")["record"]
        self.assertEqual((rules.approval_problems(call), rules.approval_problems(order)), ([], []))

        def with_effect(record, effect):
            return dict(record, effect=effect, digest=rules.digest_of(record["source"], effect))

        more = with_effect(call, dict(call["effect"], note="also: delete the backups"))
        self.assertEqual(rules.approval_lines("engine_tool", more["effect"]), call["rendered"], "the member is on no line")
        self.assertEqual(rules.approval_problems(more), ["effect"])
        plugin_more = with_effect(order, dict(order["effect"], beneficiary="another account"))
        self.assertEqual(rules.approval_lines("plugin_effect", plugin_more["effect"]), order["rendered"])
        self.assertEqual(rules.approval_problems(plugin_more), ["effect"])
        scope_more = with_effect(order, dict(order["effect"], scope=dict(order["effect"]["scope"], sub_account="hidden")))
        self.assertEqual(rules.approval_lines("plugin_effect", scope_more["effect"]), order["rendered"])
        self.assertEqual(rules.approval_problems(scope_more), ["effect"])
        less = dict(call, effect={key: value for key, value in call["effect"].items() if key != "engine"})
        self.assertIn("effect", rules.approval_problems(less))
        self.assertIn("effect", rules.approval_problems(dict(order, effect=dict(order["effect"], scope="paper"))))
        self.assertIn("effect", rules.approval_problems(dict(call, effect="rm -rf /")))
        for record, effect in ((call, more["effect"]), (order, plugin_more["effect"]), (order, scope_more["effect"])):
            model = rules.ApprovalModel(record)
            before = client._exact(model.record)
            self.refused("effect", model.change, effect=effect)
            self.assertEqual(client._exact(model.record), before)

    def test_what_is_carried_out_is_the_bound_form_and_a_record_that_agrees_with_itself(self):
        """A model is made from a record: the numbers of its call are normalised as it is made. A record that
        is changed behind the model afterwards (minus zero put back, another effect, another run on the
        envelope) is never carried out: consumption checks the whole record, and refuses with nothing changed."""
        rules = self.rules
        durable = self.vector("approval.lifecycle/s11-*.json")
        self.assertTrue(rules.unnormalised(durable["record"]["effect"]), "the vector's record holds 1.0 and -0.0")
        model = rules.ApprovalModel(durable["record"], durable["binding"])
        self.assertFalse(rules.unnormalised(model.record["effect"]))
        self.assertEqual(rules.approval_problems(model.record), [])

        def spoil_number(record):
            record["effect"]["input"]["count"] = -0.0

        def spoil_effect(record):
            record["effect"]["input"]["command"] = "rm -rf /"

        def spoil_envelope(record):
            record["run_id"] = "run-" + "f" * 32

        def spoil_lines(record):
            record["rendered"] = record["rendered"][:-1]

        for spoil in (spoil_number, spoil_effect, spoil_envelope, spoil_lines):
            model, kept = self.approved()
            spoil(model.record)
            before = client._exact(model.record)
            with self.subTest(change=spoil.__name__):
                self.refused("record_invalid", model.consume, model.record["revision"], model.record["digest"], kept)
                self.assertEqual(client._exact(model.record), before)
                self.assertEqual(model.record["state"], "approved")
        model, kept = self.approved()
        model.consume(model.record["revision"], model.record["digest"], kept)
        self.assertEqual(model.record["state"], "consumed")

    # ---- the private binding
    def test_each_member_of_the_binding_is_compared_with_the_approval_it_is_used_against(self):
        """The binding that is KEPT names another approval, another run or another engine, and the same
        binding is offered: equal to what is kept, and still not this approval's. Each is refused on its own,
        with the approval still approved; so is a binding with the member missing; and so after a restart."""
        rules = self.rules
        for member, other in (("approval_id", "apr-0fff"), ("run_id", "run-" + "0" * 32), ("engine", "codex")):
            for value in (other, None, ...):
                model, kept = self.approved(**{member: value})
                if value is ...:
                    del kept[member], model.binding[member]
                with self.subTest(member=member, value=value):
                    self.assertEqual(model.binding, kept)
                    self.refused("binding_mismatch", model.consume, 1, model.record["digest"], kept)
                    again = rules.ApprovalModel(model.record, model.binding, model.receipts)
                    self.refused("binding_mismatch", again.consume, 1, again.record["digest"], kept)
                    self.assertEqual((model.record["state"], again.record["state"]), ("approved", "approved"))
        # No binding kept and none offered, for an engine's call: refused, with a refusal and not an error.
        model, kept = self.approved()
        model.binding = None
        self.refused("binding_mismatch", model.consume, 1, model.record["digest"], None)
        self.refused("binding_mismatch", model.consume, 1, model.record["digest"], kept)
        self.assertEqual(model.record["state"], "approved")
        # A whole, self-consistent record of ANOTHER run (its envelope, its effect, its lines and its digest all
        # agree) is not the approval this binding was kept for.
        model, kept = self.approved()
        moved = copy.deepcopy(model.record)
        moved["run_id"] = moved["effect"]["run_id"] = "run-" + "0" * 32
        moved.update(digest=rules.digest_of("engine_tool", moved["effect"]), rendered=rules.approval_lines("engine_tool", moved["effect"]))
        self.assertEqual(rules.approval_problems(moved), [])
        elsewhere = rules.ApprovalModel(moved, kept)
        self.refused("binding_mismatch", elsewhere.consume, 1, moved["digest"], kept)
        self.assertEqual(elsewhere.record["state"], "approved")

    def test_a_plugin_effect_is_carried_out_with_no_binding_and_never_with_one(self):
        found = self.vector("approval.lifecycle/s02-*.json")
        engine_binding = self.vector("approval.lifecycle/s07-*.json")["binding"]
        approved = found["steps"][1]["record"]
        self.assertEqual((approved["source"], approved["state"]), ("plugin_effect", "approved"))
        for kept, offered in ((engine_binding, engine_binding), (engine_binding, None), (None, engine_binding)):
            model = self.rules.ApprovalModel(approved, kept)
            with self.subTest(kept=kept is not None, offered=offered is not None):
                self.refused("binding_mismatch", model.consume, approved["revision"], approved["digest"], offered)
                self.assertEqual(model.record["state"], "approved")
        model = self.rules.ApprovalModel(approved)
        model.consume(approved["revision"], approved["digest"], None)
        self.assertEqual(model.record["state"], "consumed")

    # ---- a change, and a call
    def test_a_refused_change_leaves_the_request_its_revision_and_its_binding_as_they_were(self):
        """Every way a change can be refused, against one pending request: the record, its revision and the
        binding are the same objects' same content afterwards, and no host exception comes out."""
        rules = self.rules
        found = self.vector("approval.lifecycle/s15-*.json")
        effect = found["record"]["effect"]
        plugin = self.vector("approval.lifecycle/s02-*.json")["record"]["effect"]
        too_many = dict(effect, input={"f%03d" % n: n for n in range(194)})
        changes = {"nothing_to_change": {}, "effect": {"effect": "rm -rf /"}, "timestamp": {"expires_at": "2026-11-13T07:49:60.000Z"},
                   "too_many_lines": {"effect": too_many}}
        targets = [dict(effect, run_id="run-" + "0" * 32), dict(effect, session_id="ses-" + "0" * 16), dict(effect, session_id=None),
                   dict(effect, engine="codex"), dict(effect, kind="plugin_effect"), plugin,
                   {key: value for key, value in effect.items() if key != "run_id"}]
        cases = list(changes.items()) + [("target", {"effect": target}) for target in targets] \
            + [("effect", {"effect": {key: value for key, value in effect.items() if key != "cwd"}}),
               # A good new effect and a bad deadline in ONE change: the effect must not have been written first.
               ("timestamp", {"effect": dict(effect, input={"command": "make"}), "expires_at": "tomorrow"})]
        for code, change in cases:
            model = rules.ApprovalModel(found["record"], found["binding"])
            before = (client._exact(model.record), client._exact(model.binding))
            with self.subTest(refusal=code, change=str(change)[:60]):
                self.refused(code, model.change, **change)
                self.assertEqual((client._exact(model.record), client._exact(model.binding)), before)
                self.assertEqual(model.record["revision"], 1)
        # The WHOLE candidate is checked, not only what the change names: a request whose envelope no longer
        # agrees with its effect cannot be changed at all, not even its deadline.
        model = rules.ApprovalModel(found["record"], found["binding"])
        model.record["run_id"] = "run-" + "0" * 32
        before = client._exact(model.record)
        self.refused("envelope", model.change, expires_at="2026-11-13T08:10:00.000Z")
        self.assertEqual(client._exact(model.record), before)
        model = rules.ApprovalModel(found["record"], found["binding"])
        model.record["state"] = "approved"
        self.refused("resolved", model.change, expires_at="2026-11-13T08:10:00.000Z")

    def test_a_change_binds_the_normalised_numbers_of_its_new_effect(self):
        rules = self.rules
        found = self.vector("approval.lifecycle/s15-*.json")
        model = rules.ApprovalModel(found["record"], found["binding"])
        spelled = copy.deepcopy(found["record"]["effect"])
        spelled["input"].update(count=2.0, offset=-0.0)
        model.change(effect=spelled)
        self.assertEqual(client._exact(model.record["effect"]["input"]["count"]), "2")
        self.assertEqual(client._exact(model.record["effect"]["input"]["offset"]), "0")
        self.assertEqual((model.record["revision"], rules.approval_problems(model.record)), (2, []))
        self.assertEqual(client._exact(spelled["input"]["count"]), "2.0", "the caller's own value is not touched")

    def test_an_approval_call_that_is_not_whole_is_refused_and_keeps_no_receipt(self):
        """A request the method table would not let into an exchange. An answer is 'approve' or 'deny': no
        other word, and no missing word, is ever read as a denial. Nothing is stored and nothing changes."""
        rules = self.rules
        found = self.vector("approval.lifecycle/s01-*.json")
        record = found["record"]
        good = {"command_id": "t.9", "approval_id": record["approval_id"], "surface": "tui", "expected_revision": 1,
                "digest": record["digest"], "answer": "approve"}
        model = rules.ApprovalModel(record)
        at = record["requested_at"]
        model.call(self.OWNER, "approval.open", {"command_id": "t.1", "approval_id": record["approval_id"], "surface": "tui"}, at)
        before, receipts = client._exact(model.record), dict(model.receipts)
        spoiled = [("answer", dict(good, answer="veto")), ("answer", dict(good, answer=None)), ("answer", dict(good, answer="Approve")),
                   ("expected_revision", dict(good, expected_revision="1")), ("expected_revision", dict(good, expected_revision=True)),
                   ("expected_revision", dict(good, expected_revision=0)), ("digest", dict(good, digest=None)),
                   ("surface", dict(good, surface="")), ("command_id", dict(good, command_id=None))]
        spoiled += [(field, {key: value for key, value in good.items() if key != field}) for field in good]
        for field, params in spoiled:
            with self.subTest(field=field, params=str(params)[:70]):
                self.assertEqual(model.call(self.OWNER, "approval.answer", params, at), {"code": "request_invalid", "detail": field})
                self.assertEqual((client._exact(model.record), model.receipts), (before, receipts))
        for principal in ({"kind": "owner"}, {"kind": "session", "id": "ses-1"}, {"id": "local"}, None, {"kind": "device", "id": ""}):
            with self.subTest(principal=principal):
                self.assertEqual(model.call(principal, "approval.answer", good, at), {"code": "scope_refused", "detail": "approve"})
                self.assertEqual((client._exact(model.record), model.receipts), (before, receipts))
        self.refused("method", model.call, self.OWNER, "approval.veto", good, at)
        self.refused("timestamp", model.call, self.OWNER, "approval.answer", good, "2026-11-13T07:34:60.000Z")
        self.assertEqual((client._exact(model.record), model.receipts), (before, receipts))
        self.assertEqual(model.call(self.OWNER, "approval.answer", good, at)["code"], "ok")
        self.assertEqual(model.record["state"], "approved")

    # ---- a timestamp
    def test_a_timestamp_is_a_real_instant_with_seconds_below_sixty(self):
        """The check stands without the pattern: a :60 is refused in an ordinary minute and at a real leap
        second alike and is never read as the next minute; so are an hour of 24, a minute of 60, a day that
        does not exist, and any text that is not exactly the declared spelling."""
        rules = self.rules
        self.assertEqual(rules.milliseconds("1970-01-01T00:00:00.000Z"), 0)
        self.assertEqual(rules.milliseconds("2026-11-13T12:34:59.999Z") + 1, rules.milliseconds("2026-11-13T12:35:00.000Z"))
        self.assertEqual(rules.milliseconds("2024-02-29T23:59:59.999Z") + 1, rules.milliseconds("2024-03-01T00:00:00.000Z"))
        wrong = ["2026-11-13T12:34:60.000Z", "2016-12-31T23:59:60.000Z", "2026-11-13T24:00:00.000Z", "2026-11-13T12:60:00.000Z",
                 "2026-02-31T07:34:00.000Z", "2025-02-29T00:00:00.000Z", "2026-13-01T00:00:00.000Z", "2026-11-13T12:34:56Z",
                 "2026-11-13T12:34:56.000", "2026-11-13 12:34:56.000Z", "2026-11-13T12:34:56.0000Z", " 2026-11-13T12:34:56.00Z",
                 "2026-11-13T+2:34:56.000Z", "2026-11-13T12:34:5\u0661.000Z", "2026-11-13T12:34:56.000Z\n", "", None, 0]
        for text in wrong:
            with self.subTest(timestamp=text):
                self.refused("timestamp", rules.milliseconds, text)
        timestamp = self.schemas.validator("common.v1.json#/$defs/timestamp")
        for text in ("2026-11-13T12:34:60.000Z", "2016-12-31T23:59:60.000Z"):
            keywords = {keyword for keyword, _, _ in client.leaf_errors(timestamp, text)}
            self.assertLessEqual({"pattern", "x-instant"}, keywords, "refused by the pattern and by the real-instant check, each on its own")

    # ---- the delivery of a queue item
    def test_a_refused_delivery_step_changes_nothing_and_raises_nothing_but_a_refusal(self):
        rules = self.rules
        found = self.vector("queue.delivery/d12-*.json")
        binding = found["steps"][0]["facts"]
        for state_after, steps in (("queued", []), ("delivering", [("begin", binding)]),
                                   ("outcome_unknown", [("begin", binding), ("restart", None)])):
            for happening, facts in (("written", None), ("written", {}), ("accepted", None), ("accepted", {}), ("turn_ended", None),
                                     ("turn_ended", {}), ("turn_ended", {"turn_id": None}), ("turn_ended", {"turn_id": 7}),
                                     ("begin", None), ("begin", {}), ("begin", dict(binding, how=["new_turn"])),
                                     ("edit", None), ("edit", {"text": 7}), ("edit", {"delivery": ["steer"]}), ("edit", {"other": 1}),
                                     ("deliver", None)):
                model = rules.DeliveryModel(found["item"])
                for step in steps:
                    model.step(*step)
                before = client._exact([model.item, model.binding, model.written])
                with self.subTest(state=state_after, happening=happening, facts=facts):
                    self.assertEqual(model.item["state"], state_after)
                    with self.assertRaises(rules.Refused):
                        model.step(happening, facts)
                    self.assertEqual(client._exact([model.item, model.binding, model.written]), before)

    def test_the_end_of_the_target_turn_quarantines_either_kind_of_delivery(self):
        """For each 'how' a binding can say: the end of another turn changes nothing, the end of the target
        turn makes the item outcome_unknown, and an acceptance after that delivers nothing and is kept."""
        rules = self.rules
        found = self.vector("queue.delivery/d05-*.json")
        steer, bound = found["item"], found["steps"][0]["facts"]
        self.assertEqual((steer["delivery"], bound["how"]), ("steer", "mid_turn"))
        for how in ("mid_turn", "new_turn"):
            binding = dict(bound, how=how)
            accepted = {"delivery_id": binding["delivery_id"], "session_id": binding["session_id"], "run_id": binding["run_id"],
                        "turn_id": binding["target_turn_id"], "how": how}
            with self.subTest(how=how):
                model = rules.DeliveryModel(steer)
                model.step("begin", binding)
                model.step("written", {"delivery_id": binding["delivery_id"]})
                model.step("turn_ended", {"turn_id": "turn-ffff"})
                self.assertEqual(model.item["state"], "delivering")
                model.step("turn_ended", {"turn_id": binding["target_turn_id"]})
                self.assertEqual(model.item["state"], "outcome_unknown")
                self.refused("not_delivering", model.step, "accepted", accepted)
                self.assertEqual((model.item["state"], model.item["turn_id"], model.evidence), ("outcome_unknown", None, [accepted]))


class FixtureTest(unittest.TestCase):
    """One test per fixture; the methods are added below from the index."""

    sets: dict = {}

    @classmethod
    def schemas_for(cls, folder):
        if folder not in cls.sets:
            cls.sets[folder] = client.SchemaSet(folder)
        return cls.sets[folder]


def _add_fixture_tests():
    for folder in client.folders():
        area = folder.relative_to(client.REPO).parts[1]
        for entry in client.load_index(folder):
            def test(self, folder=folder, entry=entry):
                problem = client.check_entry(self.schemas_for(folder), entry)
                self.assertIsNone(problem, f"{entry['file']}: {problem}")

            name = "test_" + re.sub(r"[^a-z0-9]+", "_", f"{area}_{entry['file'][:-5]}".lower())
            if hasattr(FixtureTest, name):
                raise client.ConformanceError(f"two fixtures map to {name}")
            test.__doc__ = entry.get("shows") or entry.get("rule")
            setattr(FixtureTest, name, test)


_add_fixture_tests()


class VectorTest(unittest.TestCase):
    """One test per vector; the methods are added below from the vector index."""

    sets: dict = {}

    @classmethod
    def schemas_for(cls, folder):
        if folder not in cls.sets:
            cls.sets[folder] = client.SchemaSet(folder)
        return cls.sets[folder]


def _add_vector_tests():
    for folder in client.folders():
        area = folder.relative_to(client.REPO).parts[1]
        for entry in client.load_vectors(folder):
            def test(self, folder=folder, entry=entry):
                problem = client.check_vector(self.schemas_for(folder), entry)
                self.assertIsNone(problem, f"{entry['file']}: {problem}")

            name = "test_" + re.sub(r"[^a-z0-9]+", "_", f"{area}_{entry['file'][:-5]}".lower())
            if hasattr(VectorTest, name):
                raise client.ConformanceError(f"two vectors map to {name}")
            test.__doc__ = entry["shows"]
            setattr(VectorTest, name, test)


_add_vector_tests()

if __name__ == "__main__":
    unittest.main()
