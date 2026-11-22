"""The save id of an intent on the macOS Keychain: a recovery deletes only the save its intent was written for, as on
the other backends (``test_review_7.py``), and a removal the person asks for still deletes every save of the id. Every
key is made up; the stand-in ``security`` of ``test_macos.py`` plays the keychain."""
from __future__ import annotations

from unittest import mock

from recursion_core.keys.backends import MacKeychain
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.tests.support import needs_posix
from recursion_core.keys.tests.test_macos import MacCase

V1 = 'standin-one-4b7e1d-notakey'
V2 = 'standin-two-9c0a3f-notakey'
V3 = 'standin-three-5d2e8b-notakey'
V4 = 'standin-four-7a1c2e-notakey'


@needs_posix
class MacRecoveryKeepsTheNewerSaveTest(MacCase):
    def test_an_old_remove_intent_put_back_keeps_the_newer_save(self) -> None:
        st, system, security = self.mac_store()
        self.add(st, 'beta', V3, env='OPENAI_API_KEY')
        self.add(st, 'alpha', V1)
        with mock.patch.object(MacKeychain, 'delete', side_effect=KeyStoreError('keychain_failed', 'forced')):
            with self.assertRaises(KeyStoreError):
                st.remove('alpha')
        index = st.root / 'index.json'
        snap = index.read_bytes()                   # the index with the unfinished removal of the first save
        st.remove('alpha')
        self.add(st, 'alpha', V2)
        index.write_bytes(snap)                     # an older index put back: its intent names the first save
        self.add(st, 'gamma', V4, env='XAI_API_KEY', provider='xai')     # the next write recovers that intent
        alpha = [item['password'] for item in security.items if item['acct'] == 'alpha']
        self.assertEqual(alpha, [V2.encode()])
        self.assertEqual(st.list().pending, {})

    def test_a_removal_asked_for_still_deletes_every_save(self) -> None:
        st, system, security = self.mac_store()
        self.add(st, 'alpha', V1)
        self.assertEqual(st.remove('alpha').key_id, 'alpha')
        self.assertEqual([item for item in security.items if item['acct'] == 'alpha'], [])
