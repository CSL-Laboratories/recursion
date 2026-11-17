"""The fifth reads of pull request 2 (#422): DeepSeek V4.1 Flash's F1 and Codex sol's change 1, one point. Every key
is made up; the macOS tests use the stand-in ``security`` of ``test_macos.py``."""
from __future__ import annotations

import io

from recursion_core.keys import __main__ as entry
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.system import ToolMissing, ToolTimeout
from recursion_core.keys.tests.support import FakeKeychain, busctl, needs_posix
from recursion_core.keys.tests.test_macos import ELSEWHERE, LOGIN
from recursion_core.keys.tests.test_pr2_review_3 import MacCase

ADVICE = ('unfinished remove of codex-key: its value may still be in the store; "key remove codex-key" clears it, or '
          'names the refusal that stops it')


@needs_posix
class AuditAdviceFollowsEveryRefusalTest(MacCase):
    """DeepSeek r5 F1, Codex sol r5 change 1, as DeepSeek r6 F1 and F2 left them: for an unfinished operation, ``key
    audit`` names the refusal its count met, whatever the refusal (a keychain code or not), and says that ``key
    remove`` clears the value or names the refusal that stops it; beside a backend that refuses, the removal refuses
    with that code. An answering keychain's removal clears it."""

    def unfinished(self, name):
        store, system, security = self.mac(name)
        self.add(store)
        security.login = ELSEWHERE
        with self.assertRaises(KeyStoreError):
            store.remove('codex-key')                       # refused: its intent stays, unfinished
        security.login = LOGIN
        return store, system, security

    def audit(self, store) -> tuple:
        out = io.StringIO()
        status = entry.main(['audit', '--no-journal'], out=out, err=io.StringIO(), store=store)
        return status, out.getvalue()

    def test_a_refusing_backend_is_named_and_the_removal_refuses_with_it(self) -> None:
        for failure, code in (('locked', 'keychain_locked'), ('dump_error', 'keychain_failed'),
                              ('fails', 'keychain_failed'), ('timeout', 'keychain_timeout'),
                              ('missing', 'keychain_tool_missing'), ('backend_changed', 'backend_changed')):
            with self.subTest(failure=failure):
                store, system, security = self.unfinished(failure)
                if failure in ('locked', 'dump_error', 'fails'):
                    security.mode = failure
                elif failure == 'backend_changed':              # the host now offers the Secret Service
                    system.platform, system.bus = 'linux', 'synthetic-bus'
                    system.tools['busctl'], system.tools['secret-tool'] = busctl(owner=True), FakeKeychain()
                else:
                    error = ToolTimeout('synthetic timeout') if failure == 'timeout' else ToolMissing('synthetic')

                    def unavailable(argv, input, error=error):
                        raise error

                    system.tools['security'] = unavailable
                status, text = self.audit(store)
                self.assertEqual(status, 4)
                self.assertIn(ADVICE, text)
                self.assertIn(f'keychain: its items were not counted ({code})', text)
                with self.assertRaises(KeyStoreError) as caught:
                    store.remove('codex-key')
                self.assertEqual(caught.exception.code, code)
                index = store.list()
                self.assertEqual((sorted(index.keys), sorted(index.pending)), (['codex-key'], ['codex-key']))

    def test_an_answering_keychain_gets_the_advice(self) -> None:
        store, _, security = self.unfinished('answering')
        status, text = self.audit(store)
        self.assertEqual(status, 4)
        self.assertIn('unfinished remove of codex-key: its value may still be in the store; "key remove codex-key" '
                      'clears it', text)
        store.remove('codex-key')
        self.assertEqual((store.list().keys, store.list().pending, security.items), ({}, {}, []))
