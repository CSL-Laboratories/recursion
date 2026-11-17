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
from unittest import mock

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
# The service each row names; every row that is not here names none. A service is named by the package that holds
# its lane's registrant. Naming one more is a new draft of the table, and a line here.
SERVICE_OF = {"events.read": "events", "events.follow": "events"}


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

    def test_every_timestamp_in_every_folder_is_a_real_instant_with_seconds_below_sixty(self):
        """A definition whose pattern takes a timestamp is a timestamp, in whichever folder of schemas it stands:
        it carries ``x-instant``, so that the client's validator refuses a day that does not exist, and its
        pattern refuses a second of 60. A pattern is taken for a timestamp's when it accepts a timestamp and
        refuses the same shape spelt in letters. Until Nov 15, 2026 the revoked-key list's own definition had
        neither, and a list that expired on the 31st of February fitted (a reader of the pull request showed
        it); the client's check of the keyword was never asked there."""
        a_time, same_shape, sixty = "2026-11-13T12:34:56.000Z", "abcd-ef-ghTij:kl:mn.opqZ", "2026-11-13T12:34:60.000Z"
        found = set()

        def walk(node, name, path):
            if isinstance(node, dict):
                pattern = node.get("pattern")
                if isinstance(pattern, str) and re.search(pattern, a_time) and not re.search(pattern, same_shape):
                    found.add((name, path))
                    with self.subTest(schema=name, at=path):
                        self.assertIs(node.get("x-instant"), True, "a timestamp without the real-instant keyword")
                        self.assertIsNone(re.search(pattern, sixty), "a timestamp whose pattern takes a second of 60")
                for key, value in node.items():
                    walk(value, name, f"{path}/{key}")
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, name, f"{path}/{i}")

        for schemas in self.sets.values():
            for name, schema in schemas.schemas.items():
                walk(schema, name, "")
        self.assertLessEqual({("common.v1.json", "/$defs/timestamp"), ("revoked-keys.v1.json", "/$defs/timestamp")}, found,
                             "the walk does not see both folders' timestamps")

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
                    pins = [(pin["if"], pin["then"]["properties"]["detail"]) for pin in limits.get("allOf", ())]
                    unavailable = {"required": ["code"], "properties": {"code": {"const": "unavailable"}}}
                    self.assertEqual(row["service"], SERVICE_OF.get(name))
                    if name in UNAVAILABLE:
                        self.assertEqual(pins, [(unavailable, {"enum": UNAVAILABLE[name]})])
                        self.assertIsNone(row["service"], "a mandatory built-in never waits on a service")
                    elif row["service"] is None:
                        # service_starting is the readiness of the service a row names. A row that names none can
                        # never say it; what else its 'unavailable' may come to say is not guessed here.
                        self.assertEqual(pins, [(unavailable, {"not": {"const": "service_starting"}})])
                    else:
                        self.assertEqual(pins, [], "a row that names a service may say that it is starting")
                    self.assertNotIn("service_starting", UNAVAILABLE.get(name, ()))
                    # What a row says of fallbacks and of its service is data nothing else checks: pinned here.
                    self.assertEqual(row["falls_back"], FALLS_BACK.get(name, []))
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
        the request asked for (transcript when it names none) and is the continuation of the cursor it was asked
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

    def test_a_transcript_page_is_held_to_what_the_events_of_its_raw_page_say(self):
        """A transcript page was compared with its raw page by each line's seq and type only, so a line of
        engine output with another text, relabelled as the core's and trusted, passed (a reader of the pull
        request showed it on g08). Each line is now held to its event in source, trust, state and the
        event's own text. The time is held only on a page that begins at position 1: it counts from the
        log's first event, which no other page holds."""
        g08 = self.vector("event.page/g08-*.json")

        def found(vector, number=None, **changed):
            page = copy.deepcopy(vector["page"])
            if number is not None:
                page["lines"][number - 1].update(changed)
            return self.rules.transcript_page_problems(vector["params"], vector["raw"], page)
        self.assertEqual(found(g08), [], "the published page is the projection of its raw page")
        self.assertEqual((g08["raw"]["events"][1]["type"], g08["page"]["lines"][1]["source"], g08["page"]["lines"][1]["trust"]),
                         ("engine.output", "engine", "untrusted"))
        # (The last two the schemas refuse by themselves: an engine's line is untrusted there, and a line of
        # engine output has no state. The first three they take, and only the rule finds them.)
        for changed, wanted, the_schemas_take_it in (
                ({"text": "something else", "source": "core", "trust": "trusted"}, ["2:source", "2:trust", "2:text"], True),
                ({"text": "something else"}, ["2:text"], True), ({"source": "core"}, ["2:source"], True),
                ({"trust": "trusted"}, ["2:trust"], False), ({"state": "running"}, ["2:state"], False)):
            with self.subTest(changed=sorted(changed)):
                self.assertEqual(found(g08, 2, **changed), wanted)
                judged = client.judge_vector(self.schemas, "event.page", dict(g08, page=dict(
                    g08["page"], lines=[g08["page"]["lines"][0], dict(g08["page"]["lines"][1], **changed)]), expect=wanted))
                if the_schemas_take_it:
                    self.assertIsNone(judged, "the record is one the schemas take: only the rule finds it")
                else:
                    self.assertIn("do not fit the method table", judged)
        # What one page cannot say. g08 begins at position 9, so its lines' times are not compared;
        self.assertEqual(found(g08, 2, time_rel_ms=g08["page"]["lines"][1]["time_rel_ms"] + 1), [])
        # the renderer's own words are no rule's (the gap's line is the renderer's sentence);
        self.assertEqual(found(g08, 1, text="other words", speech="other words"), [])
        # and a page that begins at position 1 holds the first event, so there the time is held too.
        first = self.vector("event.page/x18-*.json")
        events = copy.deepcopy(first["raw"]["events"])
        events[0]["seq"], events[1]["seq"], events[1]["time"] = 1, 2, "2026-11-13T07:34:02.124Z"
        params = dict(first["params"], cursor=dict(first["params"]["cursor"], seq=0))
        raw = dict(first["raw"], events=events, cursor=dict(first["raw"]["cursor"], seq=2))
        lines = [dict(line, seq=seq, time_rel_ms=at) for line, seq, at in zip(first["page"]["lines"], (1, 2), (0, 50))]
        whole = {"params": params, "raw": raw,
                 "page": dict(first["page"], lines=lines, cursor=raw["cursor"], more=raw["more"], epoch_advanced=raw["epoch_advanced"])}
        self.assertEqual(found(whole), [])
        self.assertEqual(found(whole, 2, time_rel_ms=51), ["2:time_rel_ms"])
        self.assertEqual(found(whole, 1, time_rel_ms=2074), ["1:time_rel_ms"])

    def test_a_tasks_updated_at_follows_only_the_changes_of_its_own_run_or_turn(self):
        """updated_at is the time of the latest state.changed OF the Task's own run or turn. What changed is
        named in the event's payload (``entity`` and ``entity_id``); where the event is filed (the ``run_id``
        and ``turn_id`` beside the payload) says under which turn it was logged. So a change of the run, or of
        the session, that is filed under a turn does not move that turn's Task, and a change of a turn, of a
        session or of another run in a run's log does not move the run's. A turn's scope is kept as it was:
        a change that names the turn and is filed under another turn, or under none, does not move its Task
        either. Its own later change, filed under it, moves each. (A reader of
        the pull request found the rule counting every state.changed filed under the turn, and every one
        there was for a run: the id the rule was handed was never used.) Every event here is a published
        one, filed again, and the rule's own check says first that it fits event.v1."""
        later = "2026-11-13T07:59:59.000Z"
        good = self.folder / "fixtures" / "good" / "event.v1"
        turn, run = self.vector("task.derivation/t03-*.json"), self.vector("task.derivation/r16-*.json")
        of_the_run, of_the_turn = run["events"][-1], turn["events"][-1]
        of_the_session = client.read_json(good / "h02-state-changed-session-created.json")
        of_another_run = client.read_json(good / "h29-first-event-of-a-run-without-a-session.json")
        # (A change that names the turn's id and is not a turn's: the kind is part of the name.)
        of_a_session_with_the_turns_id = copy.deepcopy(of_the_session)
        of_a_session_with_the_turns_id["payload"]["entity_id"] = turn["turn_id"]
        self.assertEqual([(event["payload"]["entity"], event["payload"]["entity_id"])
                          for event in (of_the_run, of_the_turn, of_the_session, of_a_session_with_the_turns_id)],
                         [("run", run["view"]["run_id"]), ("turn", turn["turn_id"]),
                          ("session", turn["session"]["session_id"]), ("session", turn["turn_id"])])
        self.assertNotEqual(of_another_run["payload"]["entity_id"], run["view"]["run_id"])

        def with_one_more(vector, event, turn_id):
            """The vector with a copy of this change filed last in its log, under that turn or under none."""
            last = vector["events"][-1]
            filed = dict(copy.deepcopy(event), seq=last["seq"] + 1, session_id=last["session_id"],
                         run_id=last["run_id"], turn_id=turn_id, time=later)
            return dict(vector, events=vector["events"] + [filed])

        under = turn["turn_id"]
        cases = (("a turn", turn, of_the_turn, under, ((of_the_run, under), (of_the_session, under),
                                                      (of_a_session_with_the_turns_id, under),
                                                      (of_the_turn, None), (of_the_turn, "turn-000b"))),
                 ("a run", run, of_the_run, None, ((of_the_turn, under), (of_the_session, None), (of_another_run, None))))
        for name, vector, own, own_turn, others in cases:
            self.assertIsNone(client.judge_vector(self.schemas, "task.derivation", vector), "the published vector agrees")
            for other, filed_under in others:
                with self.subTest(task_of=name, a_later_change_of=(other["payload"]["entity"], other["payload"]["entity_id"]),
                                  filed_under=filed_under):
                    self.assertIsNone(client.judge_vector(self.schemas, "task.derivation", with_one_more(vector, other, filed_under)),
                                      "a change of something else moved the Task's updated_at")
            with self.subTest(task_of=name, a_later_change_of="itself"):
                self.assertEqual(client.judge_vector(self.schemas, "task.derivation", with_one_more(vector, own, own_turn)),
                                 "the rule finds ['updated_at'], the vector says []", "its own later change moves it")

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

    def test_a_record_that_is_not_the_shape_of_an_approval_is_never_carried_out(self):
        """Complete validation is the published shape as well as what must agree inside the record. Each of
        these records agrees with itself in everything ``approval_problems`` looks at and is not an approval:
        approved and saying deny, with no answerer, with no answer time, in a form of lines this version
        does not have. None is carried out, also after a restart, and the record, the binding and the
        receipts stay as they were. (Until the fourth read each was.)"""
        rules = self.rules

        def says_deny(record):
            record["answer"] = "deny"

        def has_no_answerer(record):
            record["answered_by"] = None

        def has_no_answer_time(record):
            record["answered_at"] = None

        def is_in_another_form(record):
            record["rendered_form"] = "csl-approval-lines/v9"

        for spoil in (says_deny, has_no_answerer, has_no_answer_time, is_in_another_form):
            model, kept = self.approved()
            spoil(model.record)
            before = (client._exact(model.record), client._exact(model.binding), copy.deepcopy(model.receipts))
            with self.subTest(record=spoil.__name__):
                self.assertEqual(rules.approval_problems(model.record), [], "nothing INSIDE the record disagrees")
                self.assertFalse(rules.fits("approval.v1.json", model.record))
                self.refused("record_invalid", model.consume, model.record["revision"], model.record["digest"], kept)
                again = rules.ApprovalModel(model.record, model.binding, model.receipts)
                self.refused("record_invalid", again.consume, again.record["revision"], again.record["digest"], kept)
                self.assertEqual((client._exact(model.record), client._exact(model.binding), model.receipts), before)

    def test_a_binding_that_is_not_the_shape_of_a_binding_is_a_mismatch(self):
        """The binding kept and the binding offered are equal and name this approval, this run and this engine,
        and they are not a binding: a member is missing, is of another type, or is a time that does not exist."""
        for member, value in (("engine_request_id", ...), ("engine_request_id", 7), ("bound_at", ...),
                              ("bound_at", "2026-02-31T07:34:30.000Z"), ("left_over", "x")):
            model, kept = self.approved()
            for held in (kept, model.binding):
                if value is ...:
                    del held[member]
                else:
                    held[member] = value
            with self.subTest(member=member, value=value):
                self.assertEqual(model.binding, kept)
                self.assertFalse(self.rules.fits("approval.v1.json#/$defs/binding", kept))
                self.refused("binding_mismatch", model.consume, 1, model.record["digest"], kept)
                self.assertEqual(model.record["state"], "approved")

    def test_the_model_owns_what_it_keeps(self):
        """A caller's own object, changed after the call returned, changes nothing the model keeps: not who a
        request was shown to, not who answered it, and not a receipt it was restored with."""
        rules = self.rules
        found = self.vector("approval.lifecycle/s07-*.json")
        model = rules.ApprovalModel(found["record"], found["binding"])
        record = model.record
        opens = {"command_id": "t.1", "approval_id": record["approval_id"], "surface": "tui"}
        answers = {"command_id": "t.2", "approval_id": record["approval_id"], "surface": "tui",
                   "expected_revision": record["revision"], "digest": record["digest"], "answer": "approve"}
        who = dict(self.OWNER)
        self.assertEqual(model.call(who, "approval.open", opens, record["requested_at"])["code"], "ok")
        who["id"] = "someone-else"
        self.assertEqual(model.record["opened_by"][-1]["principal"], self.OWNER, "the opening is the model's own")
        who = dict(self.OWNER)
        self.assertEqual(model.call(who, "approval.answer", answers, record["requested_at"])["code"], "ok")
        who["id"] = "someone-else"
        self.assertEqual(model.record["answered_by"], self.OWNER, "so is the answerer")
        receipts = copy.deepcopy(model.receipts)
        restored = rules.ApprovalModel(model.record, model.binding, receipts)
        for _, kept_answer in receipts.values():
            kept_answer["code"] = "changed-later"
            kept_answer["approval"]["state"] = "denied"
        replay = restored.call(self.OWNER, "approval.answer", answers, record["requested_at"])
        self.assertEqual((replay["code"], replay["approval"]["state"]), ("ok", "approved"), "and so is a restored receipt")

    def test_the_reader_of_shapes_agrees_with_the_validator_on_every_fixture(self):
        """``rules.fits`` reads the published schemas with the standard library only. On every fixture of this
        folder, good and bad, it says what the Draft 2020-12 validator says of the same value."""
        said = {True: 0, False: 0}
        for entry in client.load_index(self.folder):
            instance = client.read_json(self.folder / "fixtures" / entry["file"])
            theirs = not client.error_objects(self.schemas.validator(entry["schema"]), instance)
            with self.subTest(fixture=entry["file"]):
                self.assertEqual(self.rules.fits(entry["schema"], instance, self.schemas.schemas), theirs)
            said[theirs] += 1
        self.assertGreater(said[True], 300, "fixtures that fit")
        self.assertGreater(said[False], 1000, "fixtures that do not")
        # A rule it does not know is never passed over as if it were not there.
        with self.assertRaises(ValueError):
            self.rules.fits("x.json", {}, {"x.json": {"propertyNames": {"maxLength": 3}}})

    def test_the_reader_of_shapes_knows_each_keyword_the_published_files_use(self):
        """One keyword at a time, away from the fixtures: a value the rule takes and one it refuses. A keyword
        that speaks of one type of value says nothing of a value of another type, as the dialect has it."""
        cases = {
            "type": ({"type": "integer"}, [3, 3.0], [True, "3", 3.5, None]),
            "type, several": ({"type": ["string", "null"]}, ["a", None], [1, [], {}]),
            "type number": ({"type": "number"}, [1, 1.5], [True, "1"]),
            "enum": ({"enum": ["a", 1, None]}, ["a", 1, 1.0, None], ["b", True, 2]),
            "const": ({"const": True}, [True], [1, "true"]),
            "allOf": ({"allOf": [{"type": "string"}, {"minLength": 2}]}, ["ab"], ["a", 12]),
            "anyOf": ({"anyOf": [{"type": "string"}, {"type": "null"}]}, ["a", None], [1]),
            "oneOf": ({"oneOf": [{"type": "integer"}, {"minimum": 5}]}, [3, 5.5, "x"], [7, 2.5]),
            "not": ({"not": {"const": "service_starting"}}, ["daemon_closing", 1], ["service_starting"]),
            "if, then": ({"if": {"const": 1}, "then": {"type": "string"}}, [2, "a"], [1]),
            "if, else": ({"if": {"type": "string"}, "then": {"minLength": 1}, "else": {"type": "null"}}, ["a", None], ["", 1]),
            "minLength": ({"minLength": 2}, ["ab", 1], ["a"]),
            "maxLength": ({"maxLength": 2}, ["ab", 123], ["abc"]),
            "pattern": ({"pattern": "^run-[0-9a-f]{4}$"}, ["run-00ff", 7], ["run-00fg", "xrun-00ff"]),
            "pattern, anywhere in the text": ({"pattern": "[0-9]"}, ["a1b"], ["ab"]),
            "x-instant": ({"x-instant": True}, ["2026-11-13T07:34:30.000Z", 7], ["2026-02-31T07:34:30.000Z"]),
            "minimum": ({"minimum": 0}, [0, 2.5, "x"], [-1, -0.5]),
            "maximum": ({"maximum": 9}, [9, "x"], [10]),
            "exclusiveMinimum": ({"exclusiveMinimum": 0}, [1, 0.1], [0, -1]),
            "minItems": ({"minItems": 1}, [[1], "x"], [[]]),
            "maxItems": ({"maxItems": 1}, [[1], []], [[1, 2]]),
            "uniqueItems": ({"uniqueItems": True}, [[1, "1", True], []], [[1, 1.0], [{"a": 1}, {"a": 1}]]),
            "items": ({"items": {"type": "string"}}, [["a"], [], "x"], [["a", 1]]),
            "required": ({"required": ["a"]}, [{"a": None}, 7], [{"b": 1}]),
            "properties": ({"properties": {"a": {"type": "string"}}}, [{"a": "x"}, {"b": 1}], [{"a": 1}]),
            "additionalProperties": ({"properties": {"a": {}}, "additionalProperties": False}, [{"a": 1}, {}], [{"a": 1, "b": 2}]),
            "additionalProperties, a schema": ({"additionalProperties": {"type": "integer"}}, [{"a": 1}], [{"a": "x"}]),
            "dependentRequired": ({"dependentRequired": {"launch": ["principal"]}}, [{"launch": 1, "principal": 2}, {"principal": 2}],
                                  [{"launch": 1}]),
            "$ref, in the file": ({"$ref": "#/$defs/word", "$defs": {"word": {"type": "string"}}}, ["a"], [1]),
            "$ref, to another file": ({"$ref": "other.json#/$defs/id"}, ["id-1"], ["1"]),
            "a schema that is true": (True, [1, None], []),
            "a schema that is false": (False, [], [1, None]),
        }
        other = {"$defs": {"id": {"type": "string", "pattern": "^id-"}}}
        for keyword, (schema, takes, refuses) in cases.items():
            schemas = {"x.json": {"$defs": {"x": schema}} if isinstance(schema, bool) else schema, "other.json": other}
            ref = "x.json#/$defs/x" if isinstance(schema, bool) else "x.json"
            for value, verdict in [(value, True) for value in takes] + [(value, False) for value in refuses]:
                with self.subTest(keyword=keyword, value=value):
                    self.assertIs(self.rules.fits(ref, value, schemas), verdict)
                    if "another file" not in keyword:       # and the table itself is what the dialect's validator says
                        self.assertIs(client.Validator(schema).is_valid(value), verdict, "the case is wrong")

    def test_the_reader_refuses_an_unsupported_keyword_wherever_a_schema_holds_it(self):
        """The whole schema set is checked before any value is looked at, so the branch a value takes does not
        decide whether a schema is supported. An unsupported keyword in an unused definition, an absent
        property, an unselected branch, a short-circuited alternative, array items, below each keyword that
        holds a schema, in a document that a reference reaches and in one that none reaches: each is refused
        (ValueError) whatever the value. Names of properties and definitions and the values of const and enum
        are not schema locations, and a keyword's name there is not a keyword. (The fifth reads, R5-2 and F5:
        until then each of the first nine placements was true.)"""
        fits = self.rules.fits
        unknown = {"propertyNames": False}
        placements = [  # the two reads' own schemas, each with the value it used
            ({"anyOf": [{}, {"propertyNames": False}]}, {}),
            ({"properties": {"absent": {"prefixItems": [False]}}}, {}),
            ({"$defs": {"later": {"unevaluatedProperties": False}}}, {}),
            ({"if": False, "then": {"propertyNames": False}}, {}),
            ({"items": {"prefixItems": [False]}}, {}),
            ({"else": {"propertyNames": False}}, {}),
            ({"anyOf": [{}, {"prefixItems": [{"type": "integer"}]}]}, ["a"]),
            ({"$defs": {"future": {"unevaluatedProperties": False}}, "type": "object"}, {}),
            ({"if": {"const": 1}, "then": {}, "else": {"format": "date-time"}}, 1),
        ]
        # Below every keyword that holds a schema, behind an alternative that is already true for any value.
        for holder in ({"$defs": {"d": unknown}}, {"properties": {"p": unknown}}, {"allOf": [unknown]}, {"anyOf": [unknown]},
                       {"oneOf": [unknown]}, {"not": unknown}, {"if": unknown}, {"then": unknown}, {"else": unknown},
                       {"items": unknown}, {"additionalProperties": unknown}):
            placements.append(({"anyOf": [True, holder]}, {}))
        for schema, value in placements:
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    fits("x.json", value, {"x.json": schema})
        # In another document of the set: one a reference reaches behind a true alternative, and one nothing reaches.
        for schemas in ({"x.json": {"anyOf": [True, {"$ref": "y.json"}]}, "y.json": {"propertyNames": False}},
                        {"x.json": {}, "y.json": {"$defs": {"z": unknown}}}):
            with self.subTest(schemas=schemas):
                with self.assertRaises(ValueError):
                    fits("x.json", {}, schemas)
        # The published set itself, with one unused definition that uses a keyword this reader does not know: every
        # read of it is refused, of that file and of the others. (Until the fifth read the suite stayed green.)
        changed = copy.deepcopy(self.schemas.schemas)
        changed["approval.v1.json"]["$defs"]["future"] = {"prefixItems": [{"type": "integer"}]}
        approval = client.read_json(next((self.folder / "fixtures" / "good" / "approval.v1").glob("h02-*.json")))
        self.assertTrue(fits("approval.v1.json", approval, self.schemas.schemas))
        for ref, value in (("approval.v1.json", approval), ("common.v1.json#/$defs/timestamp", "2026-11-13T07:34:30.000Z")):
            with self.subTest(published=ref):
                with self.assertRaises(ValueError):
                    fits(ref, value, changed)
        # Not schema locations: a keyword's name as a property, a definition, a required name or a dependency, and
        # anything at all inside a value of const or enum.
        for schema, value in (({"const": {"propertyNames": False, "$id": "elsewhere.json", "$ref": "#/nowhere"}},
                               {"propertyNames": False, "$id": "elsewhere.json", "$ref": "#/nowhere"}),
                              ({"enum": [{"prefixItems": [1]}, 2]}, {"prefixItems": [1]}),
                              ({"properties": {"prefixItems": {"type": "integer"}, "$id": {"type": "string"}}},
                               {"prefixItems": 1, "$id": "a"}),
                              ({"required": ["$ref"], "dependentRequired": {"$id": ["format"]}}, {"$ref": 1}),
                              ({"$defs": {"format": {"type": "string"}}, "$ref": "#/$defs/format"}, "a")):
            with self.subTest(schema=schema):
                self.assertIs(fits("x.json", value, {"x.json": schema}), True)
                self.assertIs(client.Validator(schema).is_valid(value), True, "the case is wrong")

    def test_the_reader_refuses_a_keyword_whose_value_it_would_misread(self):
        """A rule's value of another form than the dialect gives it is not read as if it were one: a list of
        values or of names that is a string would be read one character at a time, a length that is text
        compared with a number. Each is refused (ValueError) before a value is looked at, behind an
        alternative that is already true."""
        fits = self.rules.fits
        wrong = {"type": [{"type": "integr"}, {"type": []}, {"type": 3}, {"type": ["string", "string"]}],
                 "enum": [{"enum": "abc"}, {"enum": {"a": 1}}],
                 "minLength": [{"minLength": "2"}, {"minLength": -1}, {"minLength": True}],
                 "maxLength": [{"maxLength": "2"}], "minItems": [{"minItems": 1.5}], "maxItems": [{"maxItems": None}],
                 "pattern": [{"pattern": "("}, {"pattern": 3}],
                 "x-instant": [{"x-instant": "true"}], "uniqueItems": [{"uniqueItems": 1}],
                 "minimum": [{"minimum": "0"}, {"minimum": True}], "maximum": [{"maximum": [9]}],
                 "exclusiveMinimum": [{"exclusiveMinimum": None}],
                 "required": [{"required": "ab"}, {"required": ["a", "a"]}, {"required": [1]}],
                 "dependentRequired": [{"dependentRequired": {"a": "b"}}, {"dependentRequired": ["a"]}],
                 "$ref": [{"$ref": 3}], "allOf": [{"allOf": []}, {"allOf": {}}], "anyOf": [{"anyOf": True}],
                 "oneOf": [{"oneOf": "x"}], "properties": [{"properties": [True]}], "$defs": [{"$defs": [True]}],
                 "items": [{"items": 3}], "not": [{"not": "x"}], "additionalProperties": [{"additionalProperties": "no"}]}
        for keyword, schemas in wrong.items():
            for schema in schemas:
                with self.subTest(keyword=keyword, schema=schema):
                    with self.assertRaises(ValueError):
                        fits("x.json", "ab", {"x.json": {"anyOf": [True, schema]}})
        # The forms the dialect does give are read: an empty list of values takes nothing, as the validator says.
        for schema, value, verdict in (({"enum": []}, "ab", False), ({"required": []}, {}, True), ({"minLength": 0}, "", True)):
            with self.subTest(read=schema):
                self.assertIs(fits("x.json", value, {"x.json": schema}), verdict)
                self.assertIs(client.Validator(schema).is_valid(value), verdict, "the case is wrong")

    def test_the_reader_resolves_a_reference_as_the_dialect_does_or_refuses_it(self):
        """A reference is resolved the way Draft 2020-12 resolves it, or the schema is refused (ValueError)
        before any value is looked at: never a wrong answer, a KeyError or a TypeError. This reader resolves a
        bare document name of the set or none, then nothing or a JSON Pointer through objects to a schema
        location (with ~0 and ~1). It refuses an embedded $id or $schema, a root $id that is not the
        document's name, another dialect, any other URI, a fragment that is a name, a percent-encoded
        pointer, a pointer through an array, into a value that is not a schema, or to nothing. (The fifth
        read, R5-3: until then an array pointer was a TypeError, an encoded one a KeyError, and an embedded
        $id was passed over and gave the wrong answer.)"""
        fits, dialect = self.rules.fits, "https://json-schema.org/draft/2020-12/schema"
        # The read's six cases, and a percent-encoded pointer whose literal spelling names another definition:
        # the dialect's answer, or a refusal.
        either = {
            "array": {"$ref": "#/$defs/n/anyOf/0", "$defs": {"n": {"anyOf": [{"type": "string"}, {"type": "number"}]}}},
            "encoded": {"$ref": "#/$defs/a%20b", "$defs": {"a b": {"type": "string"}}},
            "encoded, twin": {"$ref": "#/$defs/a%20b", "$defs": {"a b": {"type": "string"}, "a%20b": {"type": "integer"}}},
            "nested": {"$id": "x.json", "$ref": "#/$defs/sub", "$defs": {
                "thing": {"type": "string"}, "sub": {"$id": "sub.json", "$ref": "#/$defs/thing", "$defs": {"thing": {"type": "integer"}}}}},
        }
        for name, schema in either.items():
            for value in ("x", 1):
                with self.subTest(case=name, value=value):
                    theirs = client.Validator(schema).is_valid(value)
                    try:
                        mine = fits("x.json", value, {"x.json": schema})
                    except ValueError:
                        continue
                    self.assertIs(mine, theirs)
        # Each form this reader does not resolve is refused, whatever the value, the read's own among them.
        refused = {
            "an embedded $id": {"x.json": {"$defs": {"s": {"$id": "s.json"}}}},
            "an embedded $schema": {"x.json": {"$defs": {"s": {"$schema": dialect}}}},
            "a root $id that is not the name": {"x.json": {"$id": "y.json"}},
            "another dialect": {"x.json": {"$schema": "http://json-schema.org/draft-07/schema#"}},
            "an absolute URI": {"x.json": {"$ref": "https://example.com/x.json"}},
            "a path": {"x.json": {"$ref": "./y.json"}, "y.json": {}},
            "a document of another folder": {"x.json": {"$ref": "sub/y.json"}, "y.json": {}},
            "a document not in the set": {"x.json": {"$ref": "y.json#/$defs/a"}},
            "a name for a fragment": {"x.json": {"$defs": {"a": {"$anchor": "a"}}, "$ref": "#a"}},
            "a name for a fragment, alone": {"x.json": {"anyOf": [True, {"$ref": "#here"}]}},
            "a percent-encoded pointer": {"x.json": either["encoded, twin"]},
            "a pointer through an array": {"x.json": either["array"]},
            "a pointer through a list of values": {"x.json": {"$ref": "#/$defs/e/enum/a", "$defs": {"e": {"enum": ["a"]}}}},
            "a pointer through a text": {"x.json": {"$ref": "#/$defs/p/pattern/b", "$defs": {"p": {"pattern": "ab"}}}},
            "a pointer into a value of const": {"x.json": {"$ref": "#/$defs/c/const", "$defs": {"c": {"const": {"type": "string"}}}}},
            "a pointer to schemas by name": {"x.json": {"$ref": "#/$defs", "$defs": {"type": {"const": 1}}}},
            "a pointer to nothing": {"x.json": {"$ref": "#/$defs/missing"}},
            "a pointer token that is not one": {"x.json": {"$ref": "#/$defs/a~2b", "$defs": {"a~2b": {}}}},
            "a document name that is a path": {"sub/x.json": {}},
            # Back to itself on the same value: undefined in the dialect, and a read that never ends.
            "a reference to its own document": {"x.json": {"$ref": "#"}},
            "a loop through a condition": {"x.json": {"$ref": "#/$defs/a", "$defs": {"a": {"if": {"$ref": "#/$defs/b"}},
                                                                                     "b": {"anyOf": [False, {"$ref": "#/$defs/a"}]}}}},
            "a loop across documents": {"x.json": {"allOf": [{"$ref": "y.json"}]}, "y.json": {"not": {"$ref": "x.json#"}}},
            "a loop in an unused definition": {"x.json": {"$defs": {"a": {"oneOf": [True, {"$ref": "#/$defs/a"}]}}}},
        }
        for name, schemas in refused.items():
            ref = next(iter(schemas))
            for value in ("x", 1):
                with self.subTest(refused=name, value=value):
                    with self.assertRaises(ValueError):
                        fits(ref, value, schemas)
        # The entry reference is held to the same.
        entry = {"x.json": {"$defs": {"n": {"anyOf": [{"type": "string"}, {"type": "number"}]}}}}
        self.assertIs(fits("x.json#/$defs/n", "x", entry), True)
        for ref in ("nope.json", "x.json#/$defs/n/anyOf/0", "x.json#/$defs", "x.json#/$defs/n/missing", "#/$defs/n", "x.json#n"):
            with self.subTest(entry=ref):
                with self.assertRaises(ValueError):
                    fits(ref, "x", entry)
        with self.assertRaises(ValueError):     # and a set that is not schemas by name is not read as one
            fits("x.json", "x", [("x.json", {})])
        # And what it resolves, it resolves as the dialect does.
        tree = {"type": "object", "properties": {"child": {"$ref": "#"}}, "additionalProperties": False}
        escaped = {"$ref": "#/$defs/a~1b", "$defs": {"a/b": {"$ref": "#/$defs/c~0d"}, "c~d": {"type": "string"}}}
        for schemas, ref, value, verdict in (({"x.json": tree}, "x.json", {"child": {"child": {}}}, True),
                                             ({"x.json": tree}, "x.json", {"child": {"child": 1}}, False),
                                             ({"x.json": escaped}, "x.json", "a", True), ({"x.json": escaped}, "x.json", 1, False),
                                             ({"x.json": escaped}, "x.json#/$defs/c~0d", 1, False),
                                             ({"x.json": {"$id": "x.json", "$ref": "y.json"}, "y.json": {"type": "string"}}, "x.json", "a", True),
                                             ({"x.json": {"$id": "x.json", "$ref": "y.json"}, "y.json": {"type": "string"}}, "x.json", 1, False),
                                             ({"x.json": {"$ref": "y.json#"}, "y.json": {"$schema": dialect, "type": "string"}}, "x.json", 1, False)):
            with self.subTest(resolved=schemas, value=value):
                self.assertIs(fits(ref, value, schemas), verdict)

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
        # A proposed effect is first held to the shape of an effect of the request's own source ('effect'). Only
        # one that is such an effect is compared with the target it would replace ('target').
        targets = [dict(effect, run_id="run-" + "0" * 32), dict(effect, session_id="ses-" + "0" * 16), dict(effect, session_id=None),
                   dict(effect, engine="codex")]
        misshapen = [dict(effect, kind="plugin_effect"), plugin, {key: value for key, value in effect.items() if key != "run_id"},
                     {key: value for key, value in effect.items() if key != "cwd"}, dict(effect, run_id="another run"),
                     dict(effect, engine="")]
        cases = list(changes.items()) + [("target", {"effect": target}) for target in targets] \
            + [("effect", {"effect": other}) for other in misshapen] \
            + [  # A good new effect and a bad deadline in ONE change: the effect must not have been written first.
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
        # Nor can a request that is not the shape of an approval: a pending one that already carries an answer.
        model = rules.ApprovalModel(found["record"], found["binding"])
        model.record["answer"] = "deny"
        before = client._exact(model.record)
        self.refused("record_invalid", model.change, expires_at="2026-11-13T08:10:00.000Z")
        self.assertEqual(client._exact(model.record), before)
        model = rules.ApprovalModel(found["record"], found["binding"])
        model.record["state"] = "approved"
        self.refused("resolved", model.change, expires_at="2026-11-13T08:10:00.000Z")

    def test_a_record_whose_members_cannot_be_read_is_record_invalid(self):
        """A durable record read back without a member that a consumption or a change reads, or with one of
        another type or outside its list, is refused 'record_invalid' before that member is read: never a
        KeyError or a TypeError, and never a refusal chosen by a state that is not a state. The record, its
        revision, the binding and the receipts stay as they were, also after a restart. (The fifth reads, R5-1
        and F1: until then a missing state, source or revision was a KeyError, a null revision a TypeError,
        and a state outside the list was 'not_approved'.)"""
        rules = self.rules

        def fixture(name):
            return client.read_json(next((self.folder / "fixtures" / "good" / "approval.v1").glob(name + "-*.json")))

        def spoiled(name, member, value):
            record = fixture(name)
            if value is ...:
                del record[member]
            else:
                record[member] = value
            return record

        def refused_unchanged(model, operation, *args, **kwargs):
            before = (client._exact(model.record), client._exact(model.binding), copy.deepcopy(model.receipts))
            self.refused("record_invalid", operation, *args, **kwargs)
            self.assertEqual((client._exact(model.record), client._exact(model.binding), model.receipts), before)

        approved, pending = fixture("h02"), fixture("h01")
        self.assertEqual((approved["state"], pending["state"]), ("approved", "pending"))
        bad_states = [..., None, True, 1, "not-a-state", "approved-ish"]
        # A consumption: the record's shape before its state, its revision, its digest or its source is read.
        for member, value in [("state", value) for value in bad_states] + [
                (member, ...) for member in ("revision", "digest", "source", "effect", "answer")] + [
                ("revision", None), ("revision", "1"), ("source", "neither"), ("effect", None)]:
            with self.subTest(consume=member, value=value):
                record = spoiled("h02", member, value)
                model = rules.ApprovalModel(record)
                refused_unchanged(model, model.consume, approved["revision"], approved["digest"], None)
                again = rules.ApprovalModel(model.record, model.binding, model.receipts)
                refused_unchanged(again, again.consume, approved["revision"], approved["digest"], None)
        # A change: the state's shape before it is branched on, and a pending record whole before its source, its
        # effect or its revision is read to build a candidate; with a new deadline, and with a new effect.
        new_effect = dict(pending["effect"], input={"command": "make"})
        for member, value in [("state", value) for value in bad_states] + [
                (member, ...) for member in ("source", "effect", "revision", "rendered")] + [
                ("revision", None), ("revision", False), ("revision", "1"), ("revision", 0), ("source", "neither"),
                ("effect", "rm -rf /"), ("effect", {})]:
            for change in ({"expires_at": pending["expires_at"]}, {"effect": new_effect}):
                with self.subTest(change=member, value=value, changing=next(iter(change))):
                    model = rules.ApprovalModel(spoiled("h01", member, value))
                    refused_unchanged(model, model.change, **change)
        # A state that IS a state keeps its own refusal, and a whole pending record is changed as before.
        for state in ("approved", "denied", "expired", "vetoed", "consumed"):
            with self.subTest(resolved=state):
                model = rules.ApprovalModel(spoiled("h01", "state", state))
                self.refused("resolved", model.change, expires_at=pending["expires_at"])
        model = rules.ApprovalModel(pending)
        model.change(effect=new_effect)
        self.assertEqual((model.record["revision"], model.record["effect"], rules.approval_problems(model.record)),
                         (pending["revision"] + 1, new_effect, []))

    def test_a_candidate_is_validated_whole_after_the_record_it_was_made_from(self):
        """The pending record is checked whole before a candidate is built from it, and the candidate is
        checked whole again before it replaces the record: lines the renderer drew outside the published
        shape (here, a line holding a line break) are refused 'record_invalid' and never committed."""
        rules = self.rules
        found = self.vector("approval.lifecycle/s15-*.json")
        model = rules.ApprovalModel(found["record"], found["binding"])
        before = (client._exact(model.record), client._exact(model.binding))
        drawn = rules.approval_lines

        def broken(source, effect):
            lines = drawn(source, effect)
            return lines[:-1] + [dict(lines[-1], text=lines[-1]["text"] + "\n")]

        with mock.patch.object(rules, "approval_lines", broken):
            self.refused("record_invalid", model.change, effect=dict(found["record"]["effect"], input={"command": "make"}))
        self.assertEqual((client._exact(model.record), client._exact(model.binding)), before)

    def test_a_fresh_open_or_answer_reads_no_member_of_a_record_that_is_not_an_approval(self):
        """A whole, well-formed approval.open or approval.answer over a restored record that lacks a member
        is refused 'record_invalid' (the core's own refusal, not an answer of either method) before the
        record is read: no host exception, no answer, no receipt, and record, binding and receipts as they
        were. (The sixth reads, Q1: until then a record without its effect or its lines was answered 'ok'
        and approved, with a receipt, and one without its state was a KeyError.)"""
        rules = self.rules
        seed = client.read_json(next((self.folder / "fixtures" / "good" / "approval.v1").glob("h01-*.json")))

        def refused_unchanged(model, principal, method, params, at):
            self.assertTrue(rules.fits("methods.v1.json#/$defs/params." + method, params), "the call itself is whole")
            before = copy.deepcopy((model.record, model.binding, model.receipts))
            try:
                self.refused("record_invalid", model.call, principal, method, params, at)
            finally:
                self.assertEqual((model.record, model.binding, model.receipts), before, "no answer and no receipt")

        # The first read's cases: the request already shown to this principal on this surface at its revision, so
        # that an answer would pass every check of its own.
        shown = copy.deepcopy(seed)
        reviewer = {"kind": "owner", "id": "review-owner"}
        shown["opened_by"] = [{"principal": reviewer, "surface": "cli", "revision": shown["revision"], "at": shown["requested_at"]}]
        self.assertTrue(rules.fits("approval.v1.json", shown))
        opens = {"approval_id": shown["approval_id"], "command_id": "r6fresh-00000001", "surface": "cli"}
        answers = dict(opens, expected_revision=shown["revision"], digest=shown["digest"], answer="approve")
        for method, missing in (("approval.open", "state"), ("approval.answer", "state"), ("approval.open", "expires_at"),
                                ("approval.answer", "effect"), ("approval.open", "effect"), ("approval.answer", "opened_by")):
            record = copy.deepcopy(shown)
            del record[missing]
            with self.subTest(read="first", method=method, missing=missing):
                refused_unchanged(rules.ApprovalModel(record), reviewer, method, opens if method == "approval.open" else answers,
                                  shown["requested_at"])
        # The second read's cases: the published request as it is, each of six members deleted, both methods.
        opens = {"command_id": "r6.open.0001", "approval_id": seed["approval_id"], "surface": "tui"}
        answers = dict(opens, command_id="r6.answer.0001", expected_revision=seed["revision"], digest=seed["digest"], answer="approve")
        for missing in ("state", "opened_by", "expires_at", "revision", "approval_id", "rendered"):
            for method, params in (("approval.open", opens), ("approval.answer", answers)):
                record = copy.deepcopy(seed)
                del record[missing]
                with self.subTest(read="second", method=method, missing=missing):
                    refused_unchanged(rules.ApprovalModel(record), self.OWNER, method, params, "2026-11-13T07:34:32.000Z")

    def test_a_kept_receipt_is_replayed_even_after_the_record_is_spoiled(self):
        """The boundary of a fresh operation is not the boundary of a replay: a receipt kept before the record
        was spoiled is still answered as it was, and nothing changes. (Both sixth reads' controls.)"""
        rules = self.rules
        seed = client.read_json(next((self.folder / "fixtures" / "good" / "approval.v1").glob("h01-*.json")))
        for principal, params, spoil in ((self.OWNER, {"command_id": "r6.replay", "approval_id": seed["approval_id"], "surface": "tui"}, "state"),
                                         ({"kind": "owner", "id": "review-owner"},
                                          {"approval_id": seed["approval_id"], "command_id": "r6original-00000001", "surface": "cli"}, "effect")):
            model = rules.ApprovalModel(seed)
            first = model.call(principal, "approval.open", params, "2026-11-13T07:34:32.000Z")
            self.assertTrue(rules.fits("methods.v1.json#/$defs/answer.approval.open", first))
            del model.record[spoil]
            before = copy.deepcopy((model.record, model.binding, model.receipts))
            with self.subTest(spoiled=spoil):
                self.assertEqual(model.call(principal, "approval.open", params, "2026-11-13T07:34:32.000Z"), first)
                self.assertEqual((model.record, model.binding, model.receipts), before)

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

    def test_a_revocation_lists_times_are_real_instants_as_every_other_timestamp(self):
        """The plugin folder's records go through the same client and the same real-instant check: a revoked-key
        list whose issue, expiry or revocation time names a day that does not exist, or a second of 60, does
        not fit, by the standard-library reader (``rules.fits``) and by the client's validator, which reports
        the real-instant keyword itself (and the pattern for a second of 60). The first case is the witness a
        reader of the pull request gave: h01 with expires_at on the 31st of February."""
        plugin = next(folder for folder in client.folders() if (folder / "revoked-keys.v1.json").is_file())
        schemas = client.SchemaSet(plugin)
        validator = schemas.validator("revoked-keys.v1.json")
        listed = client.read_json(plugin / "fixtures" / "good" / "revoked-keys.v1" / "h01-one-revoked-key.json")
        # The standard-library reader is handed the one document: it refuses the folder whole, because the manifest
        # uses minProperties, a keyword it does not read (a separate matter, named in the lane's report).
        alone = {"revoked-keys.v1.json": schemas.schemas["revoked-keys.v1.json"]}
        self.assertTrue(self.rules.fits("revoked-keys.v1.json", listed, alone))
        self.assertEqual(client.leaf_errors(validator, listed), [])
        for place in (("expires_at",), ("issued_at",), ("keys", 0, "revoked_at")):
            for text, keywords in (("2026-02-31T00:00:00.000Z", {"x-instant"}), ("2025-02-29T08:00:00.000Z", {"x-instant"}),
                                   ("2026-11-13T07:59:60.000Z", {"pattern", "x-instant"})):
                with self.subTest(at=place, timestamp=text):
                    changed = copy.deepcopy(listed)
                    holder = changed
                    for part in place[:-1]:
                        holder = holder[part]
                    holder[place[-1]] = text
                    self.assertFalse(self.rules.fits("revoked-keys.v1.json", changed, alone))
                    at = "".join(f"/{part}" for part in place)
                    seen = {keyword for keyword, location, _ in client.leaf_errors(validator, changed) if location == at}
                    self.assertLessEqual(keywords, seen)

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


# ed25519: and 32 lower-case hex digits. A shape only. The good sample names null: no manifest in this
# folder pairs plugin id paper-desk with a developer key.
_DEVELOPER_KEY_ID = "ed25519:a1b2c3d4e5f60718293a4b5c6d7e8f90"


class InstallGrantDeveloperKeyTest(unittest.TestCase):
    """install.v1 accepts developer_key_id as a key id or null, and refuses a malformed one."""

    @classmethod
    def setUpClass(cls):
        folder = client.REPO / "schemas" / "plugin" / "v1"
        cls.validator = client.SchemaSet(folder).validator("install.v1.json")
        cls.base = client.read_json(folder / "fixtures" / "good" / "install.v1" / "h03-manual-grant-to-an-unsigned-plugin.json")

    def errors_for(self, value):
        import copy
        document = copy.deepcopy(self.base)
        document["grants"][0]["developer_key_id"] = value
        return client.leaf_errors(self.validator, document)

    def test_a_well_formed_developer_key_id_and_null_are_accepted(self):
        self.assertEqual(self.errors_for(_DEVELOPER_KEY_ID), [])
        self.assertEqual(self.errors_for(None), [])

    def test_the_schema_is_draft_3_and_the_good_sample_names_no_invented_key(self):
        """Draft 2 let the member in and did not bump. The published good sample must not rest on an id
        no manifest in this folder pairs with paper-desk."""
        folder = client.REPO / "schemas" / "plugin" / "v1"
        schema = client.read_json(folder / "install.v1.json")
        self.assertEqual(schema["x-version"], "1.0-draft.3")
        self.assertIn("developer_key_id", schema["description"])
        self.assertNotIn("with nothing added", schema["description"])
        self.assertIsNone(self.base["grants"][0]["developer_key_id"])

    def test_a_malformed_developer_key_id_is_refused(self):
        malformed = (
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",  # 64 hex, not a key id
            "ed25519:A1B2C3D4E5F60718293A4B5C6D7E8F90",  # upper case
            "ed25519:a1b2c3d4e5f60718293a4b5c6d7e8f9",  # 31 hex digits
            "ed25519:a1b2c3d4e5f60718293a4b5c6d7e8f901",  # 33 hex digits
            _DEVELOPER_KEY_ID + "\n",  # trailing newline: pattern $ matches before it; the not-guard must refuse
            True,
            [],
            "",
            0,
            {"id": _DEVELOPER_KEY_ID},
        )
        for value in malformed:
            found = [item for item in self.errors_for(value) if item[1].endswith("/developer_key_id")]
            self.assertTrue(found, value)


if __name__ == "__main__":
    unittest.main()
