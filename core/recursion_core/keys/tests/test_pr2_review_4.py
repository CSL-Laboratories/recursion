"""The fourth reads of pull request 2 (#422): DeepSeek V4.1 Flash's R1, R2, R5 and R6, and Codex sol's changes 1 to 4.
Every key is made up; the macOS tests use the stand-in ``security`` of ``test_macos.py``."""
from __future__ import annotations

import io
from dataclasses import replace

from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import Pending, index_bytes
from recursion_core.keys.tests.support import OTHER, needs_posix
from recursion_core.keys.tests.test_macos import ELSEWHERE, LOGIN
from recursion_core.keys.tests.test_pr2_review_1 import SpyStdin, SpyTerminal
from recursion_core.keys.tests.test_pr2_review_3 import ADD, MacCase

BETA = {'key_id': 'beta', 'value': OTHER, 'env': 'ANTHROPIC_API_KEY', 'provider': 'anthropic'}


@needs_posix
class UnrecordedIndexIsRefusedTest(MacCase):
    """DeepSeek r4 R1 (HIGH), Codex sol r3 change 1: an index pinned to the macOS Keychain that records no keychain is
    never matched to one, since a copy of an item in another keychain lists as the original does. Every operation that
    reaches the keychain is refused ``keychain_unavailable``, beside its own keychain too, and ``security`` is asked
    nothing: no value is read, nothing is removed, and the record and any intent are kept. This replaces round 3's
    ``UnrecordedIndexTest``, whose rule matched such an index by any item of the store."""

    def arranged(self, place):
        store, system, security = self.mac(place.replace(' ', '-'))
        self.add(store)
        if place == 'an item of the store under another id':
            self.add(store, **BETA)
        self.unrecord(store)
        if place == 'a copy of the item':     # sol r4's R15
            security.items.append(dict(security.items[0], keychain=ELSEWHERE, password=OTHER.encode()))
        elif place == 'an item of the store under another id':
            security.items[1]['keychain'] = ELSEWHERE
        elif place == "only another store's item":
            security.items.append(dict(security.items[0], keychain=ELSEWHERE, gena='f' * 32, password=OTHER.encode()))
        if place != 'its own keychain':
            security.login = ELSEWHERE                   # the keychain that holds it is the login keychain now
        calls, tool = [], system.tools['security']

        def spy(argv, input, tool=tool):
            calls.append(argv[1])
            return tool(argv, input)

        system.tools['security'] = spy
        return store, security, calls

    def test_every_operation_is_refused_wherever_the_login_keychain_is(self) -> None:
        for place in ('its own keychain', 'a copy of the item', 'an item of the store under another id',
                      "only another store's item", 'none of its items'):
            with self.subTest(place=place):
                store, security, calls = self.arranged(place)
                items, keys = [dict(item) for item in security.items], sorted(store.list().keys)
                for key_id in keys:
                    env = 'ANTHROPIC_API_KEY' if key_id == 'beta' else 'CODEX_API_KEY'
                    with self.assertRaises(KeyRefused) as refused:
                        store.credential(key_id, env)
                    self.assertEqual(refused.exception.code, 'keychain_unavailable')
                self.assertEqual(store.values(), ({}, dict.fromkeys(keys, 'keychain_unavailable')))
                for call in (store.unnamed_items, lambda store=store: self.add(store, key_id='second', value=OTHER),
                             lambda store=store: store.check_add('second', 'openai', 'CODEX_API_KEY'),
                             lambda store=store: store.remove('codex-key')):
                    with self.assertRaises(KeyStoreError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, 'keychain_unavailable')
                index = store.list()
                self.assertEqual((sorted(index.keys), index.keychain), (keys, None))
                self.assertEqual(index.pending['codex-key'].op, 'remove')     # the refused removal's intent: kept
                self.assertFalse(audit_mod.run(store, journal=False).complete())
                self.assertEqual((security.items, calls), (items, []))

    def test_an_unfinished_add_keeps_its_intent(self) -> None:
        store, security, calls = self.arranged('a copy of the item')
        started = store.list().keys['codex-key'].created_at
        unfinished = replace(store.list(), keys={}, pending={'codex-key': Pending('codex-key', 'add', started)})
        (store.root / 'index.json').write_bytes(index_bytes(unfinished))
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'keychain_unavailable')
        self.assertEqual((store.list().pending, calls, len(security.items)), (unfinished.pending, [], 2))

    def test_key_audit_does_not_advise_a_removal_the_keychain_refuses(self) -> None:
        store, _, _ = self.arranged('its own keychain')
        with self.assertRaises(KeyStoreError):
            store.remove('codex-key')
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(entry.main(['audit', '--no-journal'], out=out, err=err, store=store), 4)
        self.assertNotIn('"key remove codex-key" clears it', out.getvalue())
        self.assertIn('unfinished remove of codex-key: its value may still be in the store; its keychain is refused '
                      '(keychain_unavailable), so "key remove codex-key" cannot clear it now', out.getvalue())


@needs_posix
class RemovalFinishedAfterItsLastWriteFailedTest(MacCase):
    """DeepSeek r4 R2: a removal whose last index write failed after its delete (or whose process died there) leaves
    the record, its intent and the recorded keychain, and the next removal finishes it."""

    def test_the_next_removal_finishes_it(self) -> None:
        store, _, security = self.mac()
        self.add(store)
        original, writes = store._write_index, []

        def write(folder, index):
            writes.append(index)
            if len(writes) == 2:
                raise OSError(28, 'No space left on device')
            return original(folder, index)

        store._write_index = write
        with self.assertRaises(OSError):
            store.remove('codex-key')
        del store._write_index
        index = store.list()
        self.assertEqual((sorted(index.keys), index.pending['codex-key'].op, index.keychain, security.items),
                         (['codex-key'], 'remove', LOGIN, []))
        store.remove('codex-key')
        self.assertEqual((store.list().keys, store.list().pending, security.items), ({}, {}, []))


@needs_posix
class IndexWriterTest(MacCase):
    """DeepSeek r4 R5: the index is never written with a ``keychain`` its reader refuses: an unreadable one beside a
    macOS pin is refused, and one beside another backend is left out."""

    def test_a_keychain_the_reader_refuses_is_never_written(self) -> None:
        store, _, _ = self.mac()
        self.add(store)
        index = store.list()
        self.assertEqual(index_bytes(index), (store.root / 'index.json').read_bytes())
        for bad in ('', 'login.keychain-db', '/Users/someone/"/login.keychain-db', '/Users/someone/�.keychain-db'):
            with self.subTest(keychain=bad), self.assertRaises(KeyStoreError) as caught:
                index_bytes(replace(index, keychain=bad))
            self.assertEqual(caught.exception.code, 'index_invalid')
        self.assertNotIn(b'"keychain"', index_bytes(replace(index, backend='secret_service')))


@needs_posix
class UnfinishedOperationAsksTheKeychainFirstTest(MacCase):
    """Codex sol r4 change 3: ``key add`` of an id with an unfinished operation, on a store whose login keychain moved,
    is refused before it prompts or reads standard input, and the index is unchanged (sol's R04, R12)."""

    def check(self, store, extra) -> None:
        before = (store.root / 'index.json').read_bytes()
        terminal, stdin, out, err = SpyTerminal(), SpyStdin(), io.StringIO(), io.StringIO()
        status = entry.main(['add', 'codex-key', *ADD, *extra], out=out, err=err, store=store, terminal=terminal,
                            stdin=stdin)
        self.assertEqual(status, 1)
        self.assertTrue(err.getvalue().startswith('refused: keychain_unavailable: '), err.getvalue())
        self.assertEqual((terminal.prompts, stdin.reads, (store.root / 'index.json').read_bytes()), ([], 0, before))

    def test_an_unfinished_add(self) -> None:
        for extra in ((), ('--allow-metered-spend', '--cap-usd', '5', '--stdin')):
            with self.subTest(extra=extra):
                store, _, security = self.mac(f'add-{len(extra)}')
                security.mode = 'writes_then_fails'
                with self.assertRaises(KeyStoreError):
                    self.add(store)
                security.mode, security.login = 'ok', ELSEWHERE
                self.assertEqual(store.list().pending['codex-key'].op, 'add')
                self.check(store, extra)

    def test_an_unfinished_remove(self) -> None:
        store, _, security = self.mac()
        self.add(store)
        security.login = ELSEWHERE
        with self.assertRaises(KeyStoreError):
            store.remove('codex-key')
        self.check(store, ('--allow-metered-spend', '--cap-usd', '5', '--stdin'))
