"""The key commands: no key from the command line, none echoed, none printed; a real terminal through a pty."""
from __future__ import annotations

import argparse
import io
import json
import os
import select
import subprocess
import sys
from pathlib import Path
from unittest import mock

import recursion_core
from recursion_core.control.commands import key as key_commands
from recursion_core.keys import __main__ as entry
from recursion_core.keys import cli
from recursion_core.keys.system import Completed
from recursion_core.keys.tests.support import (
    LINUX,
    MARKER,
    OTHER,
    FakeSystem,
    TempStoreCase,
    keychain_system,
    needs_posix,
)

CORE = str(Path(recursion_core.__file__).resolve().parent.parent)


class FakeTerminal:
    def __init__(self, secret: bytes | None, answers=()) -> None:
        self.secret, self.answers, self.prompts = secret, list(answers), []

    def read_secret(self, prompt):
        self.prompts.append(prompt)
        return self.secret

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)


class TtyStdin(io.BytesIO):
    def isatty(self):
        return True


@needs_posix
class CommandTest(TempStoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.state = self.base / 'state'
        self.system = FakeSystem({'PATH': '/fake/bin', 'HOME': str(self.base), 'XDG_STATE_HOME': str(self.state)})

    def run_main(self, *argv, **kw):
        out, err = io.StringIO(), io.StringIO()
        kw.setdefault('store', self.store(self.system))
        status = entry.main(list(argv), out=out, err=err, **kw)
        return status, out.getvalue(), err.getvalue()

    def test_add_from_the_terminal_asks_for_the_opt_in_and_prints_no_value(self) -> None:
        terminal = FakeTerminal(MARKER.encode() + b' ', answers=['y', '7.50'])
        status, out, err = self.run_main('add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY',
                                         terminal=terminal)
        self.assertEqual((status, err), (0, ''))
        self.assertIn('saved codex-key (openai, CODEX_API_KEY)', out)
        self.assertIn('allowed, cap $7.5', out)
        self.assertNotIn(MARKER, out)
        self.assertEqual(terminal.prompts[0], 'API key for codex-key (input is hidden): ')
        self.assertEqual(self.store(self.system).credential('codex-key', 'CODEX_API_KEY'), MARKER)

    def test_declining_the_opt_in_saves_a_key_every_run_refuses(self) -> None:
        status, out, _ = self.run_main('add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY',
                                       terminal=FakeTerminal(MARKER.encode(), answers=['']))
        self.assertEqual(status, 0)
        self.assertIn('not allowed (runs on this key are refused)', out)
        record = self.store(self.system).list().keys['codex-key']
        self.assertEqual((record.metered_spend_allowed, record.cap_usd), (False, 0))

    def test_add_with_stdin_needs_the_flag_and_a_stdin_that_is_not_a_terminal(self) -> None:
        status, out, err = self.run_main('add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY',
                                         '--stdin', '--allow-metered-spend', '--cap-usd', '3',
                                         stdin=io.BytesIO(MARKER.encode() + b'\r\n'))
        self.assertEqual(status, 0, err)
        self.assertNotIn(MARKER, out + err)
        self.assertEqual(self.store(self.system).credential('codex-key', 'CODEX_API_KEY'), MARKER)
        status, _, err = self.run_main('add', 'second', '--provider', 'openai', '--env', 'CODEX_API_KEY', '--stdin',
                                         stdin=TtyStdin(OTHER.encode()))
        self.assertEqual(status, 1)
        self.assertIn('refused: stdin_is_terminal', err)
        self.assertNotIn('second', self.store(self.system).list().keys)

    def test_add_with_no_terminal_and_no_stdin_flag_refuses_and_saves_nothing(self) -> None:
        with mock.patch.object(cli.Terminal, 'open', return_value=None):
            status, _, err = self.run_main('add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY',
                                           stdin=io.BytesIO(MARKER.encode()))
        self.assertEqual(status, 1)
        self.assertIn('refused: no_terminal', err)
        self.assertFalse(self.root.exists())

    def test_a_key_on_the_command_line_is_refused_and_not_repeated(self) -> None:
        for argv in (['add', 'codex-key', MARKER, '--provider', 'openai', '--env', 'CODEX_API_KEY'],
                     ['add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY', f'--key={MARKER}'],
                     ['add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY', '--value', MARKER],
                     [MARKER], ['add', '--provider', MARKER]):
            with self.subTest(argv=argv[:3]):
                status, out, err = self.run_main(*argv, terminal=FakeTerminal(OTHER.encode(), answers=['']))
                self.assertEqual(status, 2)
                self.assertNotIn(MARKER, out + err)
                self.assertFalse(self.root.exists())

    def test_bad_values_and_caps_are_refused(self) -> None:
        for terminal, flags in ((FakeTerminal(b'short'), []), (FakeTerminal(None), []),
                                (FakeTerminal(b'two words here'), []),
                                (FakeTerminal(MARKER.encode(), answers=['y', '0']), []),
                                (FakeTerminal(MARKER.encode(), answers=['y', 'lots']), []),
                                (FakeTerminal(MARKER.encode()), ['--cap-usd', '5']),
                                (FakeTerminal(MARKER.encode()), ['--allow-metered-spend']),
                                (FakeTerminal(MARKER.encode()), ['--allow-metered-spend', '--cap-usd', '1.234'])):
            with self.subTest(flags=flags, answers=terminal.answers):
                status, out, err = self.run_main('add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY',
                                                 *flags, terminal=terminal)
                self.assertEqual(status, 1)
                self.assertTrue(err.startswith('refused: '))
                self.assertNotIn(MARKER, out + err)
                self.assertNotIn('codex-key', self.store(self.system).list().keys)

    def test_list_shows_the_store_and_no_value(self) -> None:
        store = self.store(self.system)
        self.add(store)
        self.add(store, key_id='claude-key', value=OTHER, env='ANTHROPIC_API_KEY', provider='anthropic', cap=0)
        status, out, _ = self.run_main('list')
        self.assertEqual(status, 0)
        self.assertIn('an owner-only file', out)
        self.assertIn('no OS keychain answered at the first save', out)
        self.assertIn('codex-key  provider=openai  env=CODEX_API_KEY', out)
        self.assertNotIn(MARKER, out)
        self.assertNotIn(OTHER, out)
        status, out, _ = self.run_main('list', '--json')
        doc = json.loads(out)
        self.assertEqual(doc['backend'], 'owner_file')
        self.assertEqual([k['key_id'] for k in doc['keys']], ['claude-key', 'codex-key'])
        self.assertEqual(set(doc['keys'][0]), {'key_id', 'provider', 'env_name', 'created_at', 'store',
                                               'metered_spend_allowed', 'cap_usd'})
        system, _ = keychain_system()
        other = type(store)(self.base / 'kc' / 'keys', system=system)
        self.add(other)
        status, out, _ = self.run_main('list', store=other)
        self.assertIn('the Secret Service (the desktop keychain) (pinned at the first save)', out)

    def test_remove_asks_first_unless_told_not_to(self) -> None:
        store = self.store(self.system)
        self.add(store)
        status, out, err = self.run_main('remove', 'codex-key', terminal=FakeTerminal(None, answers=['n']))
        self.assertEqual(status, 1)
        self.assertIn('refused: not_confirmed', err)
        with mock.patch.object(cli.Terminal, 'open', return_value=None):
            status, out, err = self.run_main('remove', 'codex-key')
        self.assertIn('refused: no_terminal', err)
        self.assertIn('codex-key', self.store(self.system).list().keys)
        status, out, err = self.run_main('remove', 'codex-key', terminal=FakeTerminal(None, answers=['yes']))
        self.assertEqual(status, 0)
        self.assertIn('removed codex-key (openai, CODEX_API_KEY) from an owner-only file', out)
        status, out, err = self.run_main('remove', 'codex-key', '--yes')
        self.assertEqual(status, 1)
        self.assertIn('refused: key_unknown', err)

    def test_audit_names_the_place_and_the_id_never_the_value(self) -> None:
        store = self.store(self.system)
        self.add(store)
        status, out, _ = self.run_main('audit', '--no-journal')
        self.assertEqual(status, 0, out)
        self.assertIn('0 finding(s)', out)
        leak = self.state / 'recursion' / 'runs' / 'r1' / 'result.json'
        leak.parent.mkdir(parents=True)
        leak.write_text('{"detail": "x' + MARKER + 'y"}')
        extra = self.base / 'elsewhere.log'
        extra.write_text('line\n' + MARKER + '\n')
        status, out, _ = self.run_main('audit', '--no-journal', '--path', str(extra))
        self.assertEqual(status, 1)
        self.assertIn(f'FOUND: the value of codex-key is in {leak}', out)
        self.assertIn(f'FOUND: the value of codex-key is in {extra}', out)
        self.assertNotIn(MARKER, out)
        self.assertNotIn(str(self.root / 'secrets.json'), out)          # the owner-only file is the store itself

    def test_audit_finds_a_temp_file_a_crashed_save_left_and_reads_the_journal(self) -> None:
        store = self.store(self.system)
        self.add(store)
        stale = self.root / '.secrets.json.0123456789abcdef.tmp'
        stale.write_bytes((self.root / 'secrets.json').read_bytes())
        self.system.tools['journalctl'] = lambda argv, input: Completed(0, b'INFO started\nkey ' + MARKER.encode(), b'')
        status, out, _ = self.run_main('audit')
        self.assertEqual(status, 1)
        self.assertIn(f'FOUND: the value of codex-key is in {stale}', out)
        self.assertIn('FOUND: the value of codex-key is in the user journal', out)
        self.assertIn('journal searched', out)
        self.assertNotIn(MARKER, out)

    def test_audit_finds_a_value_split_across_two_reads_and_one_written_as_json(self) -> None:
        from recursion_core.keys import audit

        store = self.store(self.system)
        quoted = 'madeup"quote\\back-not-a-real-key'
        self.add(store, key_id='quoted', value=quoted)
        self.add(store)
        (self.state / 'recursion').mkdir(parents=True)
        split = self.state / 'recursion' / 'split.log'
        split.write_bytes(b'x' * (audit.CHUNK - 10) + MARKER.encode())     # the value straddles the first read
        as_json = self.state / 'recursion' / 'record.json'
        as_json.write_text(json.dumps({'detail': quoted}))                   # only the escaped form is in it
        status, out, _ = self.run_main('audit', '--no-journal')
        self.assertEqual(status, 1)
        self.assertIn(f'FOUND: the value of codex-key is in {split}', out)
        self.assertIn(f'FOUND: the value of quoted is in {as_json}', out)
        self.assertNotIn(quoted, out)

    def test_audit_of_a_fifo_does_not_block(self) -> None:
        store = self.store(self.system)
        self.add(store)
        (self.state / 'recursion').mkdir(parents=True)
        os.mkfifo(self.state / 'recursion' / 'pipe')
        status, out, _ = self.run_main('audit', '--no-journal')
        self.assertEqual(status, 0, out)

    def test_the_control_cli_family_parses_like_the_standalone_entry(self) -> None:
        top = argparse.ArgumentParser(prog='recursion-control')
        sub = top.add_subparsers(dest='command')
        key_commands.parser(sub)
        self.assertEqual(key_commands.NAMES, ('key',))
        args = top.parse_args(['key', 'add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY', '--stdin'])
        self.assertEqual((args.command, args.key_action, args.key_id, args.key_provider, args.key_env, args.key_stdin),
                         ('key', 'add', 'codex-key', 'openai', 'CODEX_API_KEY', True))
        args = top.parse_args(['key', 'list'])
        args.out, args.err = io.StringIO(), io.StringIO()
        environ = {'XDG_CONFIG_HOME': str(self.base / 'cfg'), 'HOME': str(self.base), 'PATH': '',
                   'XDG_RUNTIME_DIR': str(self.base)}
        with mock.patch.dict(os.environ, environ, clear=True):
            self.assertEqual(key_commands.dispatch(args, client=None), 0)
        self.assertIn('No key is saved', args.out.getvalue())


def _subprocess_env(base: Path) -> dict:
    runtime = base / 'run'
    runtime.mkdir(mode=0o700, exist_ok=True)
    return {'PATH': '/usr/bin:/bin', 'HOME': str(base), 'XDG_CONFIG_HOME': str(base / 'config'),
            'XDG_STATE_HOME': str(base / 'state'), 'XDG_RUNTIME_DIR': str(runtime), 'PYTHONPATH': CORE,
            'LC_ALL': 'C.UTF-8'}


@needs_posix
class ProcessTest(TempStoreCase):
    """The real entry in a child process: no controlling terminal, and a pseudo-terminal."""

    ADD = ('add', 'codex-key', '--provider', 'openai', '--env', 'CODEX_API_KEY')

    def test_with_no_controlling_terminal_add_refuses(self) -> None:
        done = subprocess.run([sys.executable, '-B', '-m', 'recursion_core.keys', *self.ADD], input=MARKER.encode(),
                              capture_output=True, env=_subprocess_env(self.base), start_new_session=True, check=False)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(b'refused: no_terminal', done.stderr)
        self.assertNotIn(MARKER.encode(), done.stdout + done.stderr)
        self.assertFalse((self.base / 'config').exists())

    def pty_run(self, argv, replies):
        """Run the entry on a pty; answer each prompt in ``replies`` [(prompt, text)] once it is shown."""
        master, slave = os.openpty()
        wrapper = ('import os, sys; os.setsid(); fd = os.open(sys.argv[1], os.O_RDWR); '
                   '[os.dup2(fd, n) for n in (0, 1, 2)]; os.close(fd); '
                   "os.execv(sys.executable, [sys.executable, '-B', '-m', 'recursion_core.keys'] + sys.argv[2:])")
        process = subprocess.Popen([sys.executable, '-c', wrapper, os.ttyname(slave), *argv],
                                   env=_subprocess_env(self.base), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        # The parent keeps its end of the slave open while the child runs: with no holder at all, Linux answers
        # reads of the master with EIO before the child has even opened it. So the loop ends when the child has
        # exited and nothing is left to read, not on EIO.
        seen = b''
        pending = list(replies)
        idle = 0.0
        try:
            while True:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    chunk = os.read(master, 4096)
                    seen += chunk
                    idle = 0.0
                    if pending and pending[0][0].encode() in seen.split(b'\n')[-1]:
                        os.write(master, pending.pop(0)[1].encode())
                    continue
                if process.poll() is not None:
                    break
                # A guard against a hang only: a prompt that never comes fails here instead of waiting for ever.
                idle += 0.2
                self.assertLess(idle, 120, f'no output for 120 s; seen so far: {seen[-200:]!r}')
        finally:
            os.close(slave)
            os.close(master)
            if process.poll() is None:
                process.kill()
            status = process.wait()
        return status, seen

    def test_the_terminal_reads_the_key_with_no_echo(self) -> None:
        if not LINUX:
            self.skipTest('the pty wrapper relies on Linux acquiring a controlling terminal on open')
        status, seen = self.pty_run([*self.ADD], [('(input is hidden): ', MARKER + '\n'),
                                                ('[y/N] ', 'y\n'), ('Spend cap in US dollars: ', '5\n')])
        self.assertEqual(status, 0, seen)
        self.assertNotIn(MARKER.encode(), seen)
        self.assertIn(b'[y/N] y', seen)            # echo was back on for the questions after the key
        self.assertIn(b'saved codex-key (openai, CODEX_API_KEY)', seen)
        index = json.loads((self.base / 'config' / 'recursion' / 'keys' / 'index.json').read_text())
        self.assertEqual(index['keys']['codex-key']['cap_usd'], 5)
        secrets = json.loads((self.base / 'config' / 'recursion' / 'keys' / 'secrets.json').read_text())
        self.assertEqual(secrets['secrets'], {'codex-key': MARKER})

    def test_stdin_flag_on_a_terminal_is_refused(self) -> None:
        if not LINUX:
            self.skipTest('the pty wrapper relies on Linux acquiring a controlling terminal on open')
        status, seen = self.pty_run([*self.ADD, '--stdin'], [])
        self.assertEqual(status, 1, seen)
        self.assertIn(b'refused: stdin_is_terminal', seen)
        self.assertFalse((self.base / 'config').exists())
