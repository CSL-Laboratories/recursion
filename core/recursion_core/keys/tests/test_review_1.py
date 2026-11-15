"""Regressions for the first reads of draft #386 (two Codex reads, gpt-6-astra and gpt-6.1-sol, and a DeepSeek
break-it read). Each test names the finding it pins; several follow the readers' own probes. Every key is made up."""
from __future__ import annotations

import argparse
import copy
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

from recursion_core.keys import __main__ as entry
from recursion_core.keys import cli
from recursion_core.keys.backends import OWNER_FILE_MAX, OwnerFile
from recursion_core.keys.errors import KeyRefused, KeyStoreError
from recursion_core.keys.store import INDEX_MAX, KeyStore
from recursion_core.keys.system import Completed, System, ToolTimeout
from recursion_core.keys.tests.support import (
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    busctl,
    keychain_system,
    needs_posix,
)


def run_main(store, *argv):
    out, err = io.StringIO(), io.StringIO()
    status = entry.main(list(argv), out=out, err=err, store=store)
    return status, out.getvalue() + err.getvalue()


@needs_posix
class ConsistentReadTest(TempStoreCase):
    """R1 (both Codex reads); F1 (DeepSeek): one state of the store per read, and no refusal for a concurrent save."""

    def test_a_key_removed_and_added_again_meanwhile_is_never_handed_out_under_the_old_one(self) -> None:
        store = self.store()
        self.add(store)
        reached, resume = threading.Event(), threading.Event()
        original = OwnerFile.get
        results, writer_done = [], threading.Event()

        def paused_get(backend, key_id, folder, save_id=None):
            reached.set()
            resume.wait()
            return original(backend, key_id, folder, save_id)

        def read():
            try:
                results.append(store.credential('codex-key', 'CODEX_API_KEY'))
            except KeyError as exc:
                results.append(type(exc).__name__)

        def replace():
            store.remove('codex-key')
            self.add(store, value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic', cap=0)
            writer_done.set()

        with mock.patch.object(OwnerFile, 'get', paused_get):
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reached.wait()
            writer = threading.Thread(target=replace, daemon=True)
            writer.start()
            try:
                # Time is used only to see that the writer has NOT got past the reader's lock yet.
                writer_blocked = not writer_done.wait(0.3)
            finally:
                resume.set()
            reader.join()
            writer.join()
        self.assertTrue(writer_blocked, 'the writer replaced the key while the reader held it')
        self.assertEqual(results, [MARKER])                 # the value the reader was authorized for, never OTHER
        with self.assertRaises(KeyError):
            store.credential('codex-key', 'CODEX_API_KEY')
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'ANTHROPIC_API_KEY')
        self.assertEqual(refused.exception.code, 'spend_not_allowed')

    def test_readers_do_not_wait_for_each_other(self) -> None:
        # The lock a reader takes is shared: a route starting while another reads its key is not held up.
        store = self.store()
        self.add(store)
        reached, resume, listed = threading.Event(), threading.Event(), threading.Event()
        original = OwnerFile.get

        def paused_get(backend, key_id, folder, save_id=None):
            reached.set()
            resume.wait()
            return original(backend, key_id, folder, save_id)

        with mock.patch.object(OwnerFile, 'get', paused_get):
            first = threading.Thread(target=lambda: store.credential('codex-key', 'CODEX_API_KEY'), daemon=True)
            first.start()
            reached.wait()
            second = threading.Thread(target=lambda: (store.list(), listed.set()), daemon=True)
            second.start()
            try:
                # A guard, not a timing claim: with a shared lock the second read finishes at once.
                self.assertTrue(listed.wait(60), 'a reader waited for another reader')
            finally:
                resume.set()
            first.join()
            second.join()

    def test_list_waits_for_a_writer_instead_of_reading_a_replaced_file(self) -> None:
        import fcntl

        store = self.store()
        self.add(store)
        fd = os.open(self.root / '.lock', os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)                      # a writer holds the folder
        listed = threading.Event()
        thread = threading.Thread(target=lambda: (store.list(), listed.set()))
        thread.start()
        try:
            self.assertFalse(listed.wait(0.3))              # list does not read under a writer
        finally:
            os.close(fd)
        thread.join()
        self.assertTrue(listed.is_set())

    def test_reads_create_nothing_and_refuse_an_index_without_its_lock(self) -> None:
        store = self.store()
        self.add(store)
        os.unlink(self.root / '.lock')
        for call in (lambda: store.list(), lambda: store.values()):
            with self.assertRaises(KeyStoreError) as caught:
                call()
            self.assertEqual(caught.exception.code, 'file_invalid')
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'file_invalid')
        self.assertFalse((self.root / '.lock').exists())


@needs_posix
class HelperDiagnosticsTest(TempStoreCase):
    """R2 (both Codex reads), M10/M07: nothing a helper printed reaches an error, an output or an exception chain."""

    def test_stderr_holding_the_key_never_reaches_a_command_output(self) -> None:
        for action in ('audit', 'remove'):
            with self.subTest(action=action):
                system, _ = keychain_system()
                store = KeyStore(self.base / action / 'keys', system=system)
                self.add(store)
                system.tools['secret-tool'] = lambda argv, data: Completed(1, b'', MARKER.encode() + b'\n')
                argv = ['audit', '--no-journal'] if action == 'audit' else ['remove', 'codex-key', '--yes']
                status, text = run_main(store, *argv)
                # An audit whose values cannot be read is incomplete (4), not refused (DeepSeek N4, round 2).
                self.assertEqual(status, 4 if action == 'audit' else 1)
                self.assertIn('keychain_failed', text)
                self.assertNotIn(MARKER, text)

    def test_a_store_error_echoing_its_input_says_nothing_of_it(self) -> None:
        system, _ = keychain_system()
        system.tools['secret-tool'] = lambda argv, data: Completed(1, b'', data or b'')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(self.store(system))
        self.assertEqual(caught.exception.detail, 'secret-tool store exited 1')
        self.assertNotIn(MARKER, str(caught.exception) + repr(caught.exception.args))

    def test_a_timeout_keeps_no_helper_output_anywhere_in_its_chain(self) -> None:
        # A helper that prints a made-up key and then never finishes; the store's own deadline ends it.
        helper = self.base / 'slow-helper.py'
        helper.write_text('import sys, time\nsys.stdout.write(' + repr(MARKER) + ')\nsys.stdout.flush()\n'
                          'time.sleep(3600)\n')
        system = System({'PATH': '/usr/bin:/bin', 'HOME': str(self.base)}, platform='linux')
        with mock.patch('recursion_core.keys.system.KEYCHAIN_TIMEOUT_S', 0.5):
            from recursion_core.keys.backends import SecretService
            with self.assertRaises(KeyStoreError) as caught:
                SecretService(system, 'x' * 32)._run([sys.executable, '-I', str(helper)])
        self.assertEqual(caught.exception.code, 'keychain_timeout')
        current, chain = caught.exception, []
        while current is not None:
            chain.append(repr(vars(current)) + repr(current.args))
            current = current.__context__ or current.__cause__
        self.assertEqual(len(chain), 1)                     # no context at all
        self.assertNotIn(MARKER, ''.join(chain))
        expired = subprocess.TimeoutExpired('x', 1, output=MARKER.encode())
        with self.assertRaises(ToolTimeout) as raw, mock.patch('subprocess.run', side_effect=expired):
            system.run(['x'], timeout=1)
        self.assertIsNone(raw.exception.__context__)

    def test_a_completed_helpers_repr_shows_sizes_only(self) -> None:
        self.assertEqual(repr(Completed(0, MARKER.encode(), b'err')), 'Completed(returncode=0, stdout=<44 bytes>, '
                                                                        'stderr=<3 bytes>)')


@needs_posix
class CommandLineTest(TempStoreCase):
    """R3 (both Codex reads); F2 (DeepSeek): a key on the command line is refused and never repeated, also under a
    root parser that repeats what it is given (as the control CLI's does)."""

    class RootParser(argparse.ArgumentParser):
        """Like the control CLI's: a usage error is raised with argparse's own message."""

        class Usage(Exception):
            pass

        def error(self, message):
            raise self.Usage(message)

    def parse(self, root_class, *argv):
        from recursion_core.control.commands import key as key_commands

        root = root_class(prog='recursion-control')
        sub = root.add_subparsers(dest='command')
        key_commands.parser(sub)
        return root.parse_args(['key', *argv])

    def test_mistakes_after_key_are_absorbed_and_the_command_refuses(self) -> None:
        tail = ['add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY']
        for extra in ([MARKER], [f'--key={MARKER}'], ['--value', MARKER], [f'--{MARKER}'], [f'--a={MARKER}'],
                      ['--stdin', MARKER]):
            for root_class in (argparse.ArgumentParser, self.RootParser):
                with self.subTest(form=extra[0][:8], root=root_class.__name__):
                    args = self.parse(root_class, *tail, *extra)
                    self.assertTrue(args.key_unrecognized)
                    args.out, args.err = io.StringIO(), io.StringIO()
                    status = cli.run(args, args.out, args.err, store=self.store())
                    self.assertEqual(status, 2)
                    self.assertNotIn(MARKER, args.out.getvalue() + args.err.getvalue())
                    self.assertFalse(self.root.exists())

    def test_usage_errors_keep_only_wording_that_repeats_nothing(self) -> None:
        with self.assertRaises(self.RootParser.Usage) as caught:
            self.parse(self.RootParser, 'add', '--provider', MARKER)
        self.assertEqual(str(caught.exception), 'the following arguments are required: ID, --env')
        with self.assertRaises(self.RootParser.Usage) as caught:
            self.parse(self.RootParser, MARKER)
        self.assertNotIn(MARKER, str(caught.exception))
        err = io.StringIO()
        with mock.patch('sys.stderr', err), self.assertRaises(SystemExit):
            self.parse(argparse.ArgumentParser, 'remove')
        self.assertIn('the following arguments are required: ID', err.getvalue())

    def test_a_key_in_the_ids_place_is_refused_before_any_prompt_names_it(self) -> None:
        terminal_used = []

        class Terminal:
            def read_secret(self, prompt):
                terminal_used.append(prompt)
                return OTHER.encode()

            def ask(self, prompt):
                terminal_used.append(prompt)
                return ''

        key_like = 'sk-proj-Ab3dEf5hIj7kLm9nOp1qRs7tUv'
        out, err = io.StringIO(), io.StringIO()
        status = entry.main(['add', key_like, '--provider', 'openai', '--env', 'CODEX_API_KEY'], out=out, err=err,
                            store=self.store(), terminal=Terminal())
        self.assertEqual(status, 1)
        self.assertIn('id_invalid', err.getvalue())
        self.assertNotIn(key_like, out.getvalue() + err.getvalue())
        self.assertEqual(terminal_used, [])
        self.assertFalse(self.root.exists())


@needs_posix
class RemovalRecoveryTest(TempStoreCase):
    """R4 (both Codex reads); K5: removal recovers what is already gone, and a pinned file can always be emptied."""

    def test_a_secret_service_item_that_vanished_is_removed_from_the_index(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        chain.items.clear()
        store.remove('codex-key')
        self.assertEqual(store.list().keys, {})

    def test_a_locked_item_is_not_taken_for_a_vanished_one(self) -> None:
        system, _ = keychain_system()
        store = self.store(system)
        self.add(store)
        # clear finds no UNLOCKED item (exit 1, silent), and lookup cannot unlock it: a refusal, metadata kept.
        def locked(argv, data):
            if argv[1] == 'clear':
                return Completed(1, b'', b'')
            return Completed(1, b'', b'secret-tool: Cannot get secret of a locked object\n')
        system.tools['secret-tool'] = locked
        with self.assertRaises(KeyStoreError) as caught:
            store.remove('codex-key')
        self.assertEqual(caught.exception.code, 'keychain_locked')
        self.assertIn('codex-key', store.list().keys)

    def test_an_item_lookup_unlocks_is_cleared_on_the_second_try(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        calls = []
        original = chain.__call__

        def first_clear_finds_nothing(argv, data):
            calls.append(argv[1])
            if argv[1] == 'clear' and calls.count('clear') == 1:
                return Completed(1, b'', b'')
            return original(argv, data)
        system.tools['secret-tool'] = first_clear_finds_nothing
        store.remove('codex-key')
        self.assertEqual(calls, ['clear', 'lookup', 'clear', 'lookup', 'search'])     # gone is verified (astra R4, round 2)
        self.assertEqual(chain.items, {})

    def test_a_removal_whose_index_write_failed_can_be_retried(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        writes = []
        original = KeyStore._write_index

        def second_write_fails(folder, index):
            writes.append(index)
            if len(writes) == 2:
                raise OSError(28, 'No space left on device')
            original(folder, index)
        with mock.patch.object(KeyStore, '_write_index', side_effect=second_write_fails), \
                self.assertRaises(OSError):
            store.remove('codex-key')
        self.assertEqual(chain.items, {})                   # the value is gone ...
        self.assertEqual(store.list().pending['codex-key'].op, 'remove')   # ... and the removal is on record
        with self.assertRaises(KeyRefused) as refused:
            store.credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(refused.exception.code, 'operation_pending')
        store.remove('codex-key')
        self.assertEqual((store.list().keys, store.list().pending), ({}, {}))

    def test_keys_pinned_to_the_file_can_be_removed_once_a_keychain_appears(self) -> None:
        self.add(self.store())
        system, _ = keychain_system()
        later = self.store(system)
        with self.assertRaises(KeyRefused):
            later.credential('codex-key', 'CODEX_API_KEY')
        later.remove('codex-key')                           # the file is always reachable
        self.assertEqual(later.list().backend, None)
        self.add(later, value=OTHER)                        # and the next save goes to the keychain
        self.assertEqual(later.list().backend, 'secret_service')
        self.assertEqual(later.credential('codex-key', 'CODEX_API_KEY'), OTHER)

    def test_two_config_roots_never_share_a_keychain_item(self) -> None:
        system, chain = keychain_system()
        first = KeyStore(self.base / 'a' / 'keys', system=system)
        second = KeyStore(self.base / 'b' / 'keys', system=system)
        self.add(first)
        self.add(second, value=OTHER)
        self.assertEqual(first.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        self.assertEqual(second.credential('codex-key', 'CODEX_API_KEY'), OTHER)
        self.assertEqual(len(chain.items), 2)


@needs_posix
class AuditCompletenessTest(TempStoreCase):
    """R5 (both Codex reads); F5 (DeepSeek): a journal asked for and not searched to the end is an incomplete audit."""

    def setUp(self) -> None:
        super().setUp()
        self.system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base),
                                  'XDG_STATE_HOME': str(self.base / 'state')})
        self.keys = self.store(self.system)
        self.add(self.keys)

    def test_no_journalctl_is_incomplete(self) -> None:
        status, text = run_main(self.keys, 'audit')
        self.assertEqual(status, 4)
        self.assertIn('journal not searched (journalctl is not installed)', text)

    def test_a_journalctl_that_fails_is_incomplete(self) -> None:
        helper = self.base / 'bin' / 'journalctl'
        helper.parent.mkdir()
        helper.write_text('#!/bin/sh\nexit 9\n')
        helper.chmod(0o700)
        real = System({'PATH': str(helper.parent), 'HOME': str(self.base)}, platform='linux')
        with mock.patch.object(self.system, 'which', side_effect=real.which), \
                mock.patch.object(self.system, 'stream', side_effect=real.stream):
            status, text = run_main(self.keys, 'audit')
        self.assertEqual(status, 4)
        self.assertIn('journal not searched to the end (journalctl exited 9)', text)

    def test_no_journal_on_request_is_complete(self) -> None:
        self.assertEqual(run_main(self.keys, 'audit', '--no-journal')[0], 0)


@needs_posix
class CapacityTest(TempStoreCase):
    """R6 (both Codex reads): no accepted add makes an existing key unreadable; checked at the actual limits."""

    def test_the_owner_file_at_its_limit_refuses_the_next_add_and_keeps_every_key(self) -> None:
        store = self.store()
        self.add(store)
        index = json.loads((self.root / 'index.json').read_text())
        meta = copy.deepcopy(index['keys']['codex-key'])
        secrets = {'codex-key': MARKER}
        value = 'madeup-' + 'x' * (4096 - 7)
        for n in range(1100):                               # written directly: thousands of fsyncs are not needed
            candidate = dict(secrets, **{f'key-{n}': value})
            raw = (json.dumps({'version': 1, 'secrets': dict(sorted(candidate.items()))}, indent=1) + '\n').encode()
            if len(raw) > OWNER_FILE_MAX:
                break
            secrets = candidate
            index['keys'][f'key-{n}'] = copy.deepcopy(meta)
        (self.root / 'secrets.json').write_bytes(
            (json.dumps({'version': 1, 'secrets': dict(sorted(secrets.items()))}, indent=1) + '\n').encode())
        (self.root / 'index.json').write_text(json.dumps(index))
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='last-key', value=value)
        self.assertEqual(caught.exception.code, 'store_full')
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        self.assertNotIn('last-key', store.list().keys)
        self.assertEqual(store.list().pending, {})
        store.remove('key-0')                               # and it can be made room in

    def test_the_index_at_its_limit_refuses_the_next_add_and_keeps_every_key(self) -> None:
        store = self.store()
        self.add(store)
        index = json.loads((self.root / 'index.json').read_text())
        meta = copy.deepcopy(index['keys']['codex-key'])
        meta.update(store='owner_file')
        n = 0
        while True:
            index['keys'][f'k{n:06d}'] = meta
            if len((json.dumps(index, indent=1) + '\n').encode()) > INDEX_MAX - 200:
                break
            n += 1
        secrets = {'codex-key': MARKER}
        (self.root / 'secrets.json').write_bytes(
            (json.dumps({'version': 1, 'secrets': secrets}, indent=1) + '\n').encode())
        (self.root / 'index.json').write_text(json.dumps(index, indent=1) + '\n')
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='a-much-longer-key-id-than-the-other-ids-in-the-index')
        self.assertEqual(caught.exception.code, 'store_full')
        self.assertEqual(store.credential('codex-key', 'CODEX_API_KEY'), MARKER)


@needs_posix
class PendingOperationTest(TempStoreCase):
    """R10 (astra); F3 (DeepSeek): every value a backend may hold stays findable and removable after a failure."""

    def test_an_ambiguous_save_is_kept_on_record_and_undone_by_the_next_write(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        original = chain.__call__

        def saves_then_times_out(argv, data):
            done = original(argv, data)
            if argv[1] == 'store' and 'second' in argv:
                raise ToolTimeout('secret-tool')
            return done
        system.tools['secret-tool'] = saves_then_times_out
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'keychain_timeout')
        self.assertEqual(len(chain.items), 2)               # the helper had saved it
        listed = store.list()
        self.assertNotIn('second', listed.keys)
        self.assertEqual(listed.pending['second'].op, 'add')
        _, text = run_main(store, 'list')
        self.assertIn('second  an unfinished add', text)
        system.tools['secret-tool'] = original
        self.add(store, key_id='third', value=OTHER + 'x')   # the next write recovers it
        self.assertEqual(store.list().pending, {})
        self.assertEqual(sorted(store.list().keys), ['codex-key', 'third'])
        self.assertEqual(len(chain.items), 2)

    def test_a_failed_rollback_leaves_the_value_on_record_and_remove_clears_it(self) -> None:
        system, chain = keychain_system()
        store = self.store(system)
        self.add(store)
        writes, original_write = [], KeyStore._write_index
        original_tool = chain.__call__

        def third_write_fails(folder, index):
            writes.append(index)
            if len(writes) == 2:
                raise OSError(28, 'No space left on device')
            original_write(folder, index)

        def clear_fails(argv, data):
            if argv[1] == 'clear':
                return Completed(1, b'', b'secret-tool: something went wrong\n')
            return original_tool(argv, data)

        with mock.patch.object(KeyStore, '_write_index', side_effect=third_write_fails):
            system.tools['secret-tool'] = lambda argv, data: (clear_fails(argv, data))
            with self.assertRaises(OSError):
                self.add(store, key_id='second', value=OTHER)
        system.tools['secret-tool'] = original_tool
        self.assertEqual(store.list().pending['second'].op, 'add')
        found, _ = store.values()
        self.assertEqual(found['second'], OTHER)            # the audit looks for it too
        self.assertIsNone(store.remove('second'))           # an orphan, cleared on request
        self.assertEqual(store.list().pending, {})
        self.assertEqual(len(chain.items), 1)

    def test_a_save_killed_between_the_value_and_the_index_is_on_record(self) -> None:
        script = (
            'import os, sys\n'
            'from recursion_core.keys.store import KeyStore\n'
            'from recursion_core.keys.tests.support import file_system\n'
            'store = KeyStore(sys.argv[1], system=file_system())\n'
            'store.add("a-key", "openai", "CODEX_API_KEY", sys.argv[2], metered_spend_allowed=True, cap_usd=1)\n'
            'original = KeyStore._write_index\n'
            'calls = []\n'
            'def killer(folder, index):\n'
            '    calls.append(1)\n'
            '    if len(calls) == 2:\n'
            '        os._exit(137)\n'
            '    original(folder, index)\n'
            'KeyStore._write_index = staticmethod(killer)\n'
            'store.add("orphan", "openai", "CODEX_API_KEY", sys.argv[3], metered_spend_allowed=True, cap_usd=1)\n')
        core = str(Path(__file__).resolve().parents[3])
        done = subprocess.run([sys.executable, '-B', '-c', script, str(self.root), OTHER, MARKER],
                              env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': core, 'HOME': str(self.base)},
                              capture_output=True, check=False)
        self.assertEqual(done.returncode, 137, done.stderr)
        store = self.store()
        self.assertEqual(store.list().pending['orphan'].op, 'add')
        status, text = run_main(store, 'audit', '--no-journal')
        self.assertEqual(status, 4)
        self.assertIn('unfinished add of orphan', text)
        self.assertNotIn(MARKER, text)
        self.add(store, key_id='next', value=OTHER + 'y')
        self.assertNotIn('orphan', json.loads((self.root / 'secrets.json').read_text())['secrets'])


@needs_posix
class TerminalModeTest(TempStoreCase):
    """R7 (sol) and F4 (DeepSeek): the key is read with echo AND canonical mode off; the terminal is put back without
    discarding what was typed ahead."""

    def test_the_modes_set_and_restored(self) -> None:
        import socket
        import termios

        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(MARKER.encode() + b'\ny\n')
        old = [0, 0, 0, termios.ECHO | termios.ICANON | termios.ISIG, 0, 0, [b'\x00'] * 32]
        calls = []
        with mock.patch('termios.tcgetattr', return_value=old), \
                mock.patch('termios.tcsetattr', side_effect=lambda fd, when, attrs: calls.append((when, attrs))):
            terminal = cli.Terminal(ours.fileno())
            self.assertEqual(terminal.read_secret('prompt: '), MARKER.encode())
            self.assertEqual(terminal.ask('[y/N] '), 'y')       # the answer typed ahead is kept
        (enter, quiet), (leave, restored) = calls
        self.assertEqual((enter, leave), (termios.TCSAFLUSH, termios.TCSADRAIN))
        self.assertEqual(quiet[3] & (termios.ECHO | termios.ICANON), 0)
        self.assertTrue(quiet[3] & termios.ISIG)                # Ctrl-C still interrupts
        self.assertEqual((quiet[6][termios.VMIN], quiet[6][termios.VTIME]), (1, 0))
        self.assertIs(restored, old)


@needs_posix
class CoverageTest(TempStoreCase):
    """R7 (astra) and R9 (sol): a regression for each check that could be removed with the suite still green."""

    def write_secrets(self, text: str) -> None:
        (self.root / 'secrets.json').write_text(text)

    def assert_secrets_refused(self, code='owner_file_invalid') -> None:
        with self.assertRaises(KeyRefused) as caught:
            self.store().credential('codex-key', 'CODEX_API_KEY')
        self.assertEqual(caught.exception.code, code)

    def test_the_owner_file_must_be_exactly_what_the_store_writes(self) -> None:
        self.add(self.store())
        for text in ('{"version":1,"secrets":{"codex-key":' + json.dumps(MARKER) + ',"codex-key":' + json.dumps(OTHER) + '}}',
                     json.dumps({'version': 2, 'secrets': {'codex-key': MARKER}}),
                     json.dumps({'version': True, 'secrets': {'codex-key': MARKER}}),
                     json.dumps({'version': 1, 'secrets': {'codex-key': MARKER, 'bad/id': OTHER}})):
            with self.subTest(text=text[:40]):
                self.write_secrets(text)
                self.assert_secrets_refused()

    def test_a_file_larger_than_its_limit_is_refused_at_the_read(self) -> None:
        self.add(self.store())
        (self.root / 'secrets.json').write_bytes(b'x' * (OWNER_FILE_MAX + 1))
        self.assert_secrets_refused('file_invalid')

    def test_secret_tool_output_must_be_a_key_of_the_accepted_length(self) -> None:
        system, _ = keychain_system()
        store = self.store(system)
        self.add(store)
        for data in (b'x' * 7, b'x' * 4097, b'two words here'):
            with self.subTest(length=len(data)):
                system.tools['secret-tool'] = lambda argv, raw, data=data: Completed(0, data, b'')
                with self.assertRaises(KeyRefused) as caught:
                    store.credential('codex-key', 'CODEX_API_KEY')
                self.assertEqual(caught.exception.code, 'value_invalid')

    def test_a_hard_linked_lock_is_refused(self) -> None:
        store = self.store()
        self.add(store)
        os.link(self.root / '.lock', self.base / 'lock-alias')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(store, key_id='second', value=OTHER)
        self.assertEqual(caught.exception.code, 'file_invalid')

    def test_the_package_exports_the_credential_source(self) -> None:
        import inspect

        from recursion_core.keys import credentials
        from recursion_core.keys.store import credentials as source
        self.assertIs(credentials, source)
        self.assertEqual(list(inspect.signature(credentials).parameters), ['connection_id', 'env_name'])

    def test_kwallet_only_is_refused_before_anything_is_made(self) -> None:
        system = FakeSystem(tools={'busctl': busctl(kwallet=True)}, bus='unix:path=/b')
        with self.assertRaises(KeyStoreError) as caught:
            self.add(self.store(system))
        self.assertEqual(caught.exception.code, 'keychain_unsupported')
        self.assertFalse(self.base.joinpath('config').exists())
