"""Seventh answer round of lane KEYS: three points of an outside read, each reproduced on stand-ins first.

R1  a recovery of an old intent, and the rollback of a failed add, delete only the value of the save the intent was
    written for, never a newer or an older save of the same id (the intent records its save id in ``index.json``);
    an index written before that field is still read.
R2  the audit's report masks a looked-for value wherever it occurs in a path it prints.
R4  a provider or a variable name shaped like a key is refused by ``add``; ordinary names are still accepted.

Every value here is a made-up stand-in; every store is in a temporary folder or in memory; no real keychain, bus,
terminal or file outside the test's own temporary folder is touched.
"""
from __future__ import annotations

import io
import json
from unittest import mock

from recursion_core.keys import __main__ as entry
from recursion_core.keys.backends import OwnerFile, SecretService
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.tests.support import FakeSystem, TempStoreCase, keychain_system, needs_posix

V1 = 'standin-one-4b7e1d-notakey'
V2 = 'standin-two-9c0a3f-notakey'
V3 = 'standin-three-5d2e8b-notakey'
HEXTOK = '0f1e2d3c4b5a69788796a5b4c3d2e1f0'          # 32 lower-case hex, made up
ENVTOK = 'QZXWVU9T8S7R6Q5P4O3N2M1LKJHGFEDC'          # upper-case letters and digits, made up


def owner_values(root) -> dict:
    data = json.loads((root / 'secrets.json').read_text())
    return {kid: entry['value'] for kid, entry in data['secrets'].items()}


def chain_values(chain, key_id) -> list:
    """The secrets of every stand-in keychain item saved for ``key_id``."""
    out = []
    for words, secret in chain.items.items():
        pairs = dict(zip(words[0::2], words[1::2]))
        if pairs.get('recursion_key_id') == key_id:
            out.append(secret.decode('ascii'))
    return sorted(out)


def forced_delete_failure(cls):
    return mock.patch.object(cls, 'delete', side_effect=KeyStoreError('keychain_failed', 'forced'))


@needs_posix
class R1RecoveryDeletesOnlyItsOwnSave(TempStoreCase):
    """The outside read's sequence: an old remove intent comes back with an older index.json, and a later write
    recovers it. Before this change the recovery deleted every value of the id, the newer save's included."""

    def sequence(self, st, backend_cls, *, then: str, restore: bool = True, strip_save_id: bool = False):
        self.add(st, 'beta', V3, env='OPENAI_API_KEY')
        self.add(st, 'alpha', V1)
        with forced_delete_failure(backend_cls):
            with self.assertRaises(KeyStoreError):
                st.remove('alpha')                          # the remove intent is written, the backend not reached
        snap = (self.root / 'index.json').read_bytes()
        st.remove('alpha')                                  # the person finishes the removal
        self.add(st, 'alpha', V2)                           # and later saves a NEW alpha
        self.assertEqual(st.credential('alpha', 'CODEX_API_KEY'), V2)
        if restore:
            if strip_save_id:                               # an index written before intents carried a save id
                doc = json.loads(snap)
                for entry in doc['pending'].values():
                    entry.pop('save_id', None)
                snap = (json.dumps(doc, indent=1) + '\n').encode()
            (self.root / 'index.json').write_bytes(snap)    # an older copy of the index is put back
        if then == 'save':
            self.add(st, 'gamma', 'standin-four-7a1c2e-notakey', env='XAI_API_KEY', provider='xai')
        else:
            st.remove('beta')                               # a removal of another key recovers too

    def test_owner_file_a_later_save_keeps_the_newer_alpha(self):
        st = self.store()
        self.sequence(st, OwnerFile, then='save')
        self.assertEqual(owner_values(self.root).get('alpha'), V2)
        self.assertEqual(st.list().pending, {})            # the old intent is finished, not left behind

    def test_owner_file_a_later_removal_of_another_key_keeps_the_newer_alpha(self):
        st = self.store()
        self.sequence(st, OwnerFile, then='remove')
        self.assertEqual(owner_values(self.root).get('alpha'), V2)
        self.assertNotIn('beta', owner_values(self.root))

    def test_keychain_a_later_save_keeps_the_newer_alpha(self):
        system, chain = keychain_system()
        st = self.store(system)
        self.sequence(st, SecretService, then='save')
        self.assertEqual(chain_values(chain, 'alpha'), [V2])

    def test_keychain_a_later_removal_of_another_key_keeps_the_newer_alpha(self):
        system, chain = keychain_system()
        st = self.store(system)
        self.sequence(st, SecretService, then='remove')
        self.assertEqual(chain_values(chain, 'alpha'), [V2])
        self.assertEqual(chain_values(chain, 'beta'), [])

    def test_control_without_the_put_back_both_survive(self):
        st = self.store()
        self.sequence(st, OwnerFile, then='save', restore=False)
        self.assertEqual(owner_values(self.root).get('alpha'), V2)

    def test_the_remove_intent_records_the_save_it_was_written_for(self):
        st = self.store()
        record = self.add(st, 'alpha', V1)
        with forced_delete_failure(OwnerFile):
            with self.assertRaises(KeyStoreError):
                st.remove('alpha')
        doc = json.loads((self.root / 'index.json').read_text())
        self.assertEqual(doc['pending']['alpha']['op'], 'remove')
        self.assertEqual(doc['pending']['alpha']['save_id'], record.save_id)
        self.assertEqual(st.list().pending['alpha'].save_id, record.save_id)

    def test_an_index_written_before_the_field_is_read_and_its_record_names_the_save(self):
        # An old remove intent (no save id) beside its record: the recovery takes the record's save, so the newer
        # value, saved under another save id, stays.
        st = self.store()
        self.sequence(st, OwnerFile, then='save', strip_save_id=True)
        self.assertEqual(owner_values(self.root).get('alpha'), V2)
        self.assertEqual(st.list().pending, {})

    def test_an_old_intent_with_no_record_and_a_value_of_its_id_is_left_and_refused(self):
        # An old add intent with no record and no save id: which save it was for cannot be told, so while the
        # keychain holds a value of the id the recovery leaves the intent (the id is refused operation_pending)
        # and deletes nothing; "remove <id>" clears it on request.
        system, chain = keychain_system()
        st = self.store(system)
        self.add(st, 'seed', V3, env='OPENAI_API_KEY')
        self.add(st, 'alpha', V1)
        doc = json.loads((self.root / 'index.json').read_text())
        del doc['keys']['alpha']
        doc['pending'] = {'alpha': {'op': 'add', 'started_at': '2025-11-14T22:40:00.250Z'}}
        (self.root / 'index.json').write_bytes((json.dumps(doc, indent=1) + '\n').encode())
        self.assertEqual(st.list().pending['alpha'].save_id, None)          # the older index is still read
        self.add(st, 'gamma', V2, env='XAI_API_KEY', provider='xai')       # runs the recovery
        self.assertEqual(chain_values(chain, 'alpha'), [V1])
        self.assertEqual(sorted(st.list().pending), ['alpha'])
        with self.assertRaises(KeyStoreError) as refused:
            self.add(st, 'alpha', V2)
        self.assertEqual(refused.exception.code, 'operation_pending')
        self.assertIsNone(st.remove('alpha'))
        self.assertEqual(chain_values(chain, 'alpha'), [])
        self.assertEqual(st.list().pending, {})

    def test_keychain_rollback_of_a_failed_add_keeps_an_older_save_of_the_id(self):
        system, chain = keychain_system()
        st = self.store(system)
        self.add(st, 'seed', V3)
        snap = (self.root / 'index.json').read_bytes()
        self.add(st, 'alpha', V1)
        (self.root / 'index.json').write_bytes(snap)        # the older alpha item now has no record
        original = KeyStore.__dict__['_write_index'].__func__
        calls = {'n': 0}

        def failing_second_write(folder, index):
            calls['n'] += 1
            if calls['n'] == 2:                             # the write that publishes the new save
                raise OSError(5, 'forced')
            return original(KeyStore, folder, index)
        with mock.patch.object(KeyStore, '_write_index', staticmethod(failing_second_write)):
            with self.assertRaises(OSError):
                self.add(st, 'alpha', V2)
        self.assertEqual(chain_values(chain, 'alpha'), [V1])   # the new save rolled back, the older one kept
        self.assertEqual(st.list().pending, {})


@needs_posix
class R2AuditMasksValuesInPaths(TempStoreCase):
    def setUp(self):
        super().setUp()
        self.state = self.base / 'state'
        self.system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base), 'XDG_STATE_HOME': str(self.state)})

    def audit(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(['audit', '--no-journal', *argv], out=out, err=err, store=self.store(self.system))
        return status, out.getvalue() + err.getvalue()

    def test_a_value_in_a_file_name_is_not_printed(self):
        self.add(self.store(self.system), 'codex-key', V1)
        folder = self.state / 'recursion'
        folder.mkdir(parents=True)
        (folder / (V1 + '.log')).write_text('contents ' + V1)
        status, out = self.audit()
        self.assertEqual(status, 1)
        self.assertIn('FOUND: the value of codex-key is in', out)
        self.assertNotIn(V1, out)
        self.assertIn('<value of codex-key>.log', out)

    def test_a_value_in_the_audit_root_is_not_printed(self):
        self.add(self.store(self.system), 'codex-key', V1)
        root = self.base / ('cache-' + V1)
        root.mkdir()
        (root / 'x.txt').write_text('nothing here')
        status, out = self.audit('--path', str(root))
        self.assertEqual(status, 0)
        self.assertNotIn(V1, out)
        self.assertIn('cache-<value of codex-key>', out)

    def test_control_a_value_only_in_contents_names_the_plain_path(self):
        self.add(self.store(self.system), 'codex-key', V1)
        folder = self.state / 'recursion'
        folder.mkdir(parents=True)
        (folder / 'run.log').write_text('contents ' + V1)
        status, out = self.audit()
        self.assertEqual(status, 1)
        self.assertIn('run.log', out)
        self.assertNotIn(V1, out)


@needs_posix
class R4ProviderAndVariableShapedLikeAKey(TempStoreCase):
    def run_main(self, system, *argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(list(argv), out=out, err=err, store=self.store(system), stdin=stdin)
        return status, out.getvalue() + err.getvalue()

    def index_text(self) -> str:
        path = self.root / 'index.json'
        return path.read_text() if path.exists() else ''

    def test_a_token_as_provider_is_refused_and_not_repeated(self):
        system, chain = keychain_system()
        status, out = self.run_main(system, 'add', 'codex-key', '--provider', HEXTOK, '--env', 'CODEX_API_KEY',
                                    '--stdin', stdin=io.BytesIO(V1.encode() + b'\n'))
        self.assertEqual(status, 1)
        self.assertIn('provider_invalid', out)
        _, listed = self.run_main(system, 'list')
        self.assertNotIn(HEXTOK, out + listed + self.index_text())
        self.assertFalse(any(HEXTOK in ' '.join(argv) for argv, _ in system.calls))
        self.assertEqual(chain.items, {})

    def test_a_token_as_variable_name_is_refused_and_not_repeated(self):
        system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base)})
        status, out = self.run_main(system, 'add', 'codex-key', '--provider', 'openai', '--env', ENVTOK,
                                    '--stdin', stdin=io.BytesIO(V1.encode() + b'\n'))
        self.assertEqual(status, 1)
        self.assertIn('env_name_invalid', out)
        _, listed = self.run_main(system, 'list')
        self.assertNotIn(ENVTOK, out + listed + self.index_text())

    def test_the_store_refuses_both_directly(self):
        st = self.store()
        for provider, env, code in ((HEXTOK, 'CODEX_API_KEY', 'provider_invalid'),
                                    ('openai', ENVTOK, 'env_name_invalid')):
            with self.subTest(code=code):
                with self.assertRaises(KeyStoreError) as refused:
                    st.add('codex-key', provider, env, V1)
                self.assertEqual(refused.exception.code, code)
                self.assertNotIn(HEXTOK, str(refused.exception))
                self.assertNotIn(ENVTOK, str(refused.exception))

    def test_ordinary_names_are_still_accepted(self):
        st = self.store()
        providers = ('anthropic', 'openai', 'google', 'xai', 'refuseless', 'opencode', 'nous', 'cursor',
                     'my-vendor.v2')
        envs = ('ANTHROPIC_API_KEY', 'CODEX_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY', 'XAI_API_KEY')
        for n, provider in enumerate(providers):
            with self.subTest(provider=provider):
                self.assertEqual(self.add(st, f'p{n}', V1, provider=provider).provider, provider)
        for n, env in enumerate(envs):
            with self.subTest(env=env):
                self.assertEqual(self.add(st, f'e{n}', V2, env=env).env_name, env)


if __name__ == '__main__':
    import unittest
    unittest.main()
