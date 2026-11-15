"""``key audit``: look for every saved value where it must never be, and say where, never what.

It fetches each saved value from the pinned backend and searches, as bytes:

- the daemon's state folder (``$XDG_STATE_HOME/recursion``, or ``~/.local/state/recursion``): run records,
  results, logs, the breaker store;
- Recursion's config folder (``$XDG_CONFIG_HOME/recursion``), the key store's own folder included: its index
  must hold no value. Only the owner-only file itself (``secrets.json``), which holds the values by design, is
  skipped. A temp file an interrupted save left in the store's folder is a finding by its name alone, whatever
  it holds (it may hold part of a value; the next add or remove deletes it);
- any folder or file given with ``--path``;
- the user's journal (``journalctl --user -o cat``), where the daemon's log lines go when it runs as a user
  service, unless ``--no-journal``.

It also searches for the value of an interrupted add that the backend still holds. Each value is looked for as
it was saved and as JSON would write it inside a string. Symlinks are not followed and only regular files are
read (a FIFO cannot block the search). The report names files and key ids only: no value, no part of one, no
surrounding text. Two ids that hold one value are both named, once per place.

COMPLETE OR NOT. The audit is incomplete (``key audit`` exits 4 when it found nothing) when a saved value could
not be read (gone, or its keychain does not answer now), a file could not be read, an operation is unfinished,
or the journal was asked for and not searched to the end: no ``journalctl``, one that cannot start, or one that
exits with a failure. Only ``--no-journal`` leaves the journal out without making the audit incomplete.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from recursion_core.keys.files import TEMP_NAME
from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import ToolFailed, ToolMissing

CHUNK = 1 << 16
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)
_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)


@dataclass
class Report:
    roots: list = field(default_factory=list)
    files: int = 0
    unreadable: list = field(default_factory=list)
    found: list = field(default_factory=list)        # (place, key id)
    leftovers: list = field(default_factory=list)    # temp files an interrupted save left in the store's folder
    missing: dict = field(default_factory=dict)      # key id -> refusal code: no value to search for
    journal: str = 'not searched'
    journal_incomplete: bool = False   # asked for and not searched to the end
    pending: dict = field(default_factory=dict)      # key id -> op: an operation left unfinished
    keys: int = 0

    def complete(self) -> bool:
        return not (self.missing or self.unreadable or self.journal_incomplete or self.pending)


def default_roots(store: KeyStore) -> list:
    environ = store.system.environ
    state = environ.get('XDG_STATE_HOME')
    if not state or not os.path.isabs(state):
        state = str(store.system.home() / '.local' / 'state')
    return [Path(state) / 'recursion', store.root.parent]


def needles(values: dict) -> dict:
    """Byte strings to look for, each with every key id it gives away (two ids may hold one value)."""
    out = {}
    for key_id, value in sorted(values.items()):
        for needle in (value.encode('ascii'), json.dumps(value)[1:-1].encode('ascii')):
            out.setdefault(needle, []).append(key_id)
    return out


def leftovers(store: KeyStore) -> list:
    """The temp files an interrupted save left in the store's folder, by name (``files.TEMP_NAME``)."""
    try:
        names = os.listdir(store.root)
    except FileNotFoundError:
        return []
    return [str(store.root / name) for name in sorted(names) if TEMP_NAME.fullmatch(name)]


def run(store: KeyStore, *, extra=(), journal: bool = True) -> Report:
    values, missing = store.values()
    pending = {kid: entry.op for kid, entry in store.list().pending.items()}
    report = Report(missing=missing, pending=pending, keys=len(set(values) | set(missing)),
                    leftovers=leftovers(store))
    if not values:
        report.journal = 'not searched (no value to look for)' if journal else 'not searched (--no-journal)'
        report.roots = [str(root) for root in default_roots(store)] + [str(Path(p)) for p in extra]
        return report
    found = needles(values)
    longest = max(len(n) for n in found)
    searched = {_identity(store.owner_file())} - {None}     # each file once, though roots may hold one another
    for root in [*default_roots(store), *(Path(p) for p in extra)]:
        report.roots.append(str(root))
        for path in _walk(root, report):
            identity = _identity(path)
            if identity is not None and identity in searched:
                continue
            searched.add(identity)
            _search_file(path, found, longest, report)
    if journal:
        _search_journal(store, found, longest, report)
    return report


def _identity(path: Path):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _walk(root: Path, report: Report):
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return
    except OSError:
        report.unreadable.append(str(root))
        return
    if stat.S_ISREG(st.st_mode):
        yield root
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    for folder, dirs, files in os.walk(root, followlinks=False, onerror=lambda exc: report.unreadable.append(str(exc.filename))):
        dirs.sort()
        for name in sorted(files):
            path = Path(folder) / name
            try:
                if stat.S_ISREG(os.lstat(path).st_mode):
                    yield path
            except OSError:
                report.unreadable.append(str(path))


def _search_file(path: Path, found: dict, longest: int, report: Report) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC)
    except OSError:
        report.unreadable.append(str(path))
        return
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return
        report.files += 1
        _scan(_chunks(fd), found, longest, str(path), report)
    except OSError:
        report.unreadable.append(str(path))
    finally:
        os.close(fd)


def _chunks(fd: int):
    while True:
        chunk = os.read(fd, CHUNK)
        if not chunk:
            return
        yield chunk


def _scan(chunks, found: dict, longest: int, place: str, report: Report) -> None:
    """Search a stream of chunks, keeping ``longest - 1`` bytes between chunks so no match is split."""
    seen = set()
    tail = b''
    for chunk in chunks:
        window = tail + chunk
        for needle, key_ids in found.items():
            if needle in window:
                for key_id in key_ids:
                    if key_id not in seen:
                        seen.add(key_id)
                        report.found.append((place, key_id))
        tail = window[-(longest - 1):] if longest > 1 else b''


def _search_journal(store: KeyStore, found: dict, longest: int, report: Report) -> None:
    """Search the user journal. A journal that was asked for and could not be searched to the end makes the audit
    incomplete: no ``journalctl``, one that cannot start, or one that exits with a failure."""
    tool = store.system.which('journalctl')
    if tool is None:
        report.journal, report.journal_incomplete = 'not searched (journalctl is not installed)', True
        return
    try:
        _scan(store.system.stream([tool, '--user', '--no-pager', '-o', 'cat']), found, longest, 'the user journal',
              report)
    except ToolMissing:
        report.journal, report.journal_incomplete = 'not searched (journalctl could not be started)', True
        return
    except ToolFailed as exc:
        report.journal, report.journal_incomplete = f'not searched to the end (journalctl exited {exc.args[0]})', True
        return
    report.journal = 'searched'
