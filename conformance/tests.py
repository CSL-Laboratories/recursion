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
        defined, so it is not asked again. Seven objects are open on purpose and are listed here, each with its reason
        in the schema: they hold an engine's, a vendor's or a plugin's own data, and no consumer reads
        typed fields out of them.
        """
        open_on_purpose = {
            ("event.v1.json", "/properties/payload"),                     # typed per type by the rules below it
            ("event.v1.json", "/$defs/tool.call/properties/input"),       # the tool's own input, untrusted
            ("run.view.v1.json", "/$defs/result/oneOf/1/properties/events/items/properties/data"),  # the adapter's
            ("run.view.v1.json", "/$defs/result/oneOf/1/properties/limit_notice"),                  # the adapter's
            ("approval.v1.json", "/properties/effect"),                   # typed per source by the rules below it
            ("approval.v1.json", "/$defs/plugin_effect/properties/payload"),  # the plugin's own data, never read
            ("driver.v2.json", "/$defs/event_draft/properties/payload"),  # typed per type by the rules below it
        }
        rules = ("allOf", "if", "then", "else", "not")

        def is_object(node):
            types = node.get("type")
            return types == "object" or (isinstance(types, list) and "object" in types)

        def closes_itself(node):
            if "additionalProperties" in node:
                return True
            branches = node.get("oneOf")
            if branches and all(isinstance(b, dict) and "additionalProperties" in b for b in branches):
                return True
            ifs = [rule for rule in node.get("allOf", ()) if isinstance(rule, dict) and "if" in rule]
            return bool(ifs) and all("additionalProperties" in rule.get("then", {}) for rule in ifs)

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
