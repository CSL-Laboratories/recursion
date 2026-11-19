"""The ending of a start that is not answered names the deadline that fired: a caller's deadline equal to the
start bound (``INITIALIZE_DEADLINE_S``) counts as the start bound."""

from __future__ import annotations

import unittest
from unittest import mock

from recursion_core.mcp.client import SERVER_START_TIMEOUT, McpSessionEnded
from recursion_core.mcp.client import session
from recursion_core.mcp.client.tests.support import InThread, make_client


class StartEnding(unittest.TestCase):
    def test_a_deadline_equal_to_the_start_bound_ends_with_the_start_ending_and_its_record(self):
        records: list = []
        client, stub = make_client(self, on_record=records.append)
        with mock.patch.object(session, "INITIALIZE_DEADLINE_S", 0.3):
            opening = InThread(lambda: client.open(deadline_s=0.3))
            stub.expect(self, "initialize")  # never answered
            with self.assertRaises(McpSessionEnded) as caught:
                opening.get()
        self.assertEqual(SERVER_START_TIMEOUT, caught.exception.code)
        self.assertEqual(SERVER_START_TIMEOUT, client.ending)
        self.assertEqual([{"record": SERVER_START_TIMEOUT, "limit_s": 0.3}], records)
        self.assertIsNone(stub.next(), "no cancel for initialize; the client shut the server's input")


if __name__ == "__main__":
    unittest.main()
