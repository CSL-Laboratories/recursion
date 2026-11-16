"""The third reads of pull request 2 (#422): DeepSeek V4.1 Flash's F1 to F3 and F7. Every key is made up; the macOS
tests use the stand-in ``security`` of ``test_macos.py``."""
from __future__ import annotations

import io
import json

from recursion_core.keys import __main__ as entry
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (
    OTHER,
    FakeSystem,
    TempStoreCase,
    needs_posix,
)
from recursion_core.keys.tests.test_macos import (
    ELSEWHERE,
    FakeSecurity,
    fixed_clock,
)
from recursion_core.keys.tests.test_pr2_review_1 import SpyStdin, SpyTerminal

ADD = ('--provider', 'openai', '--env', 'CODEX_API_KEY')


class MacCase(TempStoreCase):
    def mac(self, name='keys'):
        security = FakeSecurity()
        system = FakeSystem(platform='darwin', tools={'security': security})
        return KeyStore(self.base / name / 'keys', system=system, clock=fixed_clock), system, security

    @staticmethod
    def unrecord(store) -> None:
        """The index as every head before 594e3f4d wrote it: no ``keychain``."""
        path = store.root / 'index.json'
        doc = json.loads(path.read_text())
        del doc['keychain']
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=True) + '\n')


@needs_posix
class KeyAddAsksTheKeychainFirstTest(MacCase):
    """DeepSeek r3 F2: a keychain the store refuses is refused before ``key add`` reads the key."""

    def test_recorded_or_not_a_moved_login_keychain_asks_for_nothing(self) -> None:
        for recorded in (True, False):
            with self.subTest(recorded=recorded):
                store, _, security = self.mac(f'moved-{recorded}')
                self.add(store)
                if not recorded:
                    self.unrecord(store)
                security.login = ELSEWHERE
                for extra in ((), ('--allow-metered-spend', '--cap-usd', '5', '--stdin')):
                    terminal, stdin, out, err = SpyTerminal(), SpyStdin(), io.StringIO(), io.StringIO()
                    status = entry.main(['add', 'second', *ADD, *extra], out=out, err=err, store=store,
                                        terminal=terminal, stdin=stdin)
                    self.assertEqual(status, 1)
                    self.assertTrue(err.getvalue().startswith('refused: keychain_unavailable: '), err.getvalue())
                    self.assertEqual((terminal.prompts, stdin.reads), ([], 0))
                    self.assertEqual(store.list().keys.keys(), {'codex-key'})


@needs_posix
class FirstHeadItemCutShortTest(MacCase):
    """DeepSeek r3 F3: an item in the first head's service format (``recursion.<save id>``) that the listing printed
    without its generic attribute and comment makes the listing incomplete, as one in the current format does."""

    def test_it_is_refused(self) -> None:
        store, system, security = self.mac()
        record = self.add(store)
        security.items[0]['svce'] = f'recursion.{record.save_id}'
        listing = security.dump()
        cut = b'\n'.join(line for line in listing.splitlines()
                         if not line.startswith((b'    "gena"', b'    "icmt"')))

        def tool(argv, input, cut=cut, security=security):
            return Completed(0, cut, b'') if argv[1] == 'dump-keychain' else security(argv, input)

        system.tools['security'] = tool
        with self.assertRaises(KeyStoreError) as caught:
            store.unnamed_items()
        self.assertEqual(caught.exception.code, 'keychain_failed')


@needs_posix
class RecordedKeychainReplacementCharTest(MacCase):
    """DeepSeek r3 F7: a recorded keychain holding U+FFFD is refused, as ``_login_path`` refuses such a path."""

    def test_it_is_index_invalid(self) -> None:
        store, _, _ = self.mac()
        self.add(store)
        path = store.root / 'index.json'
        doc = json.loads(path.read_text())
        doc['keychain'] = '/Users/someone/�/login.keychain-db'
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=True) + '\n')
        with self.assertRaises(KeyStoreError) as caught:
            store.list()
        self.assertEqual(caught.exception.code, 'index_invalid')


@needs_posix
class RecordedKeychainReachesEveryReadTest(MacCase):
    """Codex sol r3, change 2: every read the store makes on a macOS store is bound to the recorded keychain, so a
    copy of an item in another login keychain is never read (sol's N14, N15, N16)."""

    def moved_with_a_copy(self):
        store, _, security = self.mac()
        self.add(store)
        security.items.append(dict(security.items[0], keychain=ELSEWHERE, password=OTHER.encode()))
        security.login = ELSEWHERE
        return store

    def test_a_credential_is_refused(self) -> None:
        with self.assertRaises(KeyRefused) as refused:
            self.moved_with_a_copy().credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'keychain_unavailable')

    def test_the_audits_values_are_refused(self) -> None:
        found, missing = self.moved_with_a_copy().values()
        self.assertEqual((found, missing), ({}, {'codex-key': 'keychain_unavailable'}))

    def test_the_unnamed_count_is_refused(self) -> None:
        with self.assertRaises(KeyStoreError) as caught:
            self.moved_with_a_copy().unnamed_items()
        self.assertEqual(caught.exception.code, 'keychain_unavailable')

    def test_a_mixed_listing_with_a_first_head_item_cut_short_is_refused(self) -> None:
        store, system, security = self.mac('mixed')
        record = self.add(store)
        self.add(store, key_id='second', value=OTHER)
        security.items[0]['svce'] = f'recursion.{record.save_id}'
        original = security.__call__

        def tool(argv, input):
            done = original(argv, input)
            if argv[1] == 'dump-keychain':
                return Completed(0, done.stdout.replace(b'    "icmt"<blob>="openai"\n', b'', 1), b'')
            return done

        system.tools['security'] = tool
        with self.assertRaises(KeyStoreError) as caught:
            store.unnamed_items()
        self.assertEqual(caught.exception.code, 'keychain_failed')
