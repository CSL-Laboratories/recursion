# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The lane's fixtures are exactly what its code writes, and fit SPINE's schemas."""
from __future__ import annotations

import unittest

from recursion_core.sessions.tests import make_fixtures
from recursion_core.sessions.tests.support import FIXTURES, load_json, published_rules, wire_client


class FixturesTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.found = make_fixtures.scenario()

    def test_the_files_are_what_the_code_writes(self):
        for file, _, _, record in self.found:
            with self.subTest(fixture=file):
                self.assertEqual((FIXTURES / file).read_text(encoding='utf-8'), make_fixtures.text_of(record))
        self.assertEqual(load_json(FIXTURES / 'index.json'), make_fixtures.index_of(self.found))

    def test_no_file_is_left_that_the_index_does_not_name(self):
        named = {file for file, _, _, _ in self.found} | {'index.json', 'README.md'}
        present = {path.relative_to(FIXTURES).as_posix() for path in FIXTURES.rglob('*') if path.is_file()}
        self.assertEqual(present - named, set())

    def test_every_fixture_fits_the_schema_its_index_names_on_every_interpreter(self):
        rules = published_rules()
        self.assertIsNotNone(rules, "SPINE's conformance/rules.py (#368) is not in the tree")
        for file, schema, _, record in self.found:
            with self.subTest(fixture=file):
                self.assertTrue(rules.fits(schema, record))

    def test_every_fixture_validates_against_the_schema_its_index_names(self):
        client = wire_client()
        if client is None:
            self.skipTest('jsonschema is not installed in this interpreter')
        schemas = client.SchemaSet(client.REPO / 'schemas' / 'control' / 'v1')
        for file, schema, _, record in self.found:
            with self.subTest(fixture=file):
                self.assertIsNone(client.check_instance(schemas, {'file': 'good/' + file, 'schema': schema}, record))


if __name__ == '__main__':
    unittest.main()
