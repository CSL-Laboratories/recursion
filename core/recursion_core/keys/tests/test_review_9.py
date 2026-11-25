"""Which keychain holds a key: a keychain that does not say, the record's own save as the proof, and forget.

A keychain whose ``Created`` is 0 says nothing about which keychain it is, so two of them are never taken for one. A
record's own save, found in the keychain that answers, shows that keychain holds it whatever its identifier says;
nothing else of the id counts. ``forget`` refuses while the key is still where ``remove`` deletes it. Every sentence
says where the value is and the one thing a person can do. Stand-in keychains only (``support.FakeKeychain`` and the
stand-in ``busctl``); every key is made up; no test reaches a real keychain, bus or terminal.
"""
from __future__ import annotations

import io

from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys import system as sysmod
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (LOGIN, LOGIN_CREATED, OTHER, FakeSystem, bus_searches, file_system,
                                               needs_posix)
from recursion_core.keys.tests.test_review_8 import SAVED, SESSION, Case, Terminal


def tool_actions(system) -> list:
    """What the stand-in ``secret-tool`` was asked to do, in order."""
    return [argv[1] for argv, _ in system.calls if argv[0].endswith('secret-tool')]


class Round(Case):
    def refused(self, system, code, *, held=1, key_id='codex-key', chain=None) -> str:
        before = (self.root / 'index.json').read_bytes()
        with self.assertRaises(KeyStoreError) as caught:
            self.store(system).remove(key_id)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.root / 'index.json').read_bytes(), before)       # nothing was changed
        self.assertEqual(len((chain or self.chain).items), held)                # the value stays where it was saved
        return caught.exception.detail

    def sharing(self, collection):
        """A keychain that says it is ``collection`` and holds the very items of the one the key was saved into."""
        system, chain = self.keychain(collection)
        chain.items = self.chain.items
        return system, chain

    def file_store(self) -> KeyStore:
        return KeyStore(self.base / 'file-config' / 'keys', system=file_system(), clock=lambda: SAVED + 0.25)

    def run_main(self, store, *argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(list(argv), out=out, err=err, store=store, **kw)
        return status, out.getvalue(), err.getvalue()


@needs_posix
class CreatedZeroTest(Round):
    def test_a_creation_time_of_zero_says_nothing_about_which_keychain_it_is(self) -> None:
        def busctl(argv, input):
            return Completed(0, f'o "{LOGIN}"\n'.encode() if 'ReadAlias' in argv else b't 0\n', b'')

        def dbus_send(argv, input):
            if any('ReadAlias' in word for word in argv):
                return Completed(0, f'method return time=1\n   object path "{LOGIN}"\n'.encode(), b'')
            return Completed(0, b'method return time=1\n   variant       uint64 0\n', b'')
        self.assertIsNone(sysmod.keychain_identity(FakeSystem(tools={'busctl': busctl})))
        self.assertIsNone(sysmod.keychain_identity(FakeSystem(tools={'dbus-send': dbus_send})))
        doc = self.index()
        doc['keys']['codex-key']['keychain'] = {'collection': LOGIN, 'created': 0}     # no save writes it
        self.put_index(doc)
        with self.assertRaises(KeyStoreError) as caught:
            self.store(self.system).list()
        self.assertEqual(caught.exception.code, 'index_invalid')

    def test_two_keychains_that_both_say_zero_are_not_taken_for_one(self) -> None:
        first, chain = self.keychain((LOGIN, 0))
        self.add(self.store(first), key_id='zero-key', value=OTHER)
        self.assertNotIn('keychain', self.index()['keys']['zero-key'])           # the service did not say which
        second, empty = self.keychain((LOGIN, 0))
        detail = self.refused(second, 'keychain_unrecorded', key_id='zero-key', chain=chain)
        self.assertEqual(empty.items, {})
        self.assertIn('"key forget zero-key"', detail)
        self.store(first).remove('zero-key')                                     # where its own save is, it goes
        self.assertEqual((chain.items, set(self.store(first).list().keys)), ({}, {'codex-key'}))


@needs_posix
class OwnSaveProofTest(Round):
    def test_a_moved_default_alias_with_the_own_save_there_removes_the_key(self) -> None:
        system, _ = self.sharing((SESSION, LOGIN_CREATED + 5))
        report = []
        self.assertEqual(self.store(system).remove('codex-key', report=report).key_id, 'codex-key')
        self.assertEqual((report, self.chain.items, self.store(system).list().keys), ([], {}, {}))

    def test_a_keychain_whose_created_moved_with_the_own_save_there_removes_the_key(self) -> None:
        system, _ = self.sharing((LOGIN, SAVED + 60))
        self.store(system).remove('codex-key')
        self.assertEqual((self.chain.items, self.store(system).list().keys), ({}, {}))

    def test_another_empty_keychain_is_only_asked_whether_it_holds_the_save(self) -> None:
        system, other = self.keychain((SESSION, LOGIN_CREATED + 5))
        self.refused(system, 'keychain_other')
        self.assertTrue(bus_searches(system))                                   # asked over the bus by attributes
        self.assertEqual(tool_actions(system), [])                              # nothing that reads or changes an item
        self.assertEqual(other.items, {})

    def test_a_record_naming_no_keychain_counts_only_its_own_save(self) -> None:
        doc = self.index()
        doc['keys']['codex-key'].pop('keychain')                       # a record saved before records named one
        self.put_index(doc)
        system, other = self.keychain((SESSION, 1))
        (saved,) = self.chain.items
        words = list(saved)
        words[words.index('recursion_save') + 1] = 'f' * 32            # the same store and id, another save
        other.items[tuple(words)] = OTHER.encode()
        self.refused(system, 'keychain_unrecorded')
        self.assertEqual(len(other.items), 1)                          # the other save's item stays
        self.assertNotIn('clear', tool_actions(system))

    def test_status_and_the_audit_take_the_own_save_as_the_keychain_it_was_saved_into(self) -> None:
        system, _ = self.sharing((SESSION, LOGIN_CREATED + 5))
        self.assertIn('codex-key  saved into: the keychain that answers now', self.main('status', system=system)[1])
        self.assertEqual(audit_mod.run(self.store(system), journal=False).elsewhere, {})


@needs_posix
class ForgetOnlyWhenOutOfReachTest(Round):
    def forget_refused(self, store, key_id='codex-key') -> str:
        before = (store.root / 'index.json').read_bytes()
        with self.assertRaises(KeyStoreError) as caught:
            store.forget(key_id)
        self.assertEqual(caught.exception.code, 'value_reachable')
        self.assertEqual((store.root / 'index.json').read_bytes(), before)    # the record stays
        return caught.exception.detail

    def test_forget_refuses_while_the_keychain_it_was_saved_into_answers(self) -> None:
        detail = self.forget_refused(self.store(self.system))
        self.assertIn('"key remove codex-key"', detail)
        self.assertNotIn('another keychain', detail)
        self.assertEqual(len(self.chain.items), 1)

    def test_forget_refuses_while_its_own_save_is_in_the_keychain_that_answers(self) -> None:
        system, _ = self.sharing((SESSION, LOGIN_CREATED + 5))
        self.assertIn('"key remove codex-key"', self.forget_refused(self.store(system)))
        self.assertEqual(tool_actions(system), [])

    def test_forget_refuses_a_key_in_the_owner_only_file(self) -> None:
        store = self.file_store()
        self.add(store, key_id='file-key')
        detail = self.forget_refused(store, 'file-key')
        self.assertIn('owner-only file', detail)
        self.assertIn('"key remove file-key"', detail)

    def test_the_command_says_why_and_the_record_stays(self) -> None:
        status, out, err = self.main('forget', 'codex-key', terminal=Terminal(['codex-key']))
        self.assertEqual((status, out), (1, ''))
        self.assertIn('refused: value_reachable: codex-key was saved into the keychain that answers now', err)
        self.assertIn('codex-key', self.store(self.system).list().keys)

    def test_where_another_empty_keychain_answers_forget_says_where_the_value_may_be(self) -> None:
        other, chain = self.keychain((SESSION, 1))
        status, out, err = self.main('forget', 'codex-key', terminal=Terminal(['codex-key']), system=other)
        self.assertEqual((status, err), (0, ''))
        self.assertIn('no keychain that answers here holds it', out)
        self.assertIn('its value may still sit in another keychain', out)
        self.assertEqual((chain.items, len(self.chain.items)), ({}, 1))
        self.assertEqual(tool_actions(other), [])

    def test_with_no_keychain_answering_at_all_forget_is_the_way_out(self) -> None:
        self.assertEqual(self.store(file_system()).forget('codex-key').key_id, 'codex-key')
        self.assertEqual(len(self.chain.items), 1)


@needs_posix
class TheGuaranteesBiteTest(Round):
    def test_another_keychain_made_after_the_save_is_another_one_not_a_remade_one(self) -> None:
        system, _ = self.keychain((SESSION, SAVED + 60))
        self.assertNotIn('forget', self.refused(system, 'keychain_other'))

    def test_the_audit_is_incomplete_while_a_remade_keychain_answers(self) -> None:
        remade, _ = self.keychain((LOGIN, SAVED + 60))
        report = audit_mod.run(self.store(remade), journal=False)
        self.assertEqual(report.elsewhere, {'codex-key': 'remade'})
        self.assertFalse(report.complete())
        status, out, _ = self.main('audit', '--no-journal', system=remade)
        self.assertEqual(status, 4)
        self.assertIn('keychain of codex-key: a keychain the one answering now was made after', out)
        # A remade keychain without the key's save also leaves its value unread (``missing``), so the rule is pinned
        # on the report itself as well: a remade keychain alone makes it incomplete, as another one does.
        self.assertTrue(audit_mod.Report().complete())
        for place in ('remade', 'other'):
            self.assertFalse(audit_mod.Report(elsewhere={'codex-key': place}).complete(), place)


@needs_posix
class TheWordsSayWhereTest(Round):
    def test_an_entry_already_gone_from_the_owner_only_file_is_named_as_the_file(self) -> None:
        store = self.file_store()
        self.add(store, key_id='file-key')
        kept = (store.root / 'index.json').read_bytes()
        store.remove('file-key')
        (store.root / 'index.json').write_bytes(kept)                  # an index put back: the entry is gone
        status, out, err = self.run_main(store, 'remove', 'file-key', '--yes')
        self.assertEqual((status, err), (0, ''))
        self.assertIn('note: the value of file-key was already gone from the owner-only file', out)
        self.assertNotIn('keychain', out)

    def test_another_keychain_is_named_with_the_one_step_a_person_can_take(self) -> None:
        system, _ = self.keychain((SESSION, 1))
        detail = self.refused(system, 'keychain_other')
        self.assertIn('the keychain "login" made on 2023-11-14 (UTC)', detail)
        self.assertIn('"key remove codex-key" where that keychain answers', detail)
        self.assertNotIn(LOGIN, detail)
        self.assertNotIn('forget', detail)

    def test_a_remade_keychain_says_it_holds_none_of_the_key(self) -> None:
        system, _ = self.keychain((LOGIN, SAVED + 60))
        detail = self.refused(system, 'keychain_remade')
        self.assertIn('holds no item of codex-key', detail)
        self.assertIn('"key forget codex-key"', detail)


if __name__ == '__main__':
    import unittest
    unittest.main()
