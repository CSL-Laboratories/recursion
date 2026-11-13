"""The conformance checks as tests: ``python -m unittest discover -s conformance``.

One test per fixture, plus checks that the published files are consistent with each other. Nothing here
imports the product. The file is named ``tests.py`` so that ``unittest discover`` finds it by its default
pattern; see README.md for why it is not named ``test_*.py``.
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import wire_client as client  # noqa: E402

VERSION = re.compile(r"1\.0(-draft\.[1-9][0-9]*)?")


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
        allowed = {"jsonschema", "referencing", "wire_client"} | set(sys.stdlib_module_names)
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

if __name__ == "__main__":
    unittest.main()
