"""What a person reads about forget: every sentence on when a value is read is true, and each refusal names the key.

With the owner-only file, ``forget`` of an id that only an unfinished save names, and ``remove`` of an unknown id,
read that file to learn which ids it has, and the file holds the values: the help, the README and the store's own
words said that forget reads no value. They now say it reads none from a keychain and name the file, and a test
reads the texts so that the plain sentence cannot come back alone; another counts the file's reads, so the words
stay tied to the code. A Secret Service that cannot be searched over the session bus made forget refuse in bus
terms only: the refusal now names the key, says that nothing was changed and says when to run it again; a host
whose bus cannot say whether a keychain is there says that one MAY answer. Stand-in keychains and busses only
(``support.FakeKeychain``, the stand-in ``busctl``); every key is made up; each test's own temporary folder; no
test reaches a real keychain, bus, file or terminal of any person.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from recursion_core.keys import cli, store as store_mod
from recursion_core.keys.backends import OwnerFile
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import KeyStore
from recursion_core.keys.tests.support import LOGIN_CREATED, MARKER, file_system, needs_posix
from recursion_core.keys.tests.test_review_8 import SAVED, SESSION, Terminal
from recursion_core.keys.tests.test_review_10 import Round, held

# A sentence that says no value is read, in any of the ways the texts have said it.
NO_VALUE = re.compile(r'\b(reads? no value|no value is read|reads? a value|reads? none)\b', re.I)
# The exception every such sentence about forget must carry: the owner-only file is read, and it holds the values.
FILE_READ = re.compile(r'with the owner-only file,? .*?reads? that file \(which holds the values\) to learn which ids '
                       r'it has, and shows? none', re.I)


def words(text: str) -> str:
    return re.sub(r'[\s`]+', ' ', text).strip()


def readme_row(name: str) -> str:
    readme = (Path(store_mod.__file__).parent / 'README.md').read_text(encoding='utf-8')
    rows = [line for line in readme.splitlines() if line.startswith(f'| `{name}')]
    return rows[0] if len(rows) == 1 else ''


def paragraph(doc: str, head: str) -> str:
    """The paragraph of ``doc`` from the line ``head`` to the next line that starts at the margin."""
    lines = doc.splitlines()
    at = [n for n, line in enumerate(lines) if line.strip() == head]
    if len(at) != 1:
        return ''
    end = next((n for n in range(at[0] + 1, len(lines)) if lines[n] and not lines[n][0].isspace()), len(lines))
    return '\n'.join(lines[at[0] + 1:end])


def store_forget_words() -> str:
    """The store's KEYCHAIN words on forget: the sentences of its docstring that name ``has_item``."""
    doc = words(store_mod.__doc__)
    return ' '.join(s for s in re.split(r'(?<=[.;])\s', doc) if 'has_item' in s or 'owner-only file, forget' in s)


class TheWordsOnForgetAreTrueTest(unittest.TestCase):
    """The three texts a person reads about forget, and the method that does the work."""

    def texts(self) -> dict:
        return {'cli.py forget': paragraph(cli.__doc__, '``forget <id>``'),
                'README forget row': readme_row('forget'),
                'store.py KEYCHAIN': store_forget_words(),
                'KeyStore._out_of_reach': KeyStore._out_of_reach.__doc__ or ''}

    def test_each_text_is_found(self) -> None:
        for name, text in self.texts().items():
            self.assertTrue(text, name)

    def test_no_text_says_no_value_is_read_without_naming_the_owner_only_file(self) -> None:
        for name, text in self.texts().items():
            plain = words(text)
            self.assertRegex(plain, FILE_READ, name)
            for sentence in re.split(r'(?<=[.;])\s', plain):
                if NO_VALUE.search(sentence) and 'owner-only file' not in sentence:
                    self.assertIn('from a keychain', sentence, f'{name}: {sentence}')

    def test_the_check_bites_on_the_plain_sentence(self) -> None:
        """The check above is not vacuous: the sentence these texts carried before is caught."""
        plain = words('It refuses while remove can still delete the key. It reads no value. The value may still sit')
        self.assertIsNone(FILE_READ.search(plain))
        self.assertTrue([s for s in re.split(r'(?<=[.;])\s', plain) if NO_VALUE.search(s)])


@needs_posix
class TheOwnerOnlyFileIsReadTest(Round):
    """What the words name is what the code does: forget of an unfinished save and remove of an unknown id read the
    owner-only file (and nothing else of it is shown)."""

    def test_forget_of_an_unfinished_save_and_remove_of_an_unknown_id_read_the_owner_only_file(self) -> None:
        store = KeyStore(self.base / 'file-config' / 'keys', system=file_system(), clock=lambda: SAVED + 0.25)
        self.add(store, key_id='file-key')
        path = store.root / 'index.json'
        doc = json.loads(path.read_bytes())
        saved = doc['keys'].pop('file-key')                  # only an unfinished add names the id now
        doc['pending']['file-key'] = {'op': 'add', 'started_at': saved['created_at'], 'save_id': saved['save_id']}
        path.write_bytes((json.dumps(doc, indent=1) + '\n').encode())
        for step, call, code in (('forget', lambda: store.forget('file-key'), 'value_reachable'),
                                 ('remove', lambda: store.remove('never-saved'), 'key_unknown')):
            with mock.patch.object(OwnerFile, '_read', autospec=True, side_effect=OwnerFile._read) as reads:
                with self.assertRaises(KeyStoreError) as caught:
                    call()
            self.assertEqual(caught.exception.code, code, step)
            self.assertGreaterEqual(reads.call_count, 1, step)
            self.assertNotIn(MARKER, caught.exception.detail, step)


@needs_posix
class ForgetRefusalsNameTheKeyTest(Round):
    def test_a_search_that_fails_names_the_key_says_nothing_was_changed_and_when_to_run_it_again(self) -> None:
        self.interrupted()
        before = (self.root / 'index.json').read_bytes()
        other, chain = self.sharing((SESSION, LOGIN_CREATED + 5))   # codex-key's save is asked for there
        for system, failing, key_id in ((self.system, self.chain, 'late-key'), (other, chain, 'codex-key')):
            failing.mode = 'fail'                            # the service answers on the bus and fails to search
            status, out, err = self.run_main(system, 'forget', key_id, terminal=Terminal([key_id]))
            self.assertEqual((status, out), (1, ''), key_id)
            self.assertIn(f'refused: keychain_failed: {key_id} may still be in a keychain that answers here, which '
                          'cannot be asked now (the Secret Service could not be asked over the session bus', err)
            self.assertIn('Nothing was changed', err)
            self.assertIn(f'Run "key forget {key_id}" again once the Secret Service answers on the session bus', err)
            failing.mode = 'ok'
        self.assertEqual(((self.root / 'index.json').read_bytes(), held(self.chain, 'late-key')), (before, 1))

    def test_a_bus_that_cannot_say_whether_a_keychain_is_there_says_one_may_answer(self) -> None:
        self.interrupted()
        before = (self.root / 'index.json').read_bytes()
        del self.system.tools['busctl']                      # neither busctl nor dbus-send is there to ask
        for key_id in ('late-key', 'codex-key'):
            status, out, err = self.run_main(self.system, 'forget', key_id, terminal=Terminal([key_id]))
            self.assertEqual((status, out), (1, ''), key_id)
            self.assertIn(f'refused: keychain_undetermined: {key_id} may still be in a keychain, for a keychain may '
                          'answer here and it cannot be asked now', err)
            self.assertNotIn('a keychain that answers here', err)
            self.assertIn('Nothing was changed', err)
            self.assertIn(f'Run "key forget {key_id}" again once busctl or dbus-send answers', err)
        self.assertEqual((self.root / 'index.json').read_bytes(), before)

    def test_the_other_unasked_refusals_keep_their_words(self) -> None:
        self.interrupted()
        del self.system.tools['secret-tool']
        status, out, err = self.run_main(self.system, 'forget', 'late-key', terminal=Terminal(['late-key']))
        self.assertEqual((status, out), (1, ''))
        self.assertIn('refused: keychain_tool_missing: late-key may still be in a keychain that answers here', err)
        self.assertTrue(err.rstrip().endswith('Nothing was changed'), err)


if __name__ == '__main__':
    unittest.main()
