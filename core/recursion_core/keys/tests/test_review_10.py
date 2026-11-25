"""An interrupted save and forget, any creation time a keychain gives, and where a key is without reading its value.

``forget`` of an id that only an unfinished operation names (an interrupted save) went on and said no keychain that
answers holds it while the save's item was there, and ``remove`` then called the id unknown: no order of an
interrupted save, ``forget`` and ``remove`` may leave a value behind while a command says it is gone or unknown. A
keychain whose ``Created`` is huge made a refusal a traceback or the wrong refusal: every value is taken and the
refusal is the right one, in words. ``status``, ``forget`` and the refusals of ``remove`` learn whether an item is
there by asking the service over the bus for the items that carry its attributes, which gives paths and never a
secret: no value appears in any helper's answer to them, nor in any output, error or log line. Stand-in keychains
only (``support.FakeKeychain``, the stand-in ``busctl`` and ``dbus-send``); every key is made up; each test's own
temporary folder; no test reaches a real keychain, bus or terminal.
"""
from __future__ import annotations

import io
import json
import logging
import unittest

from recursion_core.keys import __main__ as entry
from recursion_core.keys import store as store_mod
from recursion_core.keys import system as sysmod
from recursion_core.keys.backends import SecretService
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (LOGIN, LOGIN_CREATED, MARKER, OTHER, FakeKeychain, FakeSystem,
                                               attribute_pairs, bus_searches, file_system, needs_posix)
from recursion_core.keys.tests.test_review_8 import SAVED, SESSION, Case, Terminal

BIG = 2 ** 64 - 1           # the largest value a uint64 Created can hold


def tool_actions(system) -> list:
    return [argv[1] for argv, _ in system.calls if argv[0].endswith('secret-tool')]


def held(chain, key_id) -> int:
    return sum(1 for item in chain.items if attribute_pairs(item).get('recursion_key_id') == key_id)


class Round(Case):
    def sharing(self, collection):
        """A keychain that says it is ``collection`` and holds the very items of the one the key was saved into."""
        system, chain = self.keychain(collection)
        chain.items = self.chain.items
        return system, chain

    def run_main(self, system, *argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(list(argv), out=out, err=err, store=self.store(system), **kw)
        return status, out.getvalue(), err.getvalue()

    def interrupted(self, key_id='late-key'):
        """A save whose helper was killed after it saved: only an unfinished operation names the id, and its item is
        in the keychain that answers."""
        self.chain.after_store = Completed(-15, b'', b'')
        with self.assertRaises(KeyStoreError):
            self.add(self.store(self.system), key_id=key_id, value=OTHER)
        self.chain.after_store = None
        index = self.store(self.system).list()
        self.assertEqual((key_id in index.pending, key_id in index.keys, held(self.chain, key_id)), (True, False, 1))


@needs_posix
class InterruptedSaveForgetTest(Round):
    def test_forget_of_an_interrupted_save_whose_item_is_there_refuses_and_remove_then_deletes_it(self) -> None:
        self.interrupted()
        before = (self.root / 'index.json').read_bytes()
        status, out, err = self.run_main(self.system, 'forget', 'late-key', terminal=Terminal(['late-key']))
        self.assertEqual((status, out), (1, ''))
        self.assertIn('refused: value_reachable: late-key has an item in the keychain that answers now', err)
        self.assertIn('"key remove late-key"', err)
        self.assertIn('clears the unfinished operation', err)
        self.assertNotIn('no keychain that answers here holds it', out + err)
        self.assertEqual(((self.root / 'index.json').read_bytes(), held(self.chain, 'late-key')), (before, 1))
        self.assertIsNone(self.store(self.system).remove('late-key'))
        self.assertEqual(held(self.chain, 'late-key'), 0)
        self.assertNotIn('late-key', self.store(self.system).list().pending)

    def test_remove_first_then_forget_leaves_nothing_and_says_unknown_truly(self) -> None:
        self.interrupted()
        self.assertIsNone(self.store(self.system).remove('late-key'))
        with self.assertRaises(KeyStoreError) as caught:
            self.store(self.system).forget('late-key')
        self.assertEqual((caught.exception.code, held(self.chain, 'late-key')), ('key_unknown', 0))

    def test_forget_where_no_keychain_answers_goes_on_and_remove_later_names_that_step(self) -> None:
        self.interrupted()
        self.assertIsNone(self.store(file_system()).forget('late-key'))
        with self.assertRaises(KeyStoreError) as caught:
            self.store(self.system).remove('late-key')
        self.assertEqual(caught.exception.code, 'key_unknown')
        self.assertIn('"key forget" removed its record', caught.exception.detail)
        self.assertIn('the value may still sit in a keychain that did not answer then', caught.exception.detail)
        self.assertEqual(held(self.chain, 'late-key'), 1)          # what the words say: it was not deleted

    def test_an_interrupted_save_whose_item_is_not_there_is_forgotten_and_says_so(self) -> None:
        self.interrupted()
        for item in [item for item in self.chain.items if attribute_pairs(item)['recursion_key_id'] == 'late-key']:
            del self.chain.items[item]
        status, out, err = self.run_main(self.system, 'forget', 'late-key', terminal=Terminal(['late-key']))
        self.assertEqual((status, err), (0, ''))
        self.assertIn('no keychain that answers here holds it', out)
        self.assertNotIn('late-key', self.store(self.system).list().pending)

    def test_forget_of_an_interrupted_save_in_the_owner_only_file_refuses(self) -> None:
        store = KeyStore(self.base / 'file-config' / 'keys', system=file_system(), clock=lambda: SAVED + 0.25)
        self.add(store, key_id='file-key')
        path = store.root / 'index.json'
        doc = json.loads(path.read_bytes())
        saved = doc['keys'].pop('file-key')                  # only an unfinished add names the id now
        doc['pending']['file-key'] = {'op': 'add', 'started_at': saved['created_at'], 'save_id': saved['save_id']}
        path.write_bytes((json.dumps(doc, indent=1) + '\n').encode())
        before = path.read_bytes()
        with self.assertRaises(KeyStoreError) as caught:
            store.forget('file-key')
        self.assertEqual(caught.exception.code, 'value_reachable')
        self.assertIn('owner-only file', caught.exception.detail)
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(store.remove('file-key'))
        self.assertNotIn(b'file-key', (store.root / 'secrets.json').read_bytes())

    def test_a_keychain_that_answers_and_cannot_be_asked_makes_forget_refuse(self) -> None:
        self.interrupted()
        before = (self.root / 'index.json').read_bytes()
        other, chain = self.sharing((SESSION, LOGIN_CREATED + 5))
        for system, failing, key_id in ((self.system, self.chain, 'late-key'), (other, chain, 'codex-key')):
            failing.mode = 'fail'                            # the service answers on the bus and fails to search
            with self.assertRaises(KeyStoreError) as caught:
                self.store(system).forget(key_id)
            self.assertEqual(caught.exception.code, 'keychain_failed', key_id)
        self.assertEqual((self.root / 'index.json').read_bytes(), before)

    def test_secret_tool_missing_while_the_keychain_answers_makes_forget_refuse_in_words(self) -> None:
        self.interrupted()
        before = (self.root / 'index.json').read_bytes()
        del self.system.tools['secret-tool']
        for key_id in ('late-key', 'codex-key'):
            status, out, err = self.run_main(self.system, 'forget', key_id, terminal=Terminal([key_id]))
            self.assertEqual((status, out), (1, ''), key_id)
            self.assertIn(f'refused: keychain_tool_missing: {key_id} may still be in a keychain that answers here', err)
            self.assertIn('Nothing was changed', err)
        self.assertEqual((self.root / 'index.json').read_bytes(), before)


@needs_posix
class AnyCreatedTimeTest(Round):
    HUGE = (BIG, 10 ** 17, 2 ** 63, 2 ** 64, 10 ** 30)

    def saved_at(self, created, key_id):
        first, chain = self.keychain((LOGIN, created))
        self.add(self.store(first), key_id=key_id, value=OTHER)
        return chain

    def refused_in_words(self, system, key_id, code, chain):
        before = (self.root / 'index.json').read_bytes()
        status, out, err = self.run_main(system, 'remove', key_id, '--yes')
        self.assertEqual((status, out), (1, ''), key_id)
        self.assertTrue(err.startswith(f'refused: {code}: '), err)
        self.assertIn('Nothing was changed', err)
        self.assertNotIn('Traceback', err)
        self.assertEqual(((self.root / 'index.json').read_bytes(), held(chain, key_id)), (before, 1))
        return err

    def test_a_huge_created_at_the_save_gives_keychain_other_in_words_where_another_keychain_answers(self) -> None:
        for number, created in enumerate(self.HUGE):
            key_id = f'big-key-{number}'
            chain = self.saved_at(created, key_id)
            self.assertEqual(self.index()['keys'][key_id]['keychain'], {'collection': LOGIN, 'created': created})
            other, _ = self.keychain((SESSION, 5))
            self.refused_in_words(other, key_id, 'keychain_other', chain)
            older, _ = self.keychain((LOGIN, 5))             # the same path, made before the save: another one
            self.refused_in_words(older, key_id, 'keychain_other', chain)

    def test_a_huge_created_is_named_as_not_a_date(self) -> None:
        for created in (BIG, 2 ** 64, 10 ** 30, 10 ** 400):
            self.assertIn('is not a date', store_mod._named((LOGIN, created)))
        self.assertIn('made on 2023-11-14 (UTC)', store_mod._named((LOGIN, LOGIN_CREATED)))

    def test_a_keychain_that_answers_with_a_huge_created_is_a_remade_one_in_words(self) -> None:
        for created in self.HUGE:
            system, _ = self.keychain((LOGIN, created))
            err = self.refused_in_words(system, 'codex-key', 'keychain_remade', self.chain)
            self.assertIn('made on 2023-11-14 (UTC)', err)

    def test_zero_negative_and_not_a_number_name_no_keychain_and_refuse_as_unrecorded(self) -> None:
        for number, text in enumerate(('0', '00', '-5', '-0', 'nan', 'NaN', 'inf', '1e17', '+5', '5.0', '0x10', '')):
            key_id = f'odd-key-{number}'
            chain = self.saved_at(text, key_id)
            self.assertNotIn('keychain', self.index()['keys'][key_id], text)
            other, _ = self.keychain((SESSION, 5))
            self.refused_in_words(other, key_id, 'keychain_unrecorded', chain)

    def test_dbus_send_takes_the_same_values_the_same_way(self) -> None:
        for text, expected in (('0', None), ('-5', None), ('nan', None), ('1e17', None), ('+5', None),
                               (str(BIG), (LOGIN, BIG)), (str(10 ** 17), (LOGIN, 10 ** 17))):
            def dbus_send(argv, input, text=text):
                if any('ReadAlias' in word for word in argv):
                    return Completed(0, b'method return time=1 sender=:1.2\n   object path "' + LOGIN.encode() + b'"\n',
                                     b'')
                return Completed(0, f'method return time=1 sender=:1.2\n   variant       uint64 {text}\n'.encode(), b'')
            self.assertEqual(sysmod.keychain_identity(FakeSystem(tools={'dbus-send': dbus_send})), expected, text)

    def test_an_index_naming_zero_negative_or_not_a_whole_number_is_refused(self) -> None:
        for created in (0, -1, -BIG, 1.5, 1e17, float('nan'), '5', True, None, [5]):
            doc = self.index()
            doc['keys']['codex-key']['keychain']['created'] = created
            self.put_index(doc)
            status, out, err = self.run_main(self.system, 'remove', 'codex-key', '--yes')
            self.assertEqual(status, 1, created)
            self.assertTrue(err.startswith('refused: index_invalid: '), (created, err))
            self.assertEqual(held(self.chain, 'codex-key'), 1)


def dbus_send_for(chain):
    """A stand-in ``dbus-send --print-reply`` for a host with no ``busctl``: the bus's names, the keychain's identity and
    ``SearchItems`` over ``chain``'s items (paths only, as it prints them)."""
    def run(argv, input):
        method = next(word for word in argv if word.startswith('org.freedesktop.'))
        if method.endswith('.ListNames') or method.endswith('.ListActivatableNames'):
            names = '      string "org.freedesktop.DBus"\n      string "org.freedesktop.secrets"\n'
            return Completed(0, f'method return time=1 sender=x\n   array [\n{names}   ]\n'.encode(), b'')
        if method.endswith('.ReadAlias'):
            return Completed(0, f'method return time=1 sender=x\n   object path "{chain.collection[0]}"\n'.encode(), b'')
        if method.endswith('.Get'):
            return Completed(0, f'method return time=1 sender=x\n   variant       uint64 {chain.collection[1]}\n'.encode(),
                             b'')
        if method.endswith('.SearchItems'):
            found = chain._matches(argv[-1].split(':', 3)[3].split(','))
            blocks = []
            for group in ([k for k in found if k not in chain.locked], [k for k in found if k in chain.locked]):
                paths = ''.join(f'      object path "{LOGIN}/{n}"\n' for n, _ in enumerate(group, 1))
                blocks.append(f'   array [\n{paths}   ]\n')
            return Completed(0, ('method return time=1 sender=x\n' + ''.join(blocks)).encode(), b'')
        return Completed(1, b'', b'Error org.freedesktop.DBus.Error.UnknownMethod\n')
    return run


@needs_posix
class NoValueIsReadTest(Round):
    """``status``, ``forget`` and the refusals of ``remove`` are run with every helper's answer, every output and error
    line and every log record watched: none holds a value, and ``secret-tool`` (whose ``lookup`` returns a value) is
    never asked."""

    def setUp(self) -> None:
        super().setUp()
        self.logged = []
        handler = logging.Handler(logging.DEBUG)
        handler.emit = lambda record: self.logged.append(record.getMessage())
        logger = logging.getLogger()
        old = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.removeHandler, handler)
        self.addCleanup(logger.setLevel, old)

    def watched(self, system):
        answers = []
        real = system.run

        def run(argv, *, input=None, timeout):
            done = real(argv, input=input, timeout=timeout)
            answers.append(done.stdout + done.stderr)
            return done
        system.run = run
        return answers

    def no_value(self, system, answers, *texts) -> None:
        for value in (MARKER, OTHER):
            for text in (*texts, *self.logged):
                self.assertNotIn(value, text)
            for answer in answers:
                self.assertNotIn(value.encode(), answer)
        self.assertEqual(tool_actions(system), [])

    def test_status_reads_no_value(self) -> None:
        for collection in ((LOGIN, LOGIN_CREATED), (SESSION, LOGIN_CREATED + 5), (LOGIN, SAVED + 60), None):
            system, _ = self.sharing(collection)
            answers = self.watched(system)
            status, out, err = self.run_main(system, 'status')
            self.assertEqual(status, 0)
            self.assertIn('codex-key  saved into: the keychain that answers now', out)
            self.assertTrue(bus_searches(system) or collection == (LOGIN, LOGIN_CREATED))
            self.no_value(system, answers, out, err)

    def test_a_locked_own_save_is_found_with_no_unlock_prompt_and_no_value(self) -> None:
        system, chain = self.sharing((SESSION, LOGIN_CREATED + 5))
        chain.locked = set(chain.items)
        chain.unlock = 'cancel'
        answers = self.watched(system)
        status, out, err = self.run_main(system, 'forget', 'codex-key', terminal=Terminal(['codex-key']))
        self.assertEqual(status, 1)
        self.assertIn('refused: value_reachable: ', err)
        self.no_value(system, answers, out, err)

    def test_forget_reads_no_value_whether_it_refuses_or_goes_on(self) -> None:
        self.interrupted()
        for key_id, collection in (('codex-key', (LOGIN, LOGIN_CREATED)), ('codex-key', (SESSION, LOGIN_CREATED + 5)),
                                   ('late-key', (SESSION, LOGIN_CREATED + 5))):
            system, _ = self.sharing(collection)
            answers = self.watched(system)
            status, out, err = self.run_main(system, 'forget', key_id, terminal=Terminal([key_id]))
            self.assertEqual(status, 1, (key_id, collection))
            self.assertTrue(bus_searches(system) or collection == (LOGIN, LOGIN_CREATED))
            self.no_value(system, answers, out, err)
        other, _ = self.keychain((SESSION, 1))                # another empty keychain: forget goes on
        answers = self.watched(other)
        status, out, err = self.run_main(other, 'forget', 'codex-key', terminal=Terminal(['codex-key']))
        self.assertEqual(status, 0)
        self.no_value(other, answers, out, err)

    def test_the_refusals_of_remove_read_no_value(self) -> None:
        for collection, code in (((SESSION, LOGIN_CREATED + 5), 'keychain_other'),
                                 ((LOGIN, SAVED + 60), 'keychain_remade'), (None, 'keychain_unrecorded')):
            system, _ = self.keychain(collection)
            answers = self.watched(system)
            status, out, err = self.run_main(system, 'remove', 'codex-key', '--yes')
            self.assertEqual(status, 1)
            self.assertTrue(err.startswith(f'refused: {code}: '), err)
            self.assertTrue(bus_searches(system))
            self.no_value(system, answers, out, err)
        self.assertEqual(held(self.chain, 'codex-key'), 1)

    def test_over_dbus_send_alone_the_same_answers_hold(self) -> None:
        chain = FakeKeychain()
        chain.items = self.chain.items
        chain.collection = (SESSION, LOGIN_CREATED + 5)
        system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base)},
                            tools={'dbus-send': dbus_send_for(chain), 'secret-tool': chain}, bus='unix:path=/fake/bus')
        answers = self.watched(system)
        status, out, err = self.run_main(system, 'forget', 'codex-key', terminal=Terminal(['codex-key']))
        self.assertEqual(status, 1)
        self.assertIn('refused: value_reachable: codex-key is in the keychain that answers now', err)
        self.assertTrue(bus_searches(system))
        self.no_value(system, answers, out, err)
        chain.items = {}
        self.assertEqual(self.run_main(system, 'status')[0], 0)


@needs_posix
class TheLookIsTheBusTest(Round):
    def test_a_save_gone_between_the_look_and_the_removal_is_said_to_be_already_gone(self) -> None:
        system, _ = self.sharing((SESSION, LOGIN_CREATED + 5))
        real, seen = system.run, []

        def run(argv, *, input=None, timeout):
            done = real(argv, input=input, timeout=timeout)
            if 'SearchItems' in argv and not seen and not done.stdout.startswith(b'aoao 0 0'):
                seen.append(1)
                self.chain.items.clear()                     # the save is gone right after it was seen
            return done
        system.run = run
        report = []
        self.assertEqual(self.store(system).remove('codex-key', report=report).key_id, 'codex-key')
        self.assertEqual((seen, report, self.store(system).list().keys), ([1], ['already_gone'], {}))


@needs_posix
class SearchItemsReplyTest(Round):
    def ask(self, name, text):
        system = FakeSystem(tools={name: lambda argv, input: Completed(0, text, b'')})
        return sysmod.keychain_items(system, ['application', 'recursion'])

    def test_busctl_replies_are_counted_and_anything_else_is_no_answer(self) -> None:
        for text, expected in ((b'aoao 1 "/a/b/1" 0\n', 1), (b'aoao 0 0\n', 0), (b'aoao 1 "/a/1" 2 "/a/2" "/a/3"\n', 3),
                               (b'ao 1 "/a/1" ao 0\n', 1), (b'aoao 2 "/a/1" 0\n', None), (b'aoao 1 /a/1 0\n', None),
                               (b'as 1 "org.freedesktop.DBus"\n', None), (b'aoao 0 0 0\n', None), (b'', None)):
            self.assertEqual(self.ask('busctl', text), expected, text)

    def test_dbus_send_replies_are_counted_and_anything_else_is_no_answer(self) -> None:
        head = b'method return time=1 sender=x\n'
        empty = b'   array [\n   ]\n'
        one = b'   array [\n      object path "/a/b/1"\n   ]\n'
        for text, expected in ((head + one + empty, 1), (head + empty + one, 1), (head + empty + empty, 0),
                               (head + one, None), (head + one + b'   string "x"\n' + empty, None), (b'', None)):
            self.assertEqual(self.ask('dbus-send', text), expected, text)

    def test_no_tool_or_a_failing_tool_is_no_answer_and_has_item_refuses(self) -> None:
        self.assertIsNone(sysmod.keychain_items(FakeSystem(tools={}), ['application', 'recursion']))
        failing = FakeSystem(tools={'busctl': lambda argv, input: Completed(1, b'', b'Call failed\n')})
        self.assertIsNone(sysmod.keychain_items(failing, ['application', 'recursion']))
        with self.assertRaises(KeyStoreError) as caught:
            SecretService(failing, 'a' * 32).has_item('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_failed')


if __name__ == '__main__':
    unittest.main()
