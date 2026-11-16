"""The first reads of pull request 2 (#422): DeepSeek V4.1 Flash's L1, L2, L3 and I1, and Codex astra's later
item from #386's sixth reads (the owner-file shortcut of the unnamed count). Every key is made up."""
from __future__ import annotations

import io
import unittest

from recursion_core.control.methods import key as key_methods
from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import Index, KeyStore, index_bytes
from recursion_core.keys.tests.support import (
    MARKER,
    FakeSystem,
    TempStoreCase,
    keychain_system,
    needs_posix,
)

ADD = ('--provider', 'openai', '--env', 'CODEX_API_KEY')


class SpyTerminal:
    """A terminal that counts every question; a command that will be refused must ask none."""

    def __init__(self) -> None:
        self.prompts = []

    def read_secret(self, prompt):
        self.prompts.append(prompt)
        return MARKER.encode()

    def ask(self, prompt):
        self.prompts.append(prompt)
        return 'n'


class SpyStdin(io.BytesIO):
    """Standard input that is not a terminal and counts its reads."""

    def __init__(self) -> None:
        super().__init__(MARKER.encode())
        self.reads = 0

    def isatty(self):
        return False

    def read(self, *args):
        self.reads += 1
        return super().read(*args)


@needs_posix
class AddAsksForNoKeyItWouldRefuseTest(TempStoreCase):
    """DeepSeek L1 (LOW): ``key add`` read the key before it refused a bad ``--provider`` or ``--env``. Every refusal
    that needs no value now comes before the key is read, from the terminal or with ``--stdin``."""

    def stores(self):
        saved = KeyStore(self.base / 'saved' / 'keys', system=FakeSystem())
        self.add(saved)
        garbage = KeyStore(self.base / 'garbage' / 'keys', system=FakeSystem())
        self.add(garbage)
        (garbage.root / 'index.json').write_bytes(b'{')
        unbound = KeyStore(self.base / 'unbound' / 'keys', system=FakeSystem())
        self.add(unbound)
        store_id = unbound.list().store_id
        (unbound.root / 'index.json').write_bytes(index_bytes(Index(None, {}, {}, store_id)))     # emptied
        fresh = KeyStore(self.base / 'fresh' / 'keys', system=FakeSystem())
        system, _ = keychain_system()
        chain = KeyStore(self.base / 'chain' / 'keys', system=system)
        self.add(chain)                                              # saved in a keychain: no file to fall back on
        emptied = KeyStore(self.base / 'winfolder' / 'keys', system=FakeSystem())
        self.add(emptied)
        emptied.remove('codex-key')                                  # the folder stays, with no key in it
        return [
            ('provider_invalid', fresh, ('codex-key', '--provider', 'BAD PROVIDER', '--env', 'CODEX_API_KEY')),
            ('env_name_invalid', fresh, ('codex-key', '--provider', 'openai', '--env', 'not a name')),
            ('key_exists', saved, ('codex-key', *ADD)),
            ('key_exists', unbound, ('codex-key', *ADD)),            # a value the file holds and no record names
            ('key_exists', chain, ('codex-key', *ADD)),
            ('index_invalid', garbage, ('other-key', *ADD)),
            ('backend_not_built', KeyStore(self.base / 'win' / 'keys', system=FakeSystem(platform='win32')),
             ('codex-key', *ADD)),
            ('keychain_tool_missing', KeyStore(self.base / 'mac' / 'keys', system=FakeSystem(platform='darwin')),
             ('codex-key', *ADD)),
            ('backend_not_built', KeyStore(emptied.root, system=FakeSystem(platform='win32')), ('codex-key', *ADD)),
        ]

    def test_each_refusal_that_needs_no_value_comes_before_the_key_is_read(self) -> None:
        for code, store, argv in self.stores():
            for how in ('terminal', '--stdin'):
                with self.subTest(code=code, how=how, store=store.root.parent.name):
                    terminal, stdin = SpyTerminal(), SpyStdin()
                    out, err = io.StringIO(), io.StringIO()
                    extra = ('--stdin',) if how == '--stdin' else ()
                    status = entry.main(['add', *argv, *extra], out=out, err=err, store=store, terminal=terminal,
                                        stdin=stdin)
                    self.assertEqual(status, 1)
                    self.assertTrue(err.getvalue().startswith(f'refused: {code}: '), err.getvalue())
                    self.assertEqual((terminal.prompts, stdin.reads), ([], 0))
                    self.assertNotIn(MARKER, out.getvalue() + err.getvalue())

    def test_check_add_changes_nothing_and_add_still_checks(self) -> None:
        store = KeyStore(self.base / 'quiet' / 'keys', system=FakeSystem())
        store.check_add('codex-key', 'openai', 'CODEX_API_KEY')
        self.assertFalse((self.base / 'quiet').exists())            # no folder is made
        self.add(store)
        before = (store.root / 'index.json').read_bytes()
        with self.assertRaises(KeyStoreError) as caught:
            store.check_add('codex-key', 'openai', 'CODEX_API_KEY')
        self.assertEqual(caught.exception.code, 'key_exists')
        self.assertEqual((store.root / 'index.json').read_bytes(), before)
        with self.assertRaises(KeyStoreError) as caught:            # add refuses alone, as before
            store.add('codex-key', 'BAD PROVIDER', 'CODEX_API_KEY', MARKER)
        self.assertEqual(caught.exception.code, 'provider_invalid')


@needs_posix
class NulInTheFolderPathTest(TempStoreCase):
    """DeepSeek L2 (LOW): a folder path holding a NUL byte made ``list``, ``spend_policy`` and ``describe`` raise
    ``ValueError``. Every command now refuses it ``folder_invalid``, and ``describe`` reports it."""

    def test_every_command_refuses_it_typed_and_describe_reports_it(self) -> None:
        before = sorted(self.base.iterdir())
        store = KeyStore(self.base / 'a\0b' / 'keys', system=FakeSystem())
        self.assertEqual(store.describe()['refusal']['code'], 'folder_invalid')
        calls = {
            'list': store.list,
            'add': lambda: store.add('codex-key', 'openai', 'CODEX_API_KEY', MARKER, metered_spend_allowed=True,
                                     cap_usd=5),
            'check_add': lambda: store.check_add('codex-key', 'openai', 'CODEX_API_KEY'),
            'remove': lambda: store.remove('codex-key'),
            'values': store.values,
            'unnamed_items': store.unnamed_items,
        }
        for name, call in calls.items():
            with self.subTest(call=name), self.assertRaises(KeyStoreError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'folder_invalid')
        for name, call in (('spend_policy', lambda: store.spend_policy('codex-key')),
                           ('credential', lambda: store.credential('codex-key', 'CODEX_API_KEY'))):
            with self.subTest(call=name), self.assertRaises(KeyRefused) as refused:
                call()
            self.assertEqual(refused.exception.code, 'folder_invalid')
        self.assertEqual(sorted(self.base.iterdir()), before)        # nothing was made


class KeyListParamsTest(unittest.TestCase):
    """DeepSeek L3 (LOW): parameter names that are not text made ``min`` raise inside ``key.list``."""

    def test_names_that_are_not_text_are_request_invalid(self) -> None:
        for params in ({1: 'a', 'b': 2}, {None: 1, 'x': 2}, {1: 'a'}):
            with self.subTest(params=params):
                self.assertEqual(key_methods.key_list(None, params), {'code': 'request_invalid', 'detail': 'params'})
        self.assertEqual(key_methods.key_list(None, {'b': 1, 'a': 2}), {'code': 'request_invalid', 'detail': 'a'})


@needs_posix
class IdsThatAreNotTextTest(TempStoreCase):
    """DeepSeek I1 (INFO): no test passed an id that is not text, so the guards of ``credential`` and
    ``spend_policy`` were not held. Each is a plain ``KeyError`` with the id, as the Credentials type says."""

    def test_an_id_or_a_variable_name_that_is_not_text_is_a_plain_key_error(self) -> None:
        store = self.store()
        self.add(store)
        for conn in ([], {}, None, 7, b'codex-key'):
            for name, call in (('credential', lambda c: store.credential(c, 'CODEX_API_KEY')),
                               ('spend_policy', store.spend_policy)):
                with self.subTest(conn=conn, call=name), self.assertRaises(KeyError) as caught:
                    call(conn)
                self.assertIs(type(caught.exception), KeyError)
                self.assertEqual(caught.exception.args, (conn,))
        with self.assertRaises(KeyError) as caught:
            store.credential('codex-key', [])
        self.assertIs(type(caught.exception), KeyError)


@needs_posix
class FileStoreCountsNoKeychainTest(TempStoreCase):
    """Codex astra, #386 round 6 (later): ``_unnamed_items``' owner-file shortcut was held by no test. A store whose
    keys are in the owner-only file counts no keychain items, even on a host that now offers a keychain."""

    def test_a_keychain_that_appears_is_not_asked(self) -> None:
        self.add(self.store())
        system, _ = keychain_system()
        later = self.store(system)
        system.calls.clear()
        self.assertIsNone(later.unnamed_items())
        self.assertEqual(system.calls, [])
        report = audit_mod.run(later, journal=False)
        self.assertIsNone(report.unnamed_refused)


class PackageEntryPointsTest(unittest.TestCase):
    """Codex astra R6: the entry points other lanes were promised are the package's own."""

    def test_spend_policy_and_describe_are_exported(self) -> None:
        from recursion_core import keys
        from recursion_core.keys import store

        self.assertIs(keys.spend_policy, store.spend_policy)
        self.assertIs(keys.describe, store.describe)
        self.assertIn('spend_policy', keys.__all__)
        self.assertIn('describe', keys.__all__)


@needs_posix
class OrdinaryFailuresTest(TempStoreCase):
    """Codex sol R6: an ordinary I/O failure underneath the store reaches neither QUOTA nor the doctor as an
    untyped exception, and its text is not repeated."""

    def test_spend_policy_and_describe_answer_an_io_failure_typed(self) -> None:
        import errno
        from unittest import mock

        store = self.store()
        self.add(store)
        failure = OSError(errno.EIO, 'made-up I/O failure text')
        with mock.patch.object(KeyStore, '_read_index', side_effect=failure):
            with self.assertRaises(KeyRefused) as refused:
                store.spend_policy('codex-key')
            described = store.describe()
        self.assertEqual(refused.exception.code, 'store_failed')
        self.assertIsNone(refused.exception.__context__)
        self.assertNotIn('made-up', str(refused.exception))
        self.assertEqual(described['refusal'], {'code': 'store_failed', 'detail': 'OSError errno 5'})
        with self.assertRaises(KeyError) as unknown:                 # an unknown id is still a plain KeyError
            store.spend_policy('nobody')
        self.assertIs(type(unknown.exception), KeyError)
