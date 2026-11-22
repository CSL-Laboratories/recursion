"""The key commands, shared by ``recursion-control key ...`` (seam S2) and ``python -m recursion_core.keys``.

``add <id> --provider <vendor> --env <VARIABLE>``
    Reads the key from the terminal with echo and canonical mode off (so a long paste is measured whole, not
    cut at the kernel's 4095-byte line), never from the command line. ``--stdin`` reads it from standard input
    instead, and only when standard input is not a terminal (where it would be echoed). With no terminal and no
    ``--stdin`` it refuses (``no_terminal``) and saves nothing. On a terminal it then asks whether runs on this
    key may spend money billed to it, and the cap in US dollars (answers typed ahead are kept);
    ``--allow-metered-spend --cap-usd N`` answers both up front, and is the only way to opt in with ``--stdin``.
    With no opt-in the cap is 0, and every run on the key is refused before launch until it is removed and
    added again with one. An id that looks like a key is refused before any prompt shows it.
``list [--json]``
    Ids, providers, variable names, when each was saved, the store, the spend opt-in and any unfinished
    operation. Never a value.
``remove <id> [--yes]``
    Asks on the terminal before it deletes (``--yes`` skips the question; with no terminal it is required),
    then says what it removed and from where. It also clears what an interrupted save left behind, and a value of
    an id no saved key has that the store's own owner-only file still holds, whatever the host offers now (not a
    file the store does not use: another store's while the store has keys in the file, or any while its keys are
    in a keychain; with no saved key at all, the file in its folder is its own).
``audit [--path P ...] [--no-journal]``
    Searches the state folder, the config folder and the journal for every saved value (``audit.py``) and
    names each place a value was found, never the value, and each temp file an interrupted save left in the
    store's folder. Exit 1 when one was found, 4 when the search was incomplete (a value or a file could not be
    read, an operation is unfinished, the journal was asked for and not searched to the end, or the Secret Service
    holds items Recursion saved that no saved key names) and nothing was found, and 3, not 1, when it was refused,
    so that 1 always means a finding.

Exit statuses: 0 done, 1 refused (``refused: <code>: <detail>`` on standard error; 3 for ``audit``), 2 a usage
error, which includes a command line holding words the command does not take (``value_from_argv``; they are not
repeated), 130 interrupted (Ctrl-C; the terminal is restored, and an unfinished save stays on record). Every
argument's destination starts with ``key_``, so none can meet a name of the control CLI's own.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from recursion_core.keys import audit as audit_mod
from recursion_core.keys import validate as v
from recursion_core.keys.errors import KeyStoreError
from recursion_core.keys.store import STORE_TEXT, KeyStore

LINE_MAX = 8192
# argparse's own words, kept only where they repeat nothing that was typed.
_SAFE_USAGE = re.compile(r'the following arguments are required: [A-Za-z0-9_, /-]+'
                         r'|argument [A-Za-z0-9_/-]+: expected one argument')


def _safe_usage(message: str) -> str:
    if isinstance(message, str) and _SAFE_USAGE.fullmatch(message):
        return message
    return 'the command line could not be read (what was typed is not repeated here)'


def _absorb_extras(parser: argparse.ArgumentParser) -> None:
    """Make ``parser`` keep nothing it cannot use: words it does not know mark the namespace instead of travelling
    up to the root parser, whose error would print them (a key typed on the line by mistake among them). Its own
    usage errors (an unknown action word, say) keep argparse's wording only where it repeats nothing typed.

    Set on the instance: the ``key`` parser itself is made by whoever composes the CLI, of their class."""
    original_parse, original_error = parser.parse_known_args, parser.error

    def parse_known_args(args=None, namespace=None):
        namespace, extras = original_parse(args, namespace)
        if extras:
            namespace.key_unrecognized = True
        return namespace, []

    def error(message):
        return original_error(_safe_usage(message))

    parser.parse_known_args = parse_known_args
    parser.error = error


def configure(parser: argparse.ArgumentParser) -> None:
    """Give ``parser`` the four key actions. Used by both entries, so they parse alike.

    The parser may be the control CLI's (seam S2), whose usage errors repeat what was typed. So everything after
    ``key`` is parsed by parsers of this module: an unknown word or option is noted (``key_unrecognized``) and the
    command refuses without repeating it, and a usage error keeps argparse's wording only where that wording repeats
    nothing typed. Abbreviated options are off, so no mistyped option is taken for another.
    """
    _absorb_extras(parser)
    actions = parser.add_subparsers(dest='key_action', metavar='ACTION')
    actions.required = True
    add = actions.add_parser('add', allow_abbrev=False, help='save a key, read from the terminal with no echo',
                             description='Save a user-entered API key. The key is read from the terminal with no '
                                         'echo (or from standard input with --stdin), never from the command line.')
    add.add_argument('key_id', metavar='ID', help="the route's connection id, for example codex-key")
    add.add_argument('--provider', dest='key_provider', required=True, metavar='VENDOR',
                     help=f'the vendor id, lower case (published: {", ".join(v.PROVIDER_IDS)})')
    add.add_argument('--env', dest='key_env', required=True, metavar='VARIABLE',
                     help='the variable the engine reads the key from, for example CODEX_API_KEY')
    add.add_argument('--stdin', dest='key_stdin', action='store_true',
                     help='read the key from standard input (which must not be a terminal)')
    add.add_argument('--allow-metered-spend', dest='key_allow_spend', action='store_true',
                     help='runs on this key may spend money billed to it, up to --cap-usd')
    add.add_argument('--cap-usd', dest='key_cap', metavar='DOLLARS', help='the spend cap in US dollars')
    listing = actions.add_parser('list', allow_abbrev=False, help='show saved keys (never a value)')
    listing.add_argument('--json', dest='key_json', action='store_true', help='print JSON')
    remove = actions.add_parser('remove', allow_abbrev=False, help='delete a saved key')
    remove.add_argument('key_id', metavar='ID')
    remove.add_argument('--yes', dest='key_yes', action='store_true', help='do not ask first')
    audit = actions.add_parser('audit', allow_abbrev=False, help='look for saved values in state, config and logs')
    for action_parser in (add, listing, remove, audit):
        _absorb_extras(action_parser)
    audit.add_argument('--path', dest='key_paths', action='append', default=[], metavar='PATH',
                       help='also search this file or folder (repeatable)')
    audit.add_argument('--no-journal', dest='key_no_journal', action='store_true', help='do not search the journal')


class Terminal:
    """The controlling terminal, opened as ``/dev/tty``. ``read_secret`` turns echo off while it reads."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._ahead = bytearray()        # bytes read past the end of a line: answers typed ahead are kept

    def _chunk(self) -> bytes:
        if self._ahead:
            chunk, self._ahead = bytes(self._ahead), bytearray()
            return chunk
        return os.read(self.fd, 4096)

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
            chunk = self._chunk()
            if not chunk:
                break
            end = chunk.find(b'\n')
            part = chunk if end < 0 else chunk[:end]
            if end >= 0:
                self._ahead[:0] = chunk[end + 1:]
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

    def _read_secret_line(self) -> bytes | None:
        """One line read byte by byte in non-canonical mode, so its whole length is seen: the kernel's canonical
        line holds 4095 bytes and drops the rest of a longer paste, which would save a cut-down key. Backspace and
        Ctrl-U edit; Ctrl-D on an empty line is nothing. None when the line was longer than ``LINE_MAX``."""
        line, too_long = bytearray(), False
        while True:
            chunk = self._chunk()
            if not chunk:
                break                                    # the terminal hung up
            for position, byte in enumerate(chunk):
                if byte in (0x0A, 0x0D):
                    rest = chunk[position + 1:]
                    if byte == 0x0D and rest[:1] == b'\n':
                        rest = rest[1:]
                    self._ahead[:0] = rest               # what was typed after the key is kept for the questions
                    return None if too_long else bytes(line)
                if byte in (0x7F, 0x08):
                    if line:
                        line.pop()
                elif byte == 0x15:
                    line.clear()
                elif byte == 0x04 and not line:
                    return None
                elif len(line) >= LINE_MAX:
                    too_long = True
                else:
                    line.append(byte)
        return None if too_long else bytes(line)

    def read_secret(self, prompt: str) -> bytes | None:
        import termios

        old = termios.tcgetattr(self.fd)
        quiet = list(old)
        quiet[3] &= ~(termios.ECHO | termios.ICANON)
        quiet[6] = list(old[6])
        quiet[6][termios.VMIN], quiet[6][termios.VTIME] = 1, 0
        termios.tcsetattr(self.fd, termios.TCSAFLUSH, quiet)
        try:
            self._write(prompt)          # echo is already off when the prompt is shown
            return self._read_secret_line()
        finally:
            # TCSADRAIN, not TCSAFLUSH: switching echo back on must not throw away answers already typed ahead.
            termios.tcsetattr(self.fd, termios.TCSADRAIN, old)
            self._write('\n')

    def ask(self, prompt: str) -> str:
        self._write(prompt)
        line = self._read_line()
        return '' if line is None else line.decode('ascii', 'replace').strip()


def _refused(err, exc: KeyStoreError, status: int) -> int:
    print(f'refused: {exc}', file=err)
    return status


def _store_line(backend: str | None, store: KeyStore) -> str:
    if backend is None:
        return 'No key is saved; the first save pins the store this host offers then.'
    if backend == 'owner_file':
        return (f'Keys are kept in {STORE_TEXT[backend]}, {store.owner_file()} (no OS keychain answered at the first '
                'save).')
    return f'Keys are kept in {STORE_TEXT.get(backend, backend)} (pinned at the first save).'


def _spend_text(allowed: bool, cap) -> str:
    return f'allowed, cap ${cap}' if allowed else 'not allowed (runs on this key are refused)'


def run(args, out=None, err=None, *, store: KeyStore | None = None, terminal: Terminal | None = None,
        stdin=None) -> int:
    """Run one parsed key command. ``store``, ``terminal`` and ``stdin`` are for tests."""
    out = out or sys.stdout
    err = err or sys.stderr
    store = store or KeyStore()
    if getattr(args, 'key_unrecognized', False):
        print('refused: value_from_argv: the command line holds words this command does not take, and a key is never '
              'taken from the command line; nothing was done (and what was typed is not repeated here)', file=err)
        return 2
    action = getattr(args, 'key_action', None)
    refused = 3 if action == 'audit' else 1
    try:
        if action == 'add':
            return _add(args, out, err, store, terminal, stdin)
        if action == 'list':
            return _list(args, out, store)
        if action == 'remove':
            return _remove(args, out, err, store, terminal)
        if action == 'audit':
            return _audit(args, out, err, store)
    except KeyStoreError as exc:
        return _refused(err, exc, refused)
    except OSError as exc:
        # The folder or a helper failed underneath the store (a full disk, say). Its type and errno only.
        return _refused(err, KeyStoreError('store_failed', f'{type(exc).__name__} errno {exc.errno}'), refused)
    except KeyboardInterrupt:
        print('interrupted: nothing more was done (an unfinished save stays on record; "key list" shows it)', file=err)
        return 130
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
    problem = v.id_problem(args.key_id)
    if problem is not None:                  # before any prompt names the id: a key typed there is not shown
        raise KeyStoreError('id_invalid', problem)
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
    if not v.known_provider(record.provider):
        print(f'note: {record.provider} is not a published vendor; the key is kept and listed, and handed to no '
              'engine until the vendor is published', file=out)
    return 0


def _list(args, out, store) -> int:
    index = store.list()
    records = [index.keys[kid] for kid in sorted(index.keys)]
    pending = [index.pending[kid] for kid in sorted(index.pending)]
    if args.key_json:
        print(json.dumps({'backend': index.backend, 'keys': [r.as_json() for r in records],
                          'pending': [{'key_id': p.key_id, 'op': p.op, 'started_at': p.started_at} for p in pending]},
                         indent=1), file=out)
        return 0
    print(_store_line(index.backend, store), file=out)
    for r in records:
        unrouted = '' if v.known_provider(r.provider) else '  (not a published vendor: handed to no engine)'
        print(f'{r.key_id}  provider={r.provider}  env={r.env_name}  saved={r.created_at}  store={r.store}  '
              f'metered spend: {_spend_text(r.metered_spend_allowed, r.cap_usd)}{unrouted}', file=out)
    for p in pending:
        print(f'{p.key_id}  an unfinished {p.op} (started {p.started_at}): the next save or removal finishes it, '
              f'or "remove {p.key_id}" clears it', file=out)
    return 0


def _remove(args, out, err, store, terminal) -> int:
    problem = v.id_problem(args.key_id)
    if problem is not None:
        raise KeyStoreError('id_invalid', problem)
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
    if record is None:
        print(f'removed a value of {args.key_id} that no saved key named (an interrupted save, or an index lost, '
              'emptied or put back)', file=out)
    else:
        print(f'removed {record.key_id} ({record.provider}, {record.env_name}) from {STORE_TEXT[record.store]}',
              file=out)
    return 0


def _audit(args, out, err, store) -> int:
    report = audit_mod.run(store, extra=args.key_paths, journal=not args.key_no_journal)
    for place, label in report.found:
        key_id, kind = audit_mod.named(label)         # the id alone, whatever reaches the value
        if kind is None:
            print(f'FOUND: the value of {key_id} is in {place}', file=out)
        else:
            print(f'FOUND: a value of {key_id} that no saved key names is in {place}', file=out)
    for key_id, code in sorted(report.missing.items()):
        print(f'not searched for {key_id}: its value could not be read ({code})', file=out)
    for key_id, op in sorted(report.pending.items()):
        print(f'unfinished {op} of {key_id}: its value may still be in the store; "key remove {key_id}" clears it',
              file=out)
    for key_id in report.unbound:
        print(f'not named: the owner-only file holds a value of {key_id} that no saved key names; '
              f'"key remove {key_id}" deletes it', file=out)
    for key_id in report.foreign:
        print(f"not this store's: {report.owner_file or store.owner_file()} holds a value of {key_id}, and this store "
              "does not use that "
              "file (it names another store, or this store's keys are in a keychain): no command of this store "
              'changes it, and the value leaves with the file', file=out)
    if report.unnamed:
        print(f'keychain: {report.unnamed} item(s) in the Secret Service that Recursion saved and no saved key of this '
              "store names (another store's, or this store's from before its index was lost, emptied or put back); "
              'their values are not looked for. "key remove <id>" deletes every item of this store under an id a '
              'saved key has, whichever save stored it; the keychain\'s own tool finds the rest by the attribute '
              '"application recursion"', file=out)
    if report.unnamed_refused:
        print(f'keychain: its items were not counted ({report.unnamed_refused})', file=out)
    for place in report.leftovers:
        print(f'FOUND: a temp file an interrupted save left behind, which may hold part of a key: {place} (the next '
              '"key add" or "key remove" deletes it)', file=out)
    for place in report.unreadable[:20]:
        print(f'unreadable: {place}', file=out)
    if len(report.unreadable) > 20:
        print(f'unreadable: {len(report.unreadable) - 20} more', file=out)
    findings = len(report.found) + len(report.leftovers)
    print(f'audit: {report.keys} saved key(s), {report.values} value(s) looked for; {report.files} file(s) searched '
          f'under {", ".join(report.roots)}; journal {report.journal}; {findings} finding(s)', file=out)
    if findings:
        return 1
    return 0 if report.complete() else 4
