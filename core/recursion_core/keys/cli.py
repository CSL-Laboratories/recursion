"""The key commands, shared by ``recursion-control key ...`` (seam S2) and ``python -m recursion_core.keys``.

``add <id> --provider <vendor> --env <VARIABLE>``
    Reads the key from the terminal with echo off, never from the command line. ``--stdin`` reads it from
    standard input instead, and only when standard input is not a terminal (where it would be echoed). With
    no terminal and no ``--stdin`` it refuses (``no_terminal``) and saves nothing. On a terminal it then asks
    whether runs on this key may spend money billed to it, and the cap in US dollars; ``--allow-metered-spend
    --cap-usd N`` answers both up front, and is the only way to opt in with ``--stdin``. With no opt-in the cap
    is 0, and every run on the key is refused before launch until it is added again with one.
``list [--json]``
    Ids, providers, variable names, when each was saved, the store and the spend opt-in. Never a value.
``remove <id> [--yes]``
    Asks on the terminal before it deletes (``--yes`` skips the question; with no terminal it is required),
    then says what it removed and from where.
``audit [--path P ...] [--no-journal]``
    Searches the state folder, the config folder and the journal for every saved value (``audit.py``) and
    names each place a value was found, never the value. Exit 1 when one was found, 4 when the search was
    incomplete (a value or a file could not be read) and nothing was found.

Exit statuses: 0 done, 1 refused (``refused: <code>: <detail>`` on standard error), 2 a usage error.
Every argument's destination starts with ``key_``, so none can meet a name of the control CLI's own.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from recursion_core.keys import audit as audit_mod
from recursion_core.keys import validate as v
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import STORE_TEXT, KeyStore

LINE_MAX = 8192


def configure(parser: argparse.ArgumentParser) -> None:
    """Give ``parser`` the four key actions. Used by both entries, so they parse alike."""
    actions = parser.add_subparsers(dest='key_action', metavar='ACTION')
    actions.required = True
    add = actions.add_parser('add', help='save a key, read from the terminal with no echo',
                             description='Save a user-entered API key. The key is read from the terminal with no '
                                         'echo (or from standard input with --stdin), never from the command line.')
    add.add_argument('key_id', metavar='ID', help="the route's connection id, for example codex-key")
    add.add_argument('--provider', dest='key_provider', required=True, help='the vendor, for example openai')
    add.add_argument('--env', dest='key_env', required=True, metavar='VARIABLE',
                     help='the variable the engine reads the key from, for example CODEX_API_KEY')
    add.add_argument('--stdin', dest='key_stdin', action='store_true',
                     help='read the key from standard input (which must not be a terminal)')
    add.add_argument('--allow-metered-spend', dest='key_allow_spend', action='store_true',
                     help='runs on this key may spend money billed to it, up to --cap-usd')
    add.add_argument('--cap-usd', dest='key_cap', metavar='DOLLARS', help='the spend cap in US dollars')
    # Anything more on the line is taken here and refused, so a key typed by mistake after the id is never
    # echoed back in a usage error.
    add.add_argument('key_extra', nargs='*', help=argparse.SUPPRESS)
    listing = actions.add_parser('list', help='show saved keys (never a value)')
    listing.add_argument('--json', dest='key_json', action='store_true', help='print JSON')
    remove = actions.add_parser('remove', help='delete a saved key')
    remove.add_argument('key_id', metavar='ID')
    remove.add_argument('--yes', dest='key_yes', action='store_true', help='do not ask first')
    audit = actions.add_parser('audit', help='look for saved values in state, config and logs')
    audit.add_argument('--path', dest='key_paths', action='append', default=[], metavar='PATH',
                       help='also search this file or folder (repeatable)')
    audit.add_argument('--no-journal', dest='key_no_journal', action='store_true', help='do not search the journal')


class Terminal:
    """The controlling terminal, opened as ``/dev/tty``. ``read_secret`` turns echo off while it reads."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    @classmethod
    def open(cls) -> Terminal | None:
        try:
            import termios  # noqa: F401 - only to learn that this platform has terminals
        except ImportError:
            return None
        try:
            fd = os.open('/dev/tty', os.O_RDWR | getattr(os, 'O_NOCTTY', 0) | getattr(os, 'O_CLOEXEC', 0))
        except OSError:
            return None
        if not os.isatty(fd):
            os.close(fd)
            return None
        return cls(fd)

    def close(self) -> None:
        if self.fd >= 0:
            fd, self.fd = self.fd, -1
            os.close(fd)

    def __repr__(self) -> str:
        return 'Terminal()'

    def _write(self, text: str) -> None:
        data = text.encode('ascii', 'replace')
        while data:
            data = data[os.write(self.fd, data):]

    def _read_line(self) -> bytes | None:
        """One line from the terminal without its newline; None when it was longer than ``LINE_MAX``."""
        chunks, total, too_long = [], 0, False
        while True:
            chunk = os.read(self.fd, 4096)
            if not chunk:
                break
            end = chunk.find(b'\n')
            part = chunk if end < 0 else chunk[:end]
            total += len(part)
            if total > LINE_MAX:
                too_long = True
            elif not too_long:
                chunks.append(part)
            if end >= 0:
                break
        if too_long:
            return None
        return b''.join(chunks).rstrip(b'\r')

    def read_secret(self, prompt: str) -> bytes | None:
        import termios

        old = termios.tcgetattr(self.fd)
        quiet = list(old)
        quiet[3] &= ~termios.ECHO
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, quiet)
        try:
            self._write(prompt)          # echo is already off when the prompt is shown
            return self._read_line()
        finally:
            termios.tcsetattr(self.fd, termios.TCSAFLUSH, old)
            self._write('\n')

    def ask(self, prompt: str) -> str:
        self._write(prompt)
        line = self._read_line()
        return '' if line is None else line.decode('ascii', 'replace').strip()


def _refused(err, exc: KeyStoreError) -> int:
    print(f'refused: {exc}', file=err)
    return 1


def _store_line(backend: str | None, store: KeyStore) -> str:
    if backend is None:
        return 'No key is saved; the first save pins the store this host offers then.'
    if backend == 'owner_file':
        return (f'Keys are kept in {STORE_TEXT[backend]}, {store.owner_file()} (no OS keychain answered at the first '
                'save).')
    return f'Keys are kept in {STORE_TEXT[backend]} (pinned at the first save).'


def _spend_text(allowed: bool, cap) -> str:
    return f'allowed, cap ${cap}' if allowed else 'not allowed (runs on this key are refused)'


def run(args, out=None, err=None, *, store: KeyStore | None = None, terminal: Terminal | None = None,
        stdin=None) -> int:
    """Run one parsed key command. ``store``, ``terminal`` and ``stdin`` are for tests."""
    out = out or sys.stdout
    err = err or sys.stderr
    store = store or KeyStore()
    try:
        if args.key_action == 'add':
            return _add(args, out, err, store, terminal, stdin)
        if args.key_action == 'list':
            return _list(args, out, store)
        if args.key_action == 'remove':
            return _remove(args, out, err, store, terminal)
        if args.key_action == 'audit':
            return _audit(args, out, err, store)
    except KeyStoreError as exc:
        return _refused(err, exc)
    except OSError as exc:
        # The folder or a helper failed underneath the store (a full disk, say). Its type and errno only.
        return _refused(err, KeyStoreError('store_failed', f'{type(exc).__name__} errno {exc.errno}'))
    print('usage: key add|list|remove|audit', file=err)
    return 2


def _read_stdin(stdin) -> bytes | None:
    stream = getattr(stdin, 'buffer', stdin)
    raw = stream.read(v.VALUE_MAX + 3)
    if len(raw) > v.VALUE_MAX + 2:
        return None
    if raw.endswith(b'\r\n'):
        return raw[:-2]
    if raw.endswith(b'\n'):
        return raw[:-1]
    return raw


def _add(args, out, err, store, terminal, stdin) -> int:
    if args.key_extra:
        print('refused: value_from_argv: a key is never taken from the command line; nothing was saved '
              '(and what was typed after the id is not repeated here)', file=err)
        return 2
    allow, cap_text = args.key_allow_spend, args.key_cap
    cap = 0
    if cap_text is not None:
        cap = v.parse_cap(cap_text)
        if cap is None:
            raise KeyStoreError('cap_invalid', 'a cap is US dollars, 0 to 100000, with at most two decimals')
    if allow and not cap:
        raise KeyStoreError('cap_invalid', '--allow-metered-spend needs --cap-usd above 0')
    if cap and not allow:
        raise KeyStoreError('cap_invalid', '--cap-usd above 0 needs --allow-metered-spend')
    stdin = stdin if stdin is not None else sys.stdin
    opened = None
    try:
        if args.key_stdin:
            if stdin is None or (hasattr(stdin, 'isatty') and stdin.isatty()):
                raise KeyStoreError('stdin_is_terminal', 'standard input is a terminal, where the key would be echoed; '
                                    'leave out --stdin to be asked for it with no echo')
            raw = _read_stdin(stdin)
        else:
            if terminal is None:
                terminal = opened = Terminal.open()
            if terminal is None:
                raise KeyStoreError('no_terminal', 'there is no terminal to read the key from with no echo; '
                                    'pass --stdin to read it from standard input')
            raw = terminal.read_secret(f'API key for {args.key_id} (input is hidden): ')
        # A key has no space in it, so spaces or tabs around a pasted key are dropped, nothing else.
        value = v.decode_value(raw.strip(b' \t')) if raw is not None else None
        raw = None
        if value is None:
            raise KeyStoreError('value_invalid', f'a key is {v.VALUE_MIN} to {v.VALUE_MAX} visible ASCII characters, '
                                'with no space; nothing was saved')
        if not args.key_stdin and cap_text is None and not allow:
            answer = terminal.ask('Allow runs on this key to spend money billed to it? [y/N] ')
            if answer.lower() in ('y', 'yes'):
                cap = v.parse_cap(terminal.ask('Spend cap in US dollars: '))
                if not cap:
                    raise KeyStoreError('cap_invalid', 'a cap is US dollars above 0, at most 100000, with at most two '
                                        'decimals; nothing was saved')
                allow = True
        record = store.add(args.key_id, args.key_provider, args.key_env, value, metered_spend_allowed=bool(allow),
                           cap_usd=cap if allow else 0)
        value = None
    finally:
        if opened is not None:
            opened.close()
    print(f'saved {record.key_id} ({record.provider}, {record.env_name}) in {STORE_TEXT[record.store]}; '
          f'metered spend: {_spend_text(record.metered_spend_allowed, record.cap_usd)}', file=out)
    return 0


def _list(args, out, store) -> int:
    index = store.list()
    records = [index.keys[kid] for kid in sorted(index.keys)]
    if args.key_json:
        print(json.dumps({'backend': index.backend, 'keys': [r.as_json() for r in records]}, indent=1), file=out)
        return 0
    print(_store_line(index.backend, store), file=out)
    for r in records:
        print(f'{r.key_id}  provider={r.provider}  env={r.env_name}  saved={r.created_at}  store={r.store}  '
              f'metered spend: {_spend_text(r.metered_spend_allowed, r.cap_usd)}', file=out)
    return 0


def _remove(args, out, err, store, terminal) -> int:
    if not v.valid_key_id(args.key_id):
        raise KeyStoreError('id_invalid', 'not a key id')
    if not args.key_yes:
        opened = None
        if terminal is None:
            terminal = opened = Terminal.open()
        if terminal is None:
            raise KeyStoreError('no_terminal', 'there is no terminal to confirm on; pass --yes to remove without asking')
        try:
            answer = terminal.ask(f'Remove the key {args.key_id}? [y/N] ')
        finally:
            if opened is not None:
                opened.close()
        if answer.lower() not in ('y', 'yes'):
            raise KeyStoreError('not_confirmed', f'{args.key_id} was not removed')
    record = store.remove(args.key_id)
    print(f'removed {record.key_id} ({record.provider}, {record.env_name}) from {STORE_TEXT[record.store]}', file=out)
    return 0


def _audit(args, out, err, store) -> int:
    report = audit_mod.run(store, extra=args.key_paths, journal=not args.key_no_journal)
    for place, key_id in report.found:
        print(f'FOUND: the value of {key_id} is in {place}', file=out)
    for key_id, code in sorted(report.missing.items()):
        print(f'not searched for {key_id}: its value could not be read ({code})', file=out)
    for place in report.unreadable[:20]:
        print(f'unreadable: {place}', file=out)
    if len(report.unreadable) > 20:
        print(f'unreadable: {len(report.unreadable) - 20} more', file=out)
    print(f'audit: {report.keys} saved key(s); {report.files} file(s) searched under {", ".join(report.roots)}; '
          f'journal {report.journal}; {len(report.found)} finding(s)', file=out)
    if report.found:
        return 1
    return 4 if report.missing or report.unreadable else 0
