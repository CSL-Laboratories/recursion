"""The lane's fixtures for SPINE (tests/fixtures/): each good one fits its published schema, each bad one does not.

They are what the workspace lane sends and reads: the workspace request, the launch workspace a start writes,
the completed workspace a session view shows, and the grant and revoke events. The schemas and the reader are
lane SPINE's; these files are offered for its fixtures folder.
"""
from __future__ import annotations

import json
import pathlib
import unittest

from recursion_core.workspace.tests.published import fits

FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'


class FixturesTest(unittest.TestCase):

    def setUp(self) -> None:
        if fits is None:
            self.skipTest('lane SPINE\'s conformance reader and schemas are not in this tree')
        self.index = json.loads((FIXTURES / 'index.json').read_text(encoding='ascii'))['fixtures']

    def test_the_index_names_every_file_and_only_those(self) -> None:
        named = {entry['file'] for entry in self.index}
        present = {str(path.relative_to(FIXTURES).as_posix()) for path in FIXTURES.glob('*/*.json')}
        self.assertEqual(named, present)
        self.assertTrue(any(name.startswith('bad/') for name in named))

    def test_each_good_fixture_fits_and_each_bad_one_does_not(self) -> None:
        for entry in self.index:
            value = json.loads((FIXTURES / entry['file']).read_text(encoding='ascii'))
            with self.subTest(entry['file']):
                self.assertEqual(fits(entry['schema'], value), entry['file'].startswith('good/'))


if __name__ == '__main__':
    unittest.main()
