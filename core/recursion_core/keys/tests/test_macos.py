"""The macOS Keychain backend, against a stand-in ``security`` (no Mac here, so it has not run on one).

The stand-in follows ``security help`` and Apple's published source (``SecurityTool/macOS``, keychain_find.c,
security.c, readline.c), where they matter here:

- a trailing argument names the keychain; without one, ``add-generic-password`` saves to the DEFAULT keychain and
  ``find``/``delete``/``dump-keychain`` search the SEARCH LIST, which need not hold the login keychain;
- ``find-generic-password`` and ``delete-generic-password`` given both ``-a`` and ``-s`` search by those two alone
  (the primary key) and ignore ``-G`` and ``-j``; otherwise they match every attribute given; each acts on the first
  match; ``-w`` prints the password and a newline;
- ``-i`` reads each line into a 4096-byte buffer: at most 4095 bytes are kept and the rest of a longer line is read
  as the next command; a line is split like a shell line (quotes, backslash escapes); without ``-U`` an item of the
  same account and service is not replaced; its exit status does not report a command's failure;
- exit 44 is "item not found", which ``security`` also answers when its search failed (mode ``ambiguous``);
- ``dump-keychain`` (never given ``-d``) prints each item's attributes and no password, beside three items of
  other programs; its printer can stop early with a diagnostic on standard error while the exit status stays 0.

Every key is made up.
"""
from __future__ import annotations

import io
import json
import shlex
from dataclasses import replace

from recursion_core.keys import __main__ as entry
from recursion_core.keys import audit as audit_mod
from recursion_core.keys.backends import MacKeychain
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import Index, KeyStore, index_bytes
from recursion_core.keys.system import Completed, ToolMissing, ToolTimeout, detect
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    needs_posix,
)

MATCH = {'-a': 'acct', '-s': 'svce', '-G': 'gena', '-j': 'icmt'}
VALUED = {'-a', '-s', '-G', '-j', '-l', '-X', '-c', '-C', '-D', '-T'}
LOGIN = '/Users/someone/Library/Keychains/login.keychain-db'
ELSEWHERE = '/Users/someone/Library/Keychains/elsewhere.keychain-db'
TOOL = '/usr/bin/security'
NOT_FOUND = b'security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.\n'
OPEN_FAILED = b'security: SecKeychainOpen: The specified keychain could not be found.\n'


class FakeSecurity:
    def __init__(self) -> None:
        self.items: list = []       # each {'keychain', 'acct', 'svce', 'gena', 'icmt', 'labl', 'password'}
        # ok | locked | fails | drops | mangles | stuck | garbled | bare | hexform | ambiguous | writes_then_fails |
        # dump_error | dump_partial | elsewhere (default keychain and search list are not the login keychain) |
        # no_login | locks_after_add | bad_value
        self.mode = 'ok'
        self.after_dump = None      # called once after the next listing: another process acting between two
        self.login = LOGIN          # what login-keychain answers (another keychain can become the login one)
        self.dumps: list = []       # every dump-keychain command line
        self.lines: list = []       # every line -i was given, as it was split by the 4095-byte reader

    @staticmethod
    def _parse(words) -> tuple:
        options, rest, i = {}, [], 0
        while i < len(words):
            if words[i] in VALUED and i + 1 < len(words):
                options[words[i]] = words[i + 1]
                i += 2
            elif words[i].startswith('-'):
                options[words[i]] = True
                i += 1
            else:
                rest.append(words[i])
                i += 1
        return options, rest

    def _keychains(self, rest, *, adding=False):
        """The keychains a command acts on. A named one it cannot open: the search list instead, as Apple's
        ``keychain_create_array`` gives; the command then writes the open error on standard error."""
        if rest and rest[0] in (LOGIN, ELSEWHERE):
            return [rest[0]]
        self.fell_back = self.fell_back or bool(rest)
        if self.mode == 'elsewhere':
            return [ELSEWHERE]
        return [LOGIN]

    def _matches(self, options, keychains) -> list:
        given = {flag: value for flag, value in options.items() if flag in MATCH}
        if '-a' in given and '-s' in given:          # the primary key: -G and -j are not part of the search
            given = {flag: given[flag] for flag in ('-a', '-s')}
        wanted = {MATCH[flag]: value for flag, value in given.items()}
        return [item for item in self.items if item['keychain'] in keychains
                and all(item[name] == value for name, value in wanted.items())]

    def __call__(self, argv, input):
        """A command, and the keychain-open error on standard error when it fell back to the search list."""
        self.fell_back = False
        done = self._answer(argv, input)
        if self.fell_back:
            return Completed(done.returncode, done.stdout, OPEN_FAILED + done.stderr)
        return done

    def _answer(self, argv, input):
        if self.mode == 'locked':
            return Completed(36, b'', b'security: SecKeychainItemCopyContent: User interaction is not allowed.\n')
        if self.mode == 'fails':
            return Completed(1, b'', b'security: something went wrong with ' + MARKER.encode() + b'\n')
        command = argv[1]
        if command == 'login-keychain':
            if self.mode == 'no_login' or len(argv) != 2:
                return Completed(50, b'', b'security: SecKeychainCopyLogin: The specified keychain could not be '
                                          b'found.\n')
            return Completed(0, f'    "{self.login}"\n'.encode(), b'')
        if command == '-i':
            return self._interactive(input)
        options, rest = self._parse(argv[2:])
        if command == 'dump-keychain':
            self.dumps.append(list(argv))
            keychains = self._keychains(rest)
            if '-d' in options:
                return Completed(2, b'', b'usage\n')
            if self.mode == 'dump_error':
                return Completed(0, b'', b'security: SecKeychainItemCopyAttributesAndData: CSSM error\n')
            listing = self.dump(keychains)
            hook, self.after_dump = self.after_dump, None
            if hook is not None:
                hook()
            return Completed(0, listing, b'')
        keychains = self._keychains(rest)
        found = self._matches(options, keychains)
        if self.mode in ('ambiguous', 'writes_then_fails'):   # a search that failed answers "not found" too
            return Completed(44, b'', b'security: SecKeychainSearchCopyNext: CSSM_ERRCODE_INTERNAL_ERROR\n' + NOT_FOUND)
        if command == 'find-generic-password':
            if not found:
                return Completed(44, b'', NOT_FOUND)
            if '-w' in options:
                if self.mode == 'bad_value':
                    return Completed(0, b'not a key\n', b'')            # a value no save of this store wrote
                return Completed(0, found[0]['password'] + b'\n', b'')
            return Completed(0, f'keychain: "{found[0]["keychain"]}"\nclass: "genp"\n'.encode(), b'')
        if command == 'delete-generic-password':
            if not found:
                return Completed(44, b'', NOT_FOUND)
            if self.mode != 'stuck':
                self.items.remove(found[0])
            return Completed(0, b'', b'password has been deleted.\n')
        return Completed(2, b'', b'usage\n')

    def _interactive(self, data: bytes) -> Completed:
        lines = []
        for line in data.decode('utf-8').split('\n'):
            while len(line.encode('utf-8')) > 4095:         # the reader keeps 4095 bytes; the rest is the next line
                lines.append(line[:4095])
                line = line[4095:]
            lines.append(line)
        self.lines.extend(lines)
        for line in lines:
            try:
                words = shlex.split(line)
            except ValueError:
                continue                                      # an unbalanced quote: that command is not run
            if not words or words[0] != 'add-generic-password' or '-U' in words:
                continue                                      # -i goes on whatever a line does
            options, rest = self._parse(words[1:])
            keychains = self._keychains(rest)
            try:
                password = bytes.fromhex(options['-X'])
            except (KeyError, ValueError):
                continue
            item = {name: options.get(flag) for flag, name in MATCH.items()}
            item.update(keychain=keychains[0], labl=options.get('-l'), password=password)
            if any(old['keychain'] == item['keychain'] and old['acct'] == item['acct'] and old['svce'] == item['svce']
                   for old in self.items):
                continue                                      # "already exists": not replaced without -U
            if self.mode == 'mangles':
                item['password'] = item['password'][:-1]
            if self.mode != 'drops':
                self.items.append(item)
        if self.mode == 'writes_then_fails':
            return Completed(1, b'', b'security: SecKeychainItemCreateFromContent: an error\n')
        if self.mode == 'locks_after_add':
            self.mode = 'locked'                                        # the keychain locks before the read-back
        return Completed(0, b'', b'')

    OTHERS = (f'keychain: "{LOGIN}"\nversion: 512\nclass: "inet"\n'
              'attributes:\n    "acct"<blob>="codex-key"\n    "srvr"<blob>="api.example.org"\n'
              f'keychain: "{LOGIN}"\nversion: 512\nclass: "genp"\n'
              'attributes:\n    0x00000007 <blob>="Example Mail"\n    "acct"<blob>="codex-key"\n'
              '    "gena"<blob>=<NULL>\n    "icmt"<blob>=<NULL>\n    "svce"<blob>="com.example.mail"\n'
              f'keychain: "{LOGIN}"\nversion: 512\nclass: "genp"\n'
              'attributes:\n    "acct"<blob>="codex-key"\n    "svce"<blob>="recursion.not-a-save"\n')

    def dump(self, keychains=(LOGIN,)) -> bytes:
        """``security dump-keychain`` without ``-d``: each item's attributes, never its password."""
        if self.mode == 'garbled':
            return b'not a keychain dump\n'
        text = '' if self.mode == 'bare' or LOGIN not in keychains else self.OTHERS
        for item in self.items:
            if item['keychain'] not in keychains:
                continue
            service = item['svce']
            shown = f'0x{service.encode().hex().upper()}  "{service}"' if self.mode == 'hexform' else f'"{service}"'
            text += (f'keychain: "{item["keychain"]}"\nversion: 512\nclass: "genp"\n'
                     f'attributes:\n    0x00000007 <blob>="{item["labl"]}"\n    0x00000008 <blob>=<NULL>\n')
            if self.mode == 'dump_partial':
                continue                                      # the printer stopped early on this item
            text += (f'    "acct"<blob>="{item["acct"]}"\n'
                     '    "cdat"<timedate>=0x32303235313131343232343030305A00  "20251114224000Z\\000"\n'
                     f'    "gena"<blob>="{item["gena"]}"\n    "icmt"<blob>="{item["icmt"]}"\n'
                     f'    "svce"<blob>={shown}\n    "type"<uint32>=<NULL>\n')
        return text.encode()


def mac_system():
    security = FakeSecurity()
    return FakeSystem(platform='darwin', tools={'security': security}), security


def fixed_clock() -> float:
    return 1_763_160_000.25


def run_main(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    status = entry.main(list(argv), out=out, err=err, store=store)
    return status, out.getvalue() + err.getvalue()


class MacCase(TempStoreCase):
    def mac_store(self, name='keys', system=None):
        if system is None:
            system, security = mac_system()
        else:
            security = system.tools['security']
        return KeyStore(self.base / name / 'keys', system=system, clock=fixed_clock), system, security


@needs_posix
class MacKeychainTest(MacCase):
    def test_round_trip_with_the_key_never_in_the_arguments(self) -> None:
        store, system, security = self.mac_store()
        record = self.add(store)
        index = store.list()
        self.assertEqual(index.backend, 'macos_keychain')
        self.assertFalse((store.root / 'secrets.json').exists())
        self.assertEqual(security.items, [{
            'keychain': LOGIN, 'acct': 'codex-key', 'svce': f'recursion.{index.store_id}.{record.save_id}.openai',
            'gena': index.store_id, 'icmt': 'openai', 'labl': 'Recursion API key codex-key',
            'password': MARKER.encode()}])
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        self.assertEqual(store.remove('codex-key').key_id, 'codex-key')
        self.assertEqual(security.items, [])
        for argv, stdin in system.calls:
            self.assertNotIn(MARKER, ' '.join(argv))
            self.assertNotIn(MARKER.encode().hex(), ' '.join(argv))
            if stdin is not None:
                self.assertNotIn(MARKER.encode(), stdin)                 # only its hexadecimal form
                self.assertIn(MARKER.encode().hex().encode(), stdin)

    def test_a_value_is_released_only_to_the_record_of_its_save(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        older = (store.root / 'index.json').read_bytes()
        store.remove('codex-key')
        self.add(store, value=OTHER)                                     # another save of the same id
        (store.root / 'index.json').write_bytes(older)                   # the older index put back
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')       # never the other save's value
        self.assertEqual(store.remove('codex-key').key_id, 'codex-key')  # every save of the id goes
        self.assertEqual(security.items, [])

    def test_a_save_or_a_removal_touches_only_its_own_id_and_store(self) -> None:
        store, system, security = self.mac_store()
        other, _, _ = self.mac_store('other', system)
        self.add(store, key_id='alpha')
        self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        self.add(other, key_id='alpha', value=OTHER)                     # the same id in another store
        store.remove('alpha')
        self.assertEqual(sorted((item['acct'], item['gena']) for item in security.items),
                         sorted([('beta', store.list().store_id), ('alpha', other.list().store_id)]))
        self.assertEqual(other.credential('alpha', 'CODEX_API_KEY'), OTHER)
        self.assertEqual(store.credential('beta', 'ANTHROPIC_API_KEY'), OTHER)

    def test_no_save_replaces_another_and_a_removal_takes_every_save_of_the_id(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        store_id = store.list().store_id
        (store.root / 'index.json').write_bytes(index_bytes(Index(None, {}, {}, store_id)))     # emptied
        self.add(store, value=OTHER)
        self.assertEqual(sorted(item['password'] for item in security.items), sorted([MARKER.encode(), OTHER.encode()]))
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), OTHER)
        store.remove('codex-key')
        self.assertEqual(security.items, [])

    def test_a_save_the_keychain_did_not_keep_is_refused_and_only_its_own_item_taken_out(self) -> None:
        for mode in ('drops', 'mangles'):
            with self.subTest(mode=mode):
                store, _, security = self.mac_store(mode)
                security.mode = mode
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(store)
                self.assertEqual(caught.exception.code, 'keychain_failed')
                self.assertEqual((security.items, store.list().keys, store.list().pending), ([], {}, {}))
        store, _, security = self.mac_store('orphan')
        self.add(store, key_id='alpha')
        store_id = store.list().store_id
        (store.root / 'index.json').write_bytes(index_bytes(Index(None, {}, {}, store_id)))     # emptied
        security.mode = 'mangles'
        with self.assertRaises(KeyStoreError):
            self.add(store, key_id='alpha', value=OTHER)
        self.assertEqual([item['password'] for item in security.items], [MARKER.encode()])     # the earlier save's
        self.assertEqual(sorted(store.list().pending), ['alpha'])        # its item is there: the intent stays
        security.mode = 'ok'
        self.assertIsNone(store.remove('alpha'))
        self.assertEqual(security.items, [])

    def test_a_locked_keychain_is_refused_and_never_answered_from_the_file(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.mode = 'locked'
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'keychain_locked')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'keychain_locked')
        self.assertFalse((store.root / 'secrets.json').exists())

    def test_a_vanished_item_is_secret_missing_and_can_still_be_removed(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.items.clear()
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')
        store.remove('codex-key')
        self.assertEqual(store.list().keys, {})

    def test_other_failures_are_keychain_failed_with_fixed_text(self) -> None:
        store, _, security = self.mac_store()
        security.mode = 'fails'
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertNotIn(MARKER, str(caught.exception))
        self.assertNotIn(MARKER.encode().hex(), str(caught.exception))
        self.assertNotIn('something went wrong', str(caught.exception))  # nothing the tool printed

    def test_an_item_that_does_not_go_keeps_its_record(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.mode = 'stuck'
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertIn('codex-key', store.list().keys)
        self.assertEqual(json.loads((store.root / 'index.json').read_text())['pending']['codex-key']['op'], 'remove')


@needs_posix
class MacNativeSemanticsTest(MacCase):
    """Codex astra, #422 round 1: the stand-in now follows Apple's ``security`` where the backend relies on it."""

    def test_r1_a_record_whose_store_or_provider_was_edited_gets_nothing(self) -> None:
        store, _, _ = self.mac_store()
        self.add(store)
        index = store.list()
        record = index.keys['codex-key']
        good = (store.root / 'index.json').read_bytes()
        edits = {'provider': replace(index, keys={'codex-key': replace(record, provider='anthropic')}),
                 'store': replace(index, store_id='0' * 32)}
        for what, edited in edits.items():
            with self.subTest(edited=what):
                (store.root / 'index.json').write_bytes(index_bytes(edited))
                with self.assertRaises(KeyRefused) as refused:
                    store.credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(refused.exception.code, 'secret_missing')
        (store.root / 'index.json').write_bytes(good)
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_r1_the_stand_in_ignores_generic_and_comment_beside_account_and_service(self) -> None:
        _, _, security = self.mac_store()
        security.items.append({'keychain': LOGIN, 'acct': 'a', 'svce': 's', 'gena': 'g', 'icmt': 'c', 'labl': 'l',
                               'password': OTHER.encode()})
        done = security([TOOL, 'find-generic-password', '-a', 'a', '-s', 's', '-G', 'other', '-j', 'other', '-w',
                         LOGIN], None)
        self.assertEqual((done.returncode, done.stdout), (0, OTHER.encode() + b'\n'))   # as Apple's primary-key search

    def test_r2_a_listing_with_a_diagnostic_or_a_cut_item_is_refused_never_counted_as_empty(self) -> None:
        for mode in ('dump_error', 'dump_partial'):
            with self.subTest(mode=mode):
                store, _, security = self.mac_store(mode)
                self.add(store)
                (store.root / 'index.json').unlink()                      # an item no record names
                security.mode = mode
                with self.assertRaises(KeyStoreError) as caught:
                    store.unnamed_items()
                self.assertEqual(caught.exception.code, 'keychain_failed')
                report = audit_mod.run(store, journal=False)
                self.assertEqual(report.unnamed_refused, 'keychain_failed')
                self.assertFalse(report.complete())
        store, _, security = self.mac_store('empty')
        security.mode = 'bare'
        self.assertEqual(store.unnamed_items(), 0)                        # a truly empty keychain is still 0

    def test_r3_every_command_names_the_login_keychain_whatever_the_default_and_search_list(self) -> None:
        store, system, security = self.mac_store()
        security.mode = 'elsewhere'                   # the default keychain and the search list are another one
        self.add(store)
        self.assertEqual([item['keychain'] for item in security.items], [LOGIN])
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        self.assertEqual(store.unnamed_items(), 0)
        store.remove('codex-key')
        self.assertEqual(security.items, [])
        for argv, stdin in system.calls:
            if argv[1] in ('find-generic-password', 'delete-generic-password', 'dump-keychain'):
                self.assertEqual(argv[-1], LOGIN)
            elif argv[1] == '-i':
                self.assertTrue(stdin.rstrip(b'\n').endswith(f'"{LOGIN}"'.encode()))
            else:
                self.assertEqual(argv[1:], ['login-keychain'])

    def test_r3_no_login_keychain_is_refused_and_nothing_is_written_or_lost(self) -> None:
        store, _, security = self.mac_store()
        security.mode = 'no_login'
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertEqual((security.items, store.list().keys, store.list().pending), ([], {}, {}))
        security.mode = 'ok'
        self.add(store)
        security.mode = 'no_login'
        with self.assertRaises(KeyStoreError):
            store.remove('codex-key')
        self.assertIn('codex-key', store.list().keys)                     # the record stays while the item may
        self.assertEqual(len(security.items), 1)

    def test_r4_the_tool_is_apples_own_at_its_fixed_path_whatever_path_finds(self) -> None:
        class ElsewhereOnPath(FakeSystem):
            def which(self, name):
                return f'/opt/other/bin/{name}' if name in self.tools else None

        security = FakeSecurity()
        system = ElsewhereOnPath(platform='darwin', tools={'security': security})
        store = KeyStore(self.base / 'path' / 'keys', system=system, clock=fixed_clock)
        self.add(store)
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        self.assertEqual({argv[0] for argv, _ in system.calls}, {TOOL})

        class NoneOnPath(FakeSystem):
            def which(self, name):
                return None

        self.assertEqual(detect(NoneOnPath(platform='darwin', tools={'security': FakeSecurity()})), 'macos_keychain')

    def test_r5_a_key_longer_than_the_interactive_line_is_refused_before_anything_is_written(self) -> None:
        store, system, security = self.mac_store()
        self.add(store, key_id='first')                                   # the store exists and has its id
        longest = MacKeychain(system, store.list().store_id).longest('codex-key', 'openai')
        self.assertLess(longest, 4096)
        exact = 'k' * longest
        self.add(store, value=exact)                                      # exactly the longest: saved whole
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), exact)
        self.assertTrue(all(len(line.encode()) <= 4094 for line in security.lines))
        store.remove('codex-key')
        system.calls.clear()
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, value='k' * (longest + 1))
        self.assertEqual(caught.exception.code, 'value_invalid')
        self.assertIn(str(longest), str(caught.exception))
        self.assertEqual({argv[1] for argv, _ in system.calls}, {'login-keychain'})   # refused before the intent:
        # no line given to security, and no search for an item to undo
        self.assertEqual((sorted(store.list().keys), store.list().pending), (['first'], {}))
        self.assertEqual([item['acct'] for item in security.items], ['first'])

    def test_r8_a_timeout_and_a_vanished_tool_are_typed_with_constant_text(self) -> None:
        for raised, code in ((ToolTimeout, 'keychain_timeout'), (ToolMissing, 'keychain_tool_missing')):
            with self.subTest(code=code):
                def tool(argv, input, raised=raised):
                    raise raised('security')

                system = FakeSystem(platform='darwin', tools={'security': tool})
                store = KeyStore(self.base / code / 'keys', system=system, clock=fixed_clock)
                with self.assertRaises(KeyStoreError) as caught:
                    self.add(store)
                self.assertEqual(caught.exception.code, code)
                self.assertIsNone(caught.exception.__context__)
                self.assertNotIn(MARKER, str(caught.exception))
                self.assertEqual((store.list().keys, store.list().pending), ({}, {}))

    def test_r9_an_ambiguous_not_found_never_confirms_a_removal(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.mode = 'ambiguous'
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_failed')
        self.assertIn('codex-key', store.list().keys)
        self.assertEqual(store.list().pending['codex-key'].op, 'remove')
        self.assertEqual(len(security.items), 1)
        security.mode = 'ok'
        store.remove('codex-key')
        self.assertEqual((security.items, store.list().keys, store.list().pending), ([], {}, {}))

    def test_r9_a_failed_save_whose_item_may_be_there_keeps_its_intent(self) -> None:
        store, _, security = self.mac_store()
        security.mode = 'writes_then_fails'
        with self.assertRaises(KeyStoreError):
            self.add(store)
        self.assertEqual(sorted(store.list().pending), ['codex-key'])     # its item is there: the intent stays
        self.assertEqual(len(security.items), 1)
        security.mode = 'ok'
        self.assertIsNone(store.remove('codex-key'))
        self.assertEqual(security.items, [])


@needs_posix
class MacKeychainAuditTest(MacCase):
    """The audit counts the items no record names on the macOS Keychain too (``unnamed_items``, through
    ``dump-keychain``): an index lost, emptied or put back, or another store's items, never leave it complete. Found
    by lane KEYS' own attack on pull request 2 before its first push."""

    def test_an_emptied_index_leaves_items_the_audit_counts_until_a_removal_takes_them(self) -> None:
        store, _, _ = self.mac_store()
        self.add(store)
        self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        store_id = store.list().store_id
        (store.root / 'index.json').write_bytes(index_bytes(Index(None, {}, {}, store_id)))     # emptied
        self.assertEqual(store.unnamed_items(), 2)
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 4)
        self.assertIn('keychain: 2 item(s) in the macOS Keychain that Recursion saved and no saved key of this store '
                      'names', text)
        self.assertIn('Keychain Access finds the rest by the name "Recursion API key <id>"', text)
        self.assertNotIn(MARKER, text)
        self.add(store)                                   # a new save of the id: the old save's item is still no one's
        self.assertEqual(store.unnamed_items(), 2)
        store.remove('codex-key')                         # every save of the id in this store
        self.assertEqual(store.unnamed_items(), 1)        # beta's, which no saved key names

    def test_a_lost_index_leaves_an_item_the_audit_counts(self) -> None:
        store, _, _ = self.mac_store()
        self.add(store)
        (store.root / 'index.json').unlink()
        self.assertEqual(store.unnamed_items(), 1)
        self.assertEqual(run_main(store, 'audit', '--no-journal')[0], 4)
        self.add(store, value=OTHER)                      # a new store id: the old item is named by nothing
        self.assertEqual(store.unnamed_items(), 1)
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), OTHER)

    def test_a_put_back_index_leaves_the_newer_saves_item_counted(self) -> None:
        store, _, _ = self.mac_store()
        self.add(store)
        older = (store.root / 'index.json').read_bytes()
        store.remove('codex-key')
        self.add(store, value=OTHER)
        (store.root / 'index.json').write_bytes(older)
        self.assertEqual(store.unnamed_items(), 1)
        self.assertEqual(run_main(store, 'audit', '--no-journal')[0], 4)

    def test_another_stores_items_are_counted_as_on_the_secret_service(self) -> None:
        system, _ = mac_system()
        one, _, _ = self.mac_store('one', system)
        two, _, _ = self.mac_store('two', system)
        self.add(one)
        self.add(two, value=OTHER)
        self.assertEqual((one.unnamed_items(), two.unnamed_items()), (1, 1))      # questions.md K20, option (a)

    def test_the_count_reads_attributes_only_and_counts_no_other_programs_item(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        self.assertEqual(store.unnamed_items(), 0)        # beside items of other programs under the same account
        self.assertTrue(security.dumps)
        self.assertTrue(all(argv == [TOOL, 'dump-keychain', LOGIN] for argv in security.dumps))   # no -d
        self.assertNotIn(MARKER.encode(), security.dump())
        report = audit_mod.run(store, journal=False)
        self.assertEqual((report.unnamed, report.unnamed_refused, report.complete()), (0, None, True))

    def test_a_dump_that_fails_or_cannot_be_read_is_refused_and_the_audit_incomplete(self) -> None:
        for mode, code in (('locked', 'keychain_locked'), ('fails', 'keychain_failed'), ('garbled', 'keychain_failed')):
            with self.subTest(mode=mode):
                store, _, security = self.mac_store(mode)
                self.add(store)
                security.mode = mode
                with self.assertRaises(KeyStoreError) as caught:
                    store.unnamed_items()
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(MARKER, str(caught.exception))
                report = audit_mod.run(store, journal=False)
                self.assertEqual(report.unnamed_refused, code)
                self.assertFalse(report.complete())

    def test_an_empty_keychain_counts_none_and_a_service_printed_as_hexadecimal_is_read(self) -> None:
        store, _, security = self.mac_store()
        security.mode = 'bare'
        self.assertEqual(store.unnamed_items(), 0)        # an empty dump is an empty keychain, not a refusal
        security.mode = 'ok'
        self.add(store)
        (store.root / 'index.json').unlink()
        security.mode = 'hexform'
        self.assertEqual(store.unnamed_items(), 1)

    def test_an_unfinished_operation_names_only_its_own_stores_items_of_the_id(self) -> None:
        system, security = mac_system()
        one, _, _ = self.mac_store('one', system)
        two, _, _ = self.mac_store('two', system)
        self.add(one)
        self.add(two, value=OTHER)                        # the same id in another store
        security.mode = 'stuck'
        with self.assertRaises(KeyStoreError):
            one.remove('codex-key')                       # its item stays, and the removal stays unfinished
        security.mode = 'ok'
        self.assertEqual(sorted(one.list().pending), ['codex-key'])
        self.assertEqual(one.unnamed_items(), 1)          # the other store's item: the operation names only its own


@needs_posix
class MacSolTest(MacCase):
    """Codex sol, #422 round 1: R4 (one listing), R7 (a read-back keeps its own refusal), R9 (rules no test held)."""

    def test_r4_the_audit_counts_from_one_listing_so_a_change_between_two_cannot_hide_an_item(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        self.add(store, key_id='beta', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic')
        index = store.list()
        lost_beta = replace(index, keys={'codex-key': index.keys['codex-key']})
        (store.root / 'index.json').write_bytes(index_bytes(lost_beta))   # beta's item is no one's from now on

        def a_copied_store_removes_codex():
            security.items[:] = [item for item in security.items if item['acct'] != 'codex-key']

        security.after_dump = a_copied_store_removes_codex
        before = len(security.dumps)
        self.assertEqual(store.unnamed_items(), 1)                        # beta, whatever happened after the listing
        self.assertEqual(len(security.dumps) - before, 1)

    def test_r7_a_keychain_that_locks_before_the_read_back_keeps_its_code_and_the_intent(self) -> None:
        store, _, security = self.mac_store()
        security.mode = 'locks_after_add'
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store)
        self.assertEqual(caught.exception.code, 'keychain_locked')
        self.assertEqual(sorted(store.list().pending), ['codex-key'])     # its item may be there: the intent stays
        self.assertEqual(len(security.items), 1)
        security.mode = 'ok'
        self.assertIsNone(store.remove('codex-key'))
        self.assertEqual(security.items, [])

    def test_r9_a_record_moved_from_an_unpublished_provider_to_a_published_one_gets_nothing(self) -> None:
        store, _, _ = self.mac_store()
        self.add(store, provider='mistral')                               # kept and listed, handed to no engine
        index = store.list()
        moved = replace(index, keys={'codex-key': replace(index.keys['codex-key'], provider='openai')})
        (store.root / 'index.json').write_bytes(index_bytes(moved))
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'secret_missing')

    def test_r9_a_value_read_back_that_no_save_wrote_is_refused(self) -> None:
        store, _, security = self.mac_store()
        self.add(store)
        security.mode = 'bad_value'
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'value_invalid')

    def test_r9_the_audit_names_an_item_only_by_its_records_provider_too(self) -> None:
        store, _, _ = self.mac_store()
        self.add(store)
        self.assertEqual(store.unnamed_items(), 0)
        index = store.list()
        edited = replace(index, keys={'codex-key': replace(index.keys['codex-key'], provider='anthropic')})
        (store.root / 'index.json').write_bytes(index_bytes(edited))
        self.assertEqual(store.unnamed_items(), 1)                        # the item is not that record's save
