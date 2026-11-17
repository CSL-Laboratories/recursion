"""S7 supplemental check of draft #389 additions over pinned master."""
import json
import unittest

from recursion_core.routes.engines.hermes import HermesStreamParser


class HermesPairing(unittest.TestCase):
    def test_rejected_unnamed_call_does_not_steal_a_later_result(self):
        parser = HermesStreamParser('api_key:synthetic')

        def line(obj):
            return json.dumps(obj) + '\n'

        bad = line({'type': 'tool_use', 'name': 'read_file', 'input': {'path': chr(0xd800)}})
        good = line({'type': 'tool_use', 'name': 'read_file', 'input': {'path': 'b.txt'}})
        self.assertEqual(parser.parse(bad)[0]['type'], 'engine.output')
        valid_id = parser.parse(good)[0]['payload']['call_id']
        results = [parser.parse(line({'type': 'tool_result', 'name': 'read_file', 'output': text,
                                      'is_error': err}))[0]['payload']['call_id']
                   for text, err in [('invalid path', True), ('contents of b.txt', False)]]
        self.assertNotIn(valid_id, results)
        fresh = parser.parse(good)[0]['payload']['call_id']
        done = parser.parse(line({'type': 'tool_result', 'name': 'read_file', 'output': 'fresh',
                                  'is_error': False}))[0]['payload']['call_id']
        self.assertEqual(fresh, done)


if __name__ == "__main__":
    unittest.main()
