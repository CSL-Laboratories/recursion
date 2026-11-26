"""Which keychain a key was saved into.

One config folder can meet more than one keychain: another session bus, a container or an ssh session with a keyring
of its own, a keyring wiped and made again. ``remove`` reported success against a second, empty keychain, dropped the
record and left the value in the first with no record naming it. Each record of a keychain save now names its
keychain, ``remove`` tells the cases apart, ``forget`` is the person's own step for one key, and ``status`` and the
audit show both. Stand-in keychains only (``support.FakeKeychain`` and the stand-in ``busctl``); every key is made up;
no test reaches a real keychain, bus or terminal.
"""
from __future__ import annotations

import io
import json

from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys import hooks
from recursion_core.keys import system as sysmod
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (LOGIN, LOGIN_CREATED, OTHER, FakeSystem, TempStoreCase, keychain_system,
                                               needs_posix)

SAVED = 1_763_160_000                                   # the tests' clock (TempStoreCase.store), whole seconds
SESSION = '/org/freedesktop/secrets/collection/session'


class Terminal:
    def __init__(self, answers) -> None:
        self.answers, self.prompts = list(answers), []

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class Case(TempStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.system, self.chain = self.keychain()
        self.add(self.store(self.system))

    def keychain(self, collection=(LOGIN, LOGIN_CREATED)):
        system, chain = keychain_system(environ={'PATH': '/fake/bin', 'HOME': str(self.base)})
        chain.collection = collection
        return system, chain

    def index(self) -> dict:
        return json.loads((self.root / 'index.json').read_bytes())

    def put_index(self, doc) -> None:
        (self.root / 'index.json').write_bytes((json.dumps(doc, indent=1) + '\n').encode())

    def main(self, *argv, system=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(list(argv), out=out, err=err, store=self.store(system or self.system), **kw)
        return status, out.getvalue(), err.getvalue()


@needs_posix
class KeychainRecordTest(Case):
    def test_a_keychain_save_names_its_keychain_and_others_name_none(self) -> None:
        self.assertEqual(self.index()['keys']['codex-key']['keychain'], {'collection': LOGIN, 'created': LOGIN_CREATED})
        self.assertEqual(self.store(self.system).list().keys['codex-key'].keychain, (LOGIN, LOGIN_CREATED))
        self.assertNotIn('keychain', self.store(self.system).list().keys['codex-key'].as_json())   # list --json as before
        self.chain.collection = None                                     # a service that does not say which it is
        self.add(self.store(self.system), key_id='quiet-key', value=OTHER)
        self.assertNotIn('keychain', self.index()['keys']['quiet-key'])

    def test_the_identity_is_read_through_busctl_or_dbus_send_and_nothing_else_is_taken(self) -> None:
        self.assertEqual(sysmod.keychain_identity(self.system), (LOGIN, LOGIN_CREATED))

        def dbus_send(argv, input):
            if any('ReadAlias' in word for word in argv):
                return Completed(0, b'method return time=1 sender=:1.2 -> destination=:1.3 serial=4 reply_serial=2\n'
                                    b'   object path "/org/freedesktop/secrets/collection/login"\n', b'')
            return Completed(0, b'method return time=1 sender=:1.2 -> destination=:1.3 serial=5 reply_serial=3\n'
                                b'   variant       uint64 1700000000\n', b'')
        self.assertEqual(sysmod.keychain_identity(FakeSystem(tools={'dbus-send': dbus_send})), (LOGIN, LOGIN_CREATED))
        for bad in (b'o "/"\n', b'o "relative/path"\n', b'o "/a//b"\n', b'as 1 "org.freedesktop.DBus"\n', b''):
            weird = FakeSystem(tools={'busctl': lambda argv, input, bad=bad: Completed(0, bad, b'')})
            self.assertIsNone(sysmod.keychain_identity(weird), bad)
        doc = self.index()
        for wrong in ({'collection': 'relative', 'created': 1}, {'collection': LOGIN, 'created': -1},
                      {'collection': LOGIN, 'created': '1'}, {'collection': LOGIN}, 'login'):
            doc['keys']['codex-key']['keychain'] = wrong
            self.put_index(doc)
            with self.assertRaises(KeyStoreError) as caught:
                self.store(self.system).list()
            self.assertEqual(caught.exception.code, 'index_invalid', wrong)


@needs_posix
class RemoveTellsTheKeychainsApartTest(Case):
    def refused(self, system, code, *, held=1) -> str:
        before = (self.root / 'index.json').read_bytes()
        with self.assertRaises(KeyStoreError) as caught:
            self.store(system).remove('codex-key')
        self.assertEqual(caught.exception.code, code)
        self.assertEqual((self.root / 'index.json').read_bytes(), before)       # nothing was changed
        self.assertEqual(len(self.chain.items), held)                           # the value stays where it was saved
        return caught.exception.detail

    def test_a_second_empty_keychain_is_refused_and_nothing_changes(self) -> None:
        system, other = self.keychain((SESSION, LOGIN_CREATED + 5))
        detail = self.refused(system, 'keychain_other')
        self.assertNotIn('forget', detail)
        self.assertEqual(other.items, {})
        self.assertEqual([argv for argv, _ in system.calls if argv[0].endswith('secret-tool')], [])

    def test_an_older_keychain_at_the_same_path_is_another_one(self) -> None:
        system, _ = self.keychain((LOGIN, LOGIN_CREATED - 1))
        self.refused(system, 'keychain_other')

    def test_a_keychain_wiped_and_made_again_is_refused_naming_the_step(self) -> None:
        system, _ = self.keychain((LOGIN, SAVED + 60))
        detail = self.refused(system, 'keychain_remade')
        self.assertIn('"key forget codex-key"', detail)
        self.assertIn('may still sit in another keychain', detail)

    def test_the_same_keychain_with_the_item_gone_removes_the_record_and_says_so(self) -> None:
        self.chain.items.clear()
        report = []
        self.assertEqual(self.store(self.system).remove('codex-key', report=report).key_id, 'codex-key')
        self.assertEqual(report, ['already_gone'])
        self.assertEqual(self.store(self.system).list().keys, {})

    def test_the_same_keychain_with_the_item_there_deletes_it_and_reports_nothing(self) -> None:
        report = []
        self.store(self.system).remove('codex-key', report=report)
        self.assertEqual((report, self.chain.items), ([], {}))

    def test_a_record_naming_no_keychain_is_refused_when_its_item_is_gone_and_removed_when_found(self) -> None:
        doc = self.index()
        doc['keys']['codex-key'].pop('keychain')                       # a record saved before records named one
        self.put_index(doc)
        items = dict(self.chain.items)
        self.chain.items.clear()
        detail = self.refused(self.system, 'keychain_unrecorded', held=0)
        self.assertIn('"key forget codex-key"', detail)
        self.assertIn('may still sit in another keychain', detail)
        self.assertNotIn('codex-key', self.store(self.system).list().pending)
        self.chain.items.update(items)                                 # its own save, found: that is its keychain
        self.store(self.system).remove('codex-key')
        self.assertEqual((self.store(self.system).list().keys, self.chain.items), ({}, {}))

    def test_a_keychain_that_does_not_say_which_it_is_is_refused_when_the_item_is_gone(self) -> None:
        self.chain.collection = None
        self.chain.items.clear()
        self.refused(self.system, 'keychain_unrecorded', held=0)

    def test_the_command_says_why_or_that_the_item_was_already_gone(self) -> None:
        system, _ = self.keychain((SESSION, 1))
        status, out, err = self.main('remove', 'codex-key', '--yes', system=system)
        self.assertEqual((status, out), (1, ''))
        self.assertIn('refused: keychain_other: codex-key was saved into another keychain', err)
        self.chain.items.clear()
        status, out, err = self.main('remove', 'codex-key', '--yes')
        self.assertEqual((status, err), (0, ''))
        self.assertIn('note: the item of codex-key was already gone from that keychain', out)


@needs_posix
class ForgetTest(Case):
    def test_forget_removes_one_record_touches_no_keychain_and_is_recorded(self) -> None:
        self.add(self.store(self.system), key_id='other-key', value=OTHER)
        asked = len(self.system.calls)
        elsewhere, _ = self.keychain((SESSION, 1))                     # forget is for a key whose keychain is gone
        self.assertEqual(self.store(elsewhere).forget('codex-key').key_id, 'codex-key')
        self.assertEqual(self.system.calls[asked:], [])                 # no keychain and no bus was asked
        index = self.store(self.system).list()
        self.assertEqual(set(index.keys), {'other-key'})
        self.assertEqual(len(self.chain.items), 2)                      # no value was deleted
        self.assertEqual([(f.key_id, f.store, f.keychain) for f in index.forgotten],
                         [('codex-key', 'secret_service', (LOGIN, LOGIN_CREATED))])
        doc = self.index()
        doc['forgotten'][0]['store'] = 'nowhere'
        self.put_index(doc)
        with self.assertRaises(KeyStoreError) as caught:
            self.store(self.system).list()
        self.assertEqual(caught.exception.code, 'index_invalid')

    def test_the_command_asks_for_the_id_again_and_nothing_else_takes_the_step(self) -> None:
        terminal = Terminal(['codex-key'])
        status, out, err = self.main('forget', 'codex-key', terminal=terminal, system=self.keychain((SESSION, 1))[0])
        self.assertEqual((status, err), (0, ''))
        self.assertIn('Type the id again', terminal.prompts[0])
        self.assertIn('its value may still sit in another keychain', out)
        self.assertEqual(self.store(self.system).list().keys, {})
        registered, families = {}, []

        class Commands:
            def register(self, names, *_):
                families.extend(names)

        class Hooks:
            commands = Commands()

            def set(self, name, value):
                registered[name] = value
        hooks.register(Hooks())
        self.assertEqual((set(registered), families), ({'credentials'}, ['key']))   # the person's own command line only

    def test_a_wrong_answer_no_terminal_or_an_option_keeps_the_record(self) -> None:
        status, _, err = self.main('forget', 'codex-key', terminal=Terminal(['y']))
        self.assertEqual(status, 1)
        self.assertIn('refused: not_confirmed', err)
        status, _, err = self.main('forget', 'codex-key')               # TempStoreCase: no terminal opens
        self.assertEqual(status, 1)
        self.assertIn('refused: no_terminal', err)
        status, _, _ = self.main('forget', 'codex-key', '--yes', terminal=Terminal(['codex-key']))
        self.assertEqual(status, 2)
        self.assertIn('codex-key', self.store(self.system).list().keys)


@needs_posix
class StatusAndAuditTest(Case):
    def test_status_names_a_record_whose_keychain_is_not_the_one_answering(self) -> None:
        status, out, _ = self.main('status')
        self.assertEqual(status, 0)
        self.assertIn('codex-key  saved into: the keychain that answers now', out)
        other, _ = self.keychain((SESSION, 1))
        status, out, _ = self.main('status', system=other)
        self.assertIn('codex-key  saved into: ANOTHER keychain', out)
        remade, _ = self.keychain((LOGIN, SAVED + 60))
        self.assertIn('codex-key  saved into: a keychain the one answering now was made after',
                      self.main('status', system=remade)[1])

    def test_the_audit_names_the_keychain_and_is_incomplete(self) -> None:
        other, _ = self.keychain((SESSION, 1))
        report = audit_mod.run(self.store(other), journal=False)
        self.assertEqual(report.elsewhere, {'codex-key': 'other'})
        self.assertFalse(report.complete())
        status, out, _ = self.main('audit', '--no-journal', system=other)
        self.assertEqual(status, 4)
        self.assertIn('keychain of codex-key: ANOTHER keychain', out)

    def test_status_and_the_audit_list_every_use_of_forget(self) -> None:
        self.add(self.store(self.system), key_id='other-key', value=OTHER)
        elsewhere, _ = self.keychain((SESSION, 1))                     # forget is for a key whose keychain is gone
        self.store(elsewhere).forget('codex-key')
        self.store(elsewhere).forget('other-key')
        status, out, _ = self.main('status')
        self.assertEqual([line.split(' at ')[0] for line in out.splitlines() if line.startswith('forgotten: ')],
                         ['forgotten: codex-key', 'forgotten: other-key'])
        status, out, _ = self.main('audit', '--no-journal')
        self.assertEqual([line.split(' at ')[0] for line in out.splitlines() if line.startswith('forgotten: ')],
                         ['forgotten: codex-key', 'forgotten: other-key'])
        self.assertEqual([f.key_id for f in audit_mod.run(self.store(self.system), journal=False).forgotten],
                         ['codex-key', 'other-key'])


if __name__ == '__main__':
    import unittest
    unittest.main()
