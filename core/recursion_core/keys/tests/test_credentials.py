"""``credentials()`` as the routes use it: passed where the daemon passes ``no_custody`` today.

Until SPINE's seams land, the daemon builds both adapters with ``no_custody``; these tests build the same
adapters through ``RouteService`` with the store's ``credential`` instead, and with ``no_custody`` for the base.
"""
from __future__ import annotations

import json

from recursion_core.daemon.control_adapter import no_custody
from recursion_core.keys.errors import KeyRefused
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    TempStoreCase,
    keychain_system,
    needs_posix,
)
from recursion_core.routes import contract as c
from recursion_core.routes.breaker import ConnectionBreaker
from recursion_core.routes.claude_code import ClaudeCodeAdapter
from recursion_core.routes.codex import CodexAdapter
from recursion_core.routes.service import RouteService
from recursion_core.routes.tests.fakes import (
    FakeClock,
    FakeLauncher,
    claude_ok,
    codex_script,
    request,
)

PARENT_ENV = {'PATH': '/usr/bin', 'HOME': '/home/op', 'LANG': 'C.UTF-8', 'CODEX_HOME': '/home/op/.codex-route',
              'ANTHROPIC_API_KEY': 'parent-anthropic-madeup', 'CODEX_API_KEY': 'parent-codex-madeup'}


class Harness:
    def __init__(self, scripts, credentials) -> None:
        self.clock = FakeClock()
        self.launcher = FakeLauncher(scripts, self.clock)
        args = (self.launcher, self.clock, credentials, PARENT_ENV)
        self.service = RouteService({'claude_code': ClaudeCodeAdapter(*args, wall_clock=self.clock),
                                     'codex': CodexAdapter(*args, wall_clock=self.clock)},
                                    ConnectionBreaker(self.clock), self.clock)

    def handle(self, req: dict) -> dict:
        res = self.service.handle(json.loads(json.dumps(req)))
        json.dumps(res)
        return res


@needs_posix
class CredentialSourceTest(TempStoreCase):
    def test_the_value_only_under_the_saved_variable_name(self) -> None:
        store = self.store()
        self.add(store)
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        for conn, env in (('codex-key', 'OPENAI_API_KEY'), ('codex-key', 'ANTHROPIC_API_KEY'), ('nobody', 'CODEX_API_KEY'),
                          ('codex-key\n', 'CODEX_API_KEY'), ('', 'CODEX_API_KEY'), ('codex-key', None)):
            with self.subTest(conn=conn, env=env):
                with self.assertRaises(KeyError) as caught:
                    store.credential(conn, env)
                self.assertIs(type(caught.exception), KeyError)        # exactly as S7's default raises
                self.assertEqual(caught.exception.args, (conn,))

    def test_no_store_at_all_is_the_default_refusal(self) -> None:
        with self.assertRaises(KeyError) as caught:
            self.store().credential('codex-key', 'CODEX_API_KEY')
        self.assertIs(type(caught.exception), KeyError)
        with self.assertRaises(KeyError) as base:
            no_custody('codex-key', 'CODEX_API_KEY')
        self.assertEqual(caught.exception.args, base.exception.args)

    def test_no_spend_opt_in_is_refused_without_touching_the_keychain(self) -> None:
        system, _ = keychain_system()
        store = self.store(system)
        self.add(store, cap=0)
        system.calls.clear()
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'spend_not_allowed')
        self.assertEqual(refused.exception.args, ('codex-key',))
        self.assertIsInstance(refused.exception, KeyError)
        self.assertEqual(system.calls, [])

    def test_an_unexpected_failure_is_a_refusal_not_an_escape(self) -> None:
        store = self.store()
        self.add(store)
        store._read_index = lambda folder: (_ for _ in ()).throw(RuntimeError(MARKER))
        with self.assertLogs('recursion_core.keys.store', 'WARNING') as logged, self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'store_failed')
        self.assertIsNone(refused.exception.__context__)
        self.assertNotIn(MARKER, repr(refused.exception))
        self.assertEqual(logged.output, ['WARNING:recursion_core.keys.store:the key store failed for codex-key: RuntimeError',
                                         'WARNING:recursion_core.keys.store:the key store refused codex-key: store_failed'])


@needs_posix
class RouteTest(TempStoreCase):
    """The brief's done-when lines, through the real adapters and a fake launcher."""

    def setUp(self) -> None:
        super().setUp()
        self.keys = self.store()
        self.add(self.keys, key_id='codex-key', value=MARKER, env='CODEX_API_KEY')
        self.add(self.keys, key_id='claude-key', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')

    def claude(self, credentials, conn='claude-key'):
        h = Harness([claude_ok('keyed')], credentials)
        return h, h.handle(request(kind='api_key', conn_id=conn, credential_env='ANTHROPIC_API_KEY'))

    def codex(self, credentials, conn='codex-key'):
        h = Harness([codex_script('keyed')], credentials)
        return h, h.handle(request(engine='codex', model='gpt-5', kind='api_key', conn_id=conn,
                                   credential_env='CODEX_API_KEY'))

    def test_with_nothing_registered_both_routes_refuse_as_at_the_base(self) -> None:
        for run, conn in ((self.claude, 'claude-key'), (self.codex, 'codex-key')):
            h, res = run(no_custody)
            self.assertEqual((res['status'], res['code'], res['detail']), ('error', c.LAUNCH_REFUSED,
                                                                            f'no credential for {conn}'))
            self.assertEqual(h.launcher.calls, [])

    def test_with_the_store_the_key_reaches_the_child_and_nothing_else(self) -> None:
        for run, name, value in ((self.claude, 'ANTHROPIC_API_KEY', OTHER), (self.codex, 'CODEX_API_KEY', MARKER)):
            with self.subTest(env=name):
                h, res = run(self.keys.credential)
                self.assertEqual((res['status'], res['text']), ('ok', 'keyed'))
                call = h.launcher.calls[0]
                self.assertEqual(call['env'][name], value)
                self.assertNotIn(value, json.dumps(call['argv']))
                self.assertNotIn(value, json.dumps(res))
                sent = json.dumps(h.launcher.processes[0].sent)
                self.assertNotIn(value, sent)          # never in the prompt or any message to the engine

    def test_an_unknown_id_refuses_exactly_as_the_base(self) -> None:
        for run, conn in ((self.claude, 'nobody'), (self.codex, 'nobody')):
            _, base = run(no_custody, conn)
            h, res = run(self.keys.credential, conn)
            self.assertEqual(res, base)
            self.assertEqual(h.launcher.calls, [])

    def test_a_key_saved_for_another_variable_refuses(self) -> None:
        h, res = self.codex(self.keys.credential, conn='claude-key')
        self.assertEqual((res['code'], res['detail']), (c.LAUNCH_REFUSED, 'no credential for claude-key'))
        self.assertEqual(h.launcher.calls, [])

    def test_a_key_for_an_unpublished_vendor_is_never_injected(self) -> None:
        # The key's variable is one an existing engine reads; the route names its id; it is still refused.
        self.add(self.keys, key_id='other-key', value=MARKER + 'z', env='ANTHROPIC_API_KEY', provider='mistral')
        h, res = self.claude(self.keys.credential, conn='other-key')
        self.assertEqual((res['status'], res['code'], res['detail']), ('error', c.LAUNCH_REFUSED,
                                                                        'no credential for other-key'))
        self.assertEqual(h.launcher.calls, [])

    def test_no_spend_opt_in_refuses_before_launch(self) -> None:
        self.keys.remove('codex-key')
        self.add(self.keys, key_id='codex-key', value=MARKER, env='CODEX_API_KEY', cap=0)
        h, res = self.codex(self.keys.credential)
        self.assertEqual((res['status'], res['code'], res['detail']), ('error', c.LAUNCH_REFUSED,
                                                                        'no credential for codex-key'))
        self.assertEqual(h.launcher.calls, [])
