# Model: Claude Opus 5.5 (claude-opus-5-5), lane SESSIONS.
"""The ``session`` command family (control/commands/session.py) on a command table of the test's own."""
from __future__ import annotations

import argparse
import io
import json
import unittest
from unittest import mock

from recursion_core.control import commands
from recursion_core.control.commands import session as session_commands
from recursion_core.sessions.tests.support import Lane


def cli(table):
    top = argparse.ArgumentParser(prog='recursion-control')
    sub = top.add_subparsers(dest='command', required=True)
    table.add_parsers(sub)
    return top


class CommandsTest(unittest.TestCase):

    def setUp(self):
        self.table = commands.CommandTable()
        session_commands.register(self.table)
        self.table.freeze()
        self.parser = cli(self.table)

    def run_cli(self, argv, client):
        args = self.parser.parse_args(argv)
        args.out, args.err = io.StringIO(), io.StringIO()
        status = self.table.dispatch(args, client)
        return status, args.out.getvalue(), args.err.getvalue()

    def test_the_family_adds_one_name_and_takes_none_of_the_cli_s_own(self):
        self.assertEqual(self.table.names(), ('session',))
        self.assertFalse(set(self.table.names()) & set(commands.BUILT_IN))

    def test_each_subcommand_sends_its_method_with_only_what_was_given(self):
        class Client:
            def __init__(self):
                self.calls = []

            def call(self, method, params):
                self.calls.append((method, params))
                return {'code': 'ok'}

        cases = [
            (['session', 'start', '--tenant', 't1', '--engine', 'codex', '--command-id', 'cmd-cli-00001'],
             ('session.start', {'command_id': 'cmd-cli-00001', 'tenant': 't1', 'engine': 'codex'})),
            (['session', 'turn', 'ses-1', '--text', 'go', '--revision', '3', '--command-id', 'cmd-cli-00002'],
             ('session.turn', {'command_id': 'cmd-cli-00002', 'session_id': 'ses-1', 'text': 'go',
                               'expected_revision': 3})),
            (['session', 'steer', 'ses-1', '--text', 'skip', '--turn', 'turn-1', '--command-id', 'cmd-cli-00003'],
             ('session.steer', {'command_id': 'cmd-cli-00003', 'session_id': 'ses-1', 'text': 'skip',
                                'expected_turn_id': 'turn-1'})),
            (['session', 'update', 'qi-1', '--revision', '1', '--delivery', 'steer', '--command-id', 'cmd-cli-00004'],
             ('queue.update', {'command_id': 'cmd-cli-00004', 'item_id': 'qi-1', 'expected_revision': 1,
                               'delivery': 'steer'})),
            (['session', 'cancel', 'qi-1', '--revision', '2', '--command-id', 'cmd-cli-00005'],
             ('queue.cancel', {'command_id': 'cmd-cli-00005', 'item_id': 'qi-1', 'expected_revision': 2})),
            (['session', 'status', 'ses-1'], ('session.status', {'session_id': 'ses-1'})),
            (['session', 'list', '--state', 'live', '--limit', '5'], ('session.list', {'state': 'live', 'limit': 5})),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                client = Client()
                status, out, _ = self.run_cli(argv, client)
                self.assertEqual((status, client.calls, json.loads(out)), (0, [expected], {'code': 'ok'}))

    def test_a_keyed_command_without_an_id_mints_one_and_tells_it(self):
        sent = []

        class Client:
            def call(self, method, params):
                sent.append(params)
                return {'code': 'busy', 'detail': 'live'}

        status, out, err = self.run_cli(['session', 'queue', 'ses-1', '--text', 'later'], Client())
        self.assertEqual(status, 1)
        self.assertEqual(err, f'command_id: {sent[0]["command_id"]}\n')
        self.assertRegex(sent[0]['command_id'], r'^[A-Za-z0-9._:-]{8,128}$')
        first = sent[0]['command_id']
        self.run_cli(['session', 'queue', 'ses-1', '--text', 'later'], Client())
        self.assertNotEqual(sent[1]['command_id'], first)

    def test_a_usage_error_is_argparse_s(self):
        with self.assertRaises(SystemExit) as caught, mock.patch('sys.stderr', io.StringIO()):
            self.parser.parse_args(['session', 'turn', 'ses-1', '--text', 'go', '--revision', '0'])
        self.assertEqual(caught.exception.code, 2)

    def test_end_to_end_through_the_dispatcher(self):
        lane = Lane()

        class Client:
            def call(self, method, params):
                return lane.call(method, params)

        status, out, _ = self.run_cli(['session', 'start', '--tenant', 't1', '--engine', 'claude_code',
                                       '--prompt', 'Read the README.', '--command-id', 'cmd-cli-e2e-01'], Client())
        answer = json.loads(out)
        self.assertEqual((status, answer['code'], answer['session']['state']), (0, 'ok', 'live'))
        status, out, _ = self.run_cli(['session', 'status', answer['session']['session_id']], Client())
        self.assertEqual(json.loads(out)['session'], answer['session'])


if __name__ == '__main__':
    unittest.main()
