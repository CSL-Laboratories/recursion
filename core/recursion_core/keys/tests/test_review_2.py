"""Regressions for the second reads of draft #386: two Codex reads (gpt-6.1-sol, gpt-6-astra) and a DeepSeek break-it
read. Each test names the finding it pins. Every key is made up; no real keychain, bus or terminal is reached."""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from unittest import mock

from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys import hooks, validate
from recursion_core.keys.backends import OwnerFile, SecretService
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.files import open_folder
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import Completed, System
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    attribute_pairs,
    fake_tools,
    keychain_system,
    needs_posix,
)

def fixed_clock() -> float:
    return 1_763_160_000.25


def run_main(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    status = entry.main(list(argv), out=out, err=err, store=store)
    return status, out.getvalue() + err.getvalue()


def items_of(chain, store_id: str, key_id: str) -> list:
    """The fake keychain's items saved for ``key_id`` in the store named ``store_id``, whichever save stored them."""
    return [key for key in chain.items if attribute_pairs(key).get('recursion_store') == store_id
            and attribute_pairs(key).get('recursion_key_id') == key_id]


@needs_posix
class StoreIdentityTest(TempStoreCase):
    """Sol change 1 (P1): the store's identity must not depend on how its folder is spelled; the Liaison's 8:48 PM
    ruling asks for the class to be closed, form by form."""

    def setUp(self) -> None:
        super().setUp()
        self.system, self.chain = keychain_system()

    def keychain_store(self, root) -> KeyStore:
        return KeyStore(root, system=self.system, clock=fixed_clock)

    def spellings(self, root: Path) -> dict:
        """Other spellings of ``root``, a folder under ``self.base``: each reaches the same folder."""
        link = self.base / f'link-{root.parent.parent.name}'
        if not link.exists():
            link.symlink_to(root.parent.parent)                        # a symlinked ancestor
        text = str(root)
        return {
            'dot-dot': root.parent.parent / 'recursion' / '..' / 'recursion' / 'keys',
            'symlinked parent': link / 'recursion' / 'keys',
            'trailing separator': text + '/',
            'doubled separators': text.replace('/recursion/', '//recursion//'),
            'dot': root.parent / '.' / 'keys',
            'leading double slash': '/' + text,
        }

    def test_every_spelling_reads_one_store_id_and_reaches_one_value(self) -> None:
        canonical = self.keychain_store(self.root)
        self.add(canonical)
        store_id = canonical.list().store_id
        self.assertRegex(store_id, r'^[0-9a-f]{32}$')
        for label, spelling in self.spellings(self.root).items():
            with self.subTest(spelling=label):
                alias = self.keychain_store(spelling)
                self.assertEqual(alias.list().store_id, store_id)
                self.system.calls.clear()
                self.assertEqual(alias.credential('codex-key', 'CODEX_API_KEY'), MARKER)
                lookups = [argv for argv, _ in self.system.calls if argv[1:2] == ['lookup']]
                self.assertEqual(attribute_pairs(lookups[-1][2:])['recursion_store'], store_id)

    def test_sol_witness_remove_and_add_again_through_an_alias_never_pairs_new_metadata_with_the_old_value(self) -> None:
        for number, label in enumerate(self.spellings(self.root)):
            with self.subTest(spelling=label):
                root = self.base / f'case-{number}' / 'recursion' / 'keys'
                canonical = self.keychain_store(root)
                # A key for a vendor outside PROVIDERS, with no spend opt-in, saved through the canonical folder ...
                canonical.add('codex-key', 'mistral', 'CODEX_API_KEY', OTHER)
                alias = self.keychain_store(self.spellings(root)[label])
                # ... removed and added again under a published vendor, with an opt-in, through an alias.
                alias.remove('codex-key')
                alias.add('codex-key', 'openai', 'CODEX_API_KEY', MARKER, metered_spend_allowed=True, cap_usd=5)
                self.assertEqual(canonical.credential('codex-key', 'CODEX_API_KEY'), MARKER)
                self.assertEqual(len(items_of(self.chain, canonical.list().store_id, 'codex-key')), 1)

    def test_a_removal_under_any_spelling_removes_every_value_saved_for_the_id(self) -> None:
        for number, label in enumerate(self.spellings(self.root)):
            with self.subTest(spelling=label):
                root = self.base / f'removal-{number}' / 'recursion' / 'keys'
                canonical = self.keychain_store(root)
                self.add(canonical)
                store_id = canonical.list().store_id
                # A value an earlier save of the same id left behind, under a save id no record names any more.
                self.chain.items[('application', 'recursion', 'recursion_store', store_id, 'recursion_key_id',
                                  'codex-key', 'recursion_save', 'f' * 32)] = OTHER.encode()
                self.keychain_store(self.spellings(root)[label]).remove('codex-key')
                self.assertEqual(items_of(self.chain, store_id, 'codex-key'), [])
                self.assertEqual(canonical.list().keys, {})

    def test_a_relative_folder_is_refused_and_the_working_directory_never_matters(self) -> None:
        for call in (lambda s: self.add(s), lambda s: s.list(), lambda s: s.remove('codex-key')):
            with self.assertRaises(KeyStoreError) as caught:
                call(self.keychain_store('config/recursion/keys'))
            self.assertEqual(caught.exception.code, 'folder_invalid')
        with self.assertRaises(KeyError):
            self.keychain_store('config/recursion/keys').credential('codex-key', 'CODEX_API_KEY')
        canonical = self.keychain_store(self.root)
        self.add(canonical)
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        for folder in (self.base, self.root, Path('/')):
            os.chdir(folder)
            store = self.keychain_store(self.root)
            self.assertEqual(store.list().store_id, canonical.list().store_id)
            self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_a_moved_folder_keeps_its_identity(self) -> None:
        canonical = self.keychain_store(self.root)
        self.add(canonical)
        store_id = canonical.list().store_id
        os.rename(self.base / 'config', self.base / 'moved')
        moved = self.keychain_store(self.base / 'moved' / 'recursion' / 'keys')
        self.assertEqual(moved.list().store_id, store_id)
        self.assertEqual(moved.credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_a_copied_folder_never_hands_one_saves_value_to_another_saves_record(self) -> None:
        original = self.keychain_store(self.root)
        self.add(original)
        shutil.copytree(self.base / 'config', self.base / 'copy', symlinks=True)
        copy = self.keychain_store(self.base / 'copy' / 'recursion' / 'keys')
        self.assertEqual(copy.list().store_id, original.list().store_id)   # a copy carries the identity with it
        copy.remove('codex-key')                                            # ... so it removes the shared value,
        copy.add('codex-key', 'mistral', 'CODEX_API_KEY', OTHER)            # and its new save is its own
        with self.assertRaises(KeyRefused) as refused:
            original.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')         # never OTHER, under openai's opt-in

    def test_two_stores_never_share_an_id(self) -> None:
        first, second = self.keychain_store(self.root), self.keychain_store(self.base / 'other' / 'keys')
        self.add(first)
        self.add(second, value=OTHER)
        self.assertNotEqual(first.list().store_id, second.list().store_id)
        self.assertEqual(first.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        self.assertEqual(second.credential('codex-key', 'CODEX_API_KEY'), OTHER)

    def test_the_store_id_is_made_once_and_survives_an_empty_index(self) -> None:
        store = self.keychain_store(self.root)
        self.add(store)
        store_id = store.list().store_id
        store.remove('codex-key')
        self.assertEqual(store.list().store_id, store_id)
        self.assertIsNone(store.list().backend)                             # the pin is released all the same
        self.add(store, value=OTHER)
        self.assertEqual(store.list().store_id, store_id)

    def test_an_index_without_the_store_id_or_a_save_id_is_refused(self) -> None:
        store = self.keychain_store(self.root)
        self.add(store)
        path = self.root / 'index.json'
        good = json.loads(path.read_text())
        cases = {
            'no store id': lambda d: d.pop('store_id'),
            'a null store id': lambda d: d.update(store_id=None),
            'a short store id': lambda d: d.update(store_id='a' * 31),
            'an upper-case store id': lambda d: d.update(store_id='A' * 32),
            'a numeric store id': lambda d: d.update(store_id=7),
            'no save id': lambda d: d['keys']['codex-key'].pop('save_id'),
            'a long save id': lambda d: d['keys']['codex-key'].update(save_id='b' * 33),
            'a non-hex save id': lambda d: d['keys']['codex-key'].update(save_id='g' * 32),
        }
        for label, change in cases.items():
            with self.subTest(case=label):
                doc = json.loads(json.dumps(good))
                change(doc)
                path.write_text(json.dumps(doc, indent=1) + '\n')
                with self.assertRaises(KeyStoreError) as caught:
                    store.list()
                self.assertEqual(caught.exception.code, 'index_invalid')
        path.write_text(json.dumps(good, indent=1) + '\n')
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_the_value_is_looked_up_by_its_own_save(self) -> None:
        store = self.keychain_store(self.root)
        self.add(store)
        record = store.list().keys['codex-key']
        (key,) = self.chain.items
        self.assertEqual(attribute_pairs(key)['recursion_save'], record.save_id)
        self.assertNotIn('save_id', record.as_json())                       # list --json keeps its shape
        self.chain.items[key[:-1] + ('0' * 32,)] = self.chain.items.pop(key)
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')


@needs_posix
class VerifiedRemovalTest(TempStoreCase):
    """Astra change 1 (R4): absence is never read from lookup's silent exit 1; a removal that cannot show the value
    gone is refused and the key stays on record."""

    def setUp(self) -> None:
        super().setUp()
        self.system, self.chain = keychain_system()
        self.store_ = KeyStore(self.root, system=self.system, clock=fixed_clock)
        self.add(self.store_)

    def assert_kept(self, code: str) -> None:
        with self.assertRaises(KeyStoreError) as caught:
            self.store_.remove('codex-key')
        self.assertEqual(caught.exception.code, code)
        self.assertIn('codex-key', self.store_.list().keys)
        self.assertEqual(len(self.chain.items), 1)

    def test_r4_a_helper_that_answers_every_call_with_a_silent_exit_1_keeps_the_record(self) -> None:
        self.chain.mode = 'silent'                         # astra's case: an unlock that yields no path, each time
        self.assert_kept('keychain_failed')
        self.chain.mode = 'ok'
        self.store_.remove('codex-key')
        self.assertEqual(self.chain.items, {})
        self.assertEqual(self.store_.list().keys, {})

    def test_a_locked_item_whose_unlock_is_cancelled_is_refused_and_kept(self) -> None:
        self.chain.locked = set(self.chain.items)
        self.chain.unlock = 'cancel'
        self.assert_kept('keychain_locked')
        self.assertEqual(self.store_.list().pending['codex-key'].op, 'remove')

    def test_a_locked_item_unlocked_at_the_prompt_is_removed(self) -> None:
        self.chain.locked = set(self.chain.items)
        self.store_.remove('codex-key')
        self.assertEqual(self.chain.items, {})
        self.assertEqual(self.store_.list().keys, {})

    def test_a_search_that_fails_cannot_show_the_value_gone(self) -> None:
        self.chain.locked = set(self.chain.items)
        self.chain.unlock = 'cancel'
        self.chain.searched = 'fail'
        self.assert_kept('keychain_failed')

    def test_a_clear_that_fails_is_refused_with_its_own_code(self) -> None:
        chain = self.chain

        def failing_clear(argv, input):
            if argv[1] == 'clear':
                return Completed(1, b'', b'secret-tool: The name org.freedesktop.secrets was not provided\n')
            return chain(argv, input)
        self.system.tools['secret-tool'] = failing_clear
        with self.assertRaises(KeyStoreError) as caught:
            self.store_.remove('codex-key')
        self.assertEqual((caught.exception.code, caught.exception.detail), ('keychain_failed', 'secret-tool clear exited 1'))
        self.assertIn('codex-key', self.store_.list().keys)

    def test_a_value_already_gone_is_removed_cleanly(self) -> None:
        self.chain.items.clear()
        self.assertEqual(self.store_.remove('codex-key').key_id, 'codex-key')
        self.assertEqual(self.store_.list().keys, {})

    def test_presence_answers_true_false_or_refuses(self) -> None:
        backend = SecretService(self.system, self.store_.list().store_id)
        self.assertIs(backend.presence('codex-key'), True)
        self.assertIs(backend.presence('nobody'), False)
        self.chain.locked = set(self.chain.items)
        self.chain.unlock = 'cancel'
        self.assertIs(backend.presence('codex-key'), True)        # listed by search --all, though locked
        self.assertIs(backend.holds('codex-key', None), True)
        self.chain.searched = 'fail'
        with self.assertRaises(KeyStoreError) as caught:
            backend.presence('codex-key')
        self.assertEqual(caught.exception.detail, 'secret-tool search exited 1')
        self.assertIsNone(backend.holds('codex-key', None))
        self.chain.mode = 'locked'
        with self.assertRaises(KeyStoreError) as caught:
            backend.presence('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_locked')
        self.assertIsNone(backend.holds('codex-key', None))
        del self.system.tools['secret-tool']
        self.assertIsNone(backend.holds('codex-key', None))        # a helper that is gone cannot tell either


@needs_posix
class UncertainSaveTest(TempStoreCase):
    """Astra change 2 (R10): a save whose helper failed drops its intent only when the backend verifiably holds
    nothing for the id."""

    def setUp(self) -> None:
        super().setUp()
        self.system, self.chain = keychain_system()
        self.store_ = KeyStore(self.root, system=self.system, clock=fixed_clock)

    def failing_store(self, answer: Completed) -> None:
        """``store`` fails without saving; every other call reaches the fake keychain."""
        chain = self.chain
        self.system.tools['secret-tool'] = lambda argv, input: answer if argv[1] == 'store' else chain(argv, input)

    def test_r10_a_helper_killed_after_it_saved_keeps_the_intent(self) -> None:
        self.chain.after_store = Completed(-15, b'', b'')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(self.store_)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        index = self.store_.list()
        self.assertEqual(index.pending['codex-key'].op, 'add')
        self.assertEqual(len(self.chain.items), 1)
        with self.assertRaises(KeyRefused) as refused:
            self.store_.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'operation_pending')
        self.chain.after_store = None
        self.assertIsNone(self.store_.remove('codex-key'))
        self.assertEqual(self.chain.items, {})
        self.assertEqual(self.store_.list().pending, {})

    def test_a_failed_save_that_left_nothing_drops_the_intent(self) -> None:
        self.failing_store(Completed(1, b'', b'secret-tool: Cannot create an item\n'))
        with self.assertRaises(KeyStoreError):
            self.add(self.store_)
        index = self.store_.list()
        self.assertEqual((index.keys, index.pending), ({}, {}))
        self.assertRegex(index.store_id, r'^[0-9a-f]{32}$')

    def test_a_failed_save_whose_outcome_cannot_be_told_keeps_the_intent(self) -> None:
        for answer in (Completed(-9, b'', b''), Completed(1, b'', b'')):
            with self.subTest(returncode=answer.returncode):
                self.failing_store(answer)
                self.chain.searched = 'fail'
                with self.assertRaises(KeyStoreError):
                    self.add(self.store_)
                self.assertEqual(self.store_.list().pending['codex-key'].op, 'add')
                self.chain.searched = 'ok'
                self.system.tools['secret-tool'] = self.chain
                self.assertIsNone(self.store_.remove('codex-key'))
                self.assertEqual(self.store_.list().pending, {})

    def test_the_owner_file_says_whether_it_holds_a_value(self) -> None:
        store = self.store()
        self.add(store)
        with open_folder(self.root, create=False) as folder:
            self.assertIs(OwnerFile().holds('codex-key', folder), True)
            self.assertIs(OwnerFile().holds('nobody', folder), False)
            (self.root / 'secrets.json').write_text('not json')
            self.assertIsNone(OwnerFile().holds('codex-key', folder))
            (self.root / 'secrets.json').unlink()
            self.assertIs(OwnerFile().holds('codex-key', folder), False)


_SIGNALLED = r'''#!{python} -I
import os, signal, sys
from pathlib import Path
item = Path({state!r}) / 'item'
action = sys.argv[1]
if action == 'store':
    item.write_bytes(sys.stdin.buffer.read())
    os.kill(os.getpid(), signal.SIGTERM)
elif action == 'lookup':
    if not item.exists():
        sys.exit(1)
    sys.stdout.buffer.write(item.read_bytes())
elif action == 'clear':
    if not item.exists():
        sys.exit(1)
    item.unlink()
elif action == 'search':
    if item.exists():
        sys.stdout.write('[1]\nlabel = item\n')
else:
    sys.exit(2)
'''


@needs_posix
class SignalledHelperProcessTest(TempStoreCase):
    """Astra change 2, through a real process: a stand-in ``secret-tool`` saves the value, then SIGTERMs itself."""

    def test_a_helper_that_saved_and_was_killed_leaves_a_removable_intent(self) -> None:
        tools = tempfile.TemporaryDirectory(prefix='keys-tools-')
        self.addCleanup(tools.cleanup)
        bin_dir, state = fake_tools(Path(tools.name), secret_tool=False)
        helper = bin_dir / 'secret-tool'
        helper.write_text(_SIGNALLED.format(python=sys.executable, state=str(state)))
        helper.chmod(0o700)
        runtime = Path(tools.name) / 'run'
        runtime.mkdir(mode=0o700)
        bus = socket.socket(socket.AF_UNIX)
        self.addCleanup(bus.close)
        bus.bind(str(runtime / 'bus'))
        store = KeyStore(self.root, system=System({'PATH': str(bin_dir), 'HOME': str(self.base),
                                                   'XDG_RUNTIME_DIR': str(runtime)}, platform='linux'))
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertTrue((state / 'item').exists())
        self.assertEqual(store.list().pending['codex-key'].op, 'add')
        self.assertIsNone(store.remove('codex-key'))
        self.assertFalse((state / 'item').exists())
        self.assertEqual(store.list().pending, {})


class KeyShapedIdTest(TempStoreCase):
    """DeepSeek N1 (MEDIUM): a key pasted where the id goes is refused, whatever its shape, and never repeated."""

    PASTED = (
        'sk-proj-Ab3dEf5hIj7kLm9nOp1qRs7tUv',
        'AKIAIOSFODNN7EXAMPLE',                         # upper case and digits, no lower case
        'a1b2c3d4e5f60718293a4b5c6d7e8f90',             # 32 hex digits
        'sk-ant-api03-Ab3d-Ef5h-Ij7k-Lm9n-Op1q-Rs7t',   # split by hyphens every four
        'madeup.Ab3dEf5h.Ij7kLm9n.Op1qRs7t',            # split by dots
        'madeup_Ab3dEf5h_Ij7kLm9n_Op1qRs7t',            # split by underscores
        'x' * 64,
        '123e4567-e89b-12d3-a456-426614174000',         # a UUID
        'xoxb-123456789012-ab',                         # 12 digits in a row
    )

    def test_deepseeks_shapes_are_refused_and_not_repeated(self) -> None:
        for pasted in self.PASTED:
            with self.subTest(shape=pasted[:10]):
                self.assertTrue(validate.looks_like_a_key(pasted))
                problem = validate.id_problem(pasted)
                self.assertIsNotNone(problem)
                self.assertNotIn(pasted, problem)
        if os.name == 'posix':
            store = self.store()
            with self.assertRaises(KeyStoreError) as caught:
                self.add(store, key_id='AKIAIOSFODNN7EXAMPLE')
            self.assertEqual(caught.exception.code, 'id_invalid')
            self.assertFalse(self.root.exists())

    def test_the_boundaries(self) -> None:
        cases = {
            'a' * 16: False, 'a' * 17: True,                          # a word of 16, of 17
            'k-' + 'a' * 16 + '-' + 'b' * 16: False,
            'k-20261114': False, 'k-202611140': True,                 # 8 digits in a row, 9
            'aB1-cD1': False, 'aB1-cD1-e2': True,                     # 4 changes in all, 5
            'aB1cD': False, 'aB1cDe': True,
        }
        for text, keyish in cases.items():
            with self.subTest(id=text):
                self.assertIs(validate.looks_like_a_key(text), keyish)

    def test_ordinary_ids_are_kept(self) -> None:
        for text in ('codex-key', 'login-B', 'gpt4o-mini', 'OpenAI-prod', 'claude-3-5-sonnet-20241022', 'c', 'x1',
                     'anthropic.work:key_2'):
            with self.subTest(id=text):
                self.assertIsNone(validate.id_problem(text))


@needs_posix
class AuditTest(TempStoreCase):
    """DeepSeek N3, N4 and N6, and the audit side of astra R4."""

    def test_n3_two_ids_with_one_value_are_both_named_once(self) -> None:
        root = self.base / 'same' / 'keys'
        KeyStore(root, system=FakeSystem()).add('first-key', 'openai', 'CODEX_API_KEY', MARKER,
                                                metered_spend_allowed=True, cap_usd=5)
        KeyStore(root, system=FakeSystem()).add('second-key', 'openai', 'CODEX_API_KEY', MARKER,
                                                metered_spend_allowed=True, cap_usd=5)
        state = self.base / 'same' / 'state'          # inside the config root too: one file under two roots
        (state / 'recursion').mkdir(parents=True)
        (state / 'recursion' / 'leak.log').write_text('token=' + MARKER + '\n')
        system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base), 'XDG_STATE_HOME': str(state)})
        report = audit_mod.run(KeyStore(root, system=system), journal=False)
        leak = str(state / 'recursion' / 'leak.log')
        self.assertEqual(sorted(report.found), [(leak, 'first-key'), (leak, 'second-key')])

    def test_n4_a_refused_audit_exits_3_a_finding_1(self) -> None:
        store = self.store()
        self.add(store)
        state = self.base / 'state'
        (state / 'recursion').mkdir(parents=True)
        (state / 'recursion' / 'run.log').write_text(MARKER)
        system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base), 'XDG_STATE_HOME': str(state)})
        status, text = run_main(KeyStore(self.root, system=system), 'audit', '--no-journal')
        self.assertEqual(status, 1)
        self.assertIn('FOUND: the value of codex-key', text)
        (self.root / 'index.json').write_text('{}\n')
        status, text = run_main(KeyStore(self.root, system=system), 'audit', '--no-journal')
        self.assertEqual(status, 3)
        self.assertIn('refused: index_invalid', text)
        status, _ = run_main(KeyStore(self.root, system=system), 'list')
        self.assertEqual(status, 1)                    # every other command refuses with 1, as before

    def test_a_keychain_that_does_not_answer_makes_the_audit_incomplete_not_refused(self) -> None:
        system, _ = keychain_system()
        self.add(KeyStore(self.root, system=system, clock=fixed_clock))
        gone = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base)})     # no session bus now
        self.assertEqual(KeyStore(self.root, system=gone).values(), ({}, {'codex-key': 'keychain_unavailable'}))
        status, text = run_main(KeyStore(self.root, system=gone), 'audit', '--no-journal')
        self.assertEqual(status, 4)
        self.assertIn('not searched for codex-key: its value could not be read (keychain_unavailable)', text)

    def test_a_value_that_cannot_be_read_makes_the_audit_incomplete(self) -> None:
        system, chain = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        self.add(store)
        chain.mode = 'locked'
        self.assertEqual(store.values(), ({}, {'codex-key': 'keychain_locked'}))

    def test_n6_a_temp_file_left_by_an_interrupted_save_is_a_finding_and_the_next_write_deletes_it(self) -> None:
        store = self.store()
        self.add(store)
        leftover = self.root / '.secrets.json.0123456789abcdef.tmp'
        leftover.write_bytes(b'')                      # empty, or a prefix of a key: either way it is named
        other = self.root / 'notes.tmp'
        other.write_bytes(b'')
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 1)
        self.assertIn(f'FOUND: a temp file an interrupted save left behind, which may hold part of a key: {leftover}',
                      text)
        self.assertNotIn(str(other), text)
        self.add(store, key_id='second', value=OTHER)
        self.assertFalse(leftover.exists())
        self.assertTrue(other.exists())                # only the store's own temp names are swept
        self.assertEqual(run_main(store, 'audit', '--no-journal')[0], 0)
        leftover.write_bytes(b'')
        store.remove('second')
        self.assertFalse(leftover.exists())


@needs_posix
class CredentialSourceTest(TempStoreCase):
    """DeepSeek N5 and N9."""

    def test_n5_an_unfinished_add_is_refused_operation_pending(self) -> None:
        system, chain = keychain_system()
        store = KeyStore(self.root, system=system, clock=fixed_clock)
        chain.after_store = Completed(-15, b'', b'')
        with self.assertRaises(KeyStoreError):
            self.add(store)
        self.assertNotIn('codex-key', store.list().keys)
        for env in ('CODEX_API_KEY', 'ANTHROPIC_API_KEY'):
            with self.assertRaises(KeyRefused) as refused:
                store.credential('codex-key', env)
            self.assertEqual(refused.exception.code, 'operation_pending')

    def test_n9_a_plain_key_error_carries_no_context(self) -> None:
        store = self.store()
        self.add(store)
        for connection_id, env in (('nobody', 'CODEX_API_KEY'), ('codex-key', 'ANTHROPIC_API_KEY')):
            with self.subTest(connection_id=connection_id, env=env):
                with self.assertRaises(KeyError) as caught:
                    store.credential(connection_id, env)
                self.assertIs(type(caught.exception), KeyError)
                self.assertEqual(caught.exception.args, (connection_id,))
                self.assertIsNone(caught.exception.__context__)
                self.assertIsNone(caught.exception.__cause__)


@needs_posix
class InterruptAndRegistrantTest(TempStoreCase):
    """DeepSeek N8 and N10."""

    def test_n8_ctrl_c_ends_a_command_with_130_and_no_traceback(self) -> None:
        store = self.store()
        with mock.patch.object(KeyStore, 'list', side_effect=KeyboardInterrupt):
            status, text = run_main(store, 'list')
        self.assertEqual(status, 130)
        self.assertIn('interrupted: nothing more was done', text)
        self.assertNotIn('Traceback', text)

    def test_n10_a_hooks_object_with_no_command_table_fails_before_anything_is_set(self) -> None:
        class Half:
            def __init__(self) -> None:
                self.set_calls = []

            def set(self, name, value) -> None:
                self.set_calls.append(name)

        half = Half()
        with self.assertRaises(AttributeError):
            hooks.register(half)
        self.assertEqual(half.set_calls, [])
