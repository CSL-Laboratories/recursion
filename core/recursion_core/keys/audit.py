"""``key audit``: look for every saved value where it must never be, and say where, never what.

It fetches each saved value from the pinned backend and searches, as bytes:

- the daemon's state folder (``$XDG_STATE_HOME/recursion``, or ``~/.local/state/recursion``): run records,
  results, logs, the breaker store;
- Recursion's config folder (``$XDG_CONFIG_HOME/recursion``), the key store's own folder included: its index
  must hold no value, and a temp file a crashed save left behind is a finding. Only the owner-only file
  itself (``secrets.json``), which holds the values by design, is skipped;
- any folder or file given with ``--path``;
- the user's journal (``journalctl --user -o cat``), where the daemon's log lines go when it runs as a user
  service, unless ``--no-journal``.

Each value is looked for as it was saved and as JSON would write it inside a string. Symlinks are not followed
and only regular files are read (a FIFO cannot block the search). A file that cannot be read is counted and
named, not skipped in silence. The report names files and key ids only: no value, no part of one, no
surrounding text.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from recursion_core.keys.store import KeyStore
from recursion_core.keys.system import ToolMissing

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
    missing: dict = field(default_factory=dict)      # key id -> refusal code: no value to search for
    journal: str = 'not searched'
    keys: int = 0


def default_roots(store: KeyStore) -> list:
    environ = store.system.environ
    state = environ.get('XDG_STATE_HOME')
    if not state or not os.path.isabs(state):
        state = str(store.system.home() / '.local' / 'state')
    return [Path(state) / 'recursion', store.root.parent]


def needles(values: dict) -> dict:
    """Byte strings to look for, each with the key id it gives away."""
    out = {}
    for key_id, value in values.items():
        out[value.encode('ascii')] = key_id
        out[json.dumps(value)[1:-1].encode('ascii')] = key_id
    return out


def run(store: KeyStore, *, extra=(), journal: bool = True) -> Report:
    values, missing = store.values()
    report = Report(missing=missing, keys=len(values) + len(missing))
    if not values:
        report.journal = 'not searched (no value to look for)' if journal else 'not searched'
        report.roots = [str(root) for root in default_roots(store)] + [str(Path(p)) for p in extra]
        return report
    found = needles(values)
    longest = max(len(n) for n in found)
    skip = _identity(store.owner_file())
    for root in [*default_roots(store), *(Path(p) for p in extra)]:
        report.roots.append(str(root))
        for path in _walk(root, report):
            if skip is not None and _identity(path) == skip:
                continue
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
        for needle, key_id in found.items():
            if key_id not in seen and needle in window:
                seen.add(key_id)
                report.found.append((place, key_id))
        tail = window[-(longest - 1):] if longest > 1 else b''


def _search_journal(store: KeyStore, found: dict, longest: int, report: Report) -> None:
    tool = store.system.which('journalctl')
    if tool is None:
        report.journal = 'not searched (journalctl is not installed)'
        return
    try:
        _scan(store.system.stream([tool, '--user', '--no-pager', '-o', 'cat']), found, longest, 'the user journal',
              report)
    except ToolMissing:
        report.journal = 'not searched (journalctl could not be started)'
        return
    report.journal = 'searched'
