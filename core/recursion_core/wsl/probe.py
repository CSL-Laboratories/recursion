"""One frozen reading of whether this process is on WSL, and whether Windows interop can be seen.

``read`` is the containment fact a later Stop claim reads. It does not choose a
downgrade and it does not refuse a launch: those are open (PRODUCT O75). It
records the kernel name, pid 1, the live interop sources and ``wsl.conf``.

``interop`` is ``on`` when any live source shows interop, ``unknown`` when a
source cannot be read and none shows interop, and ``off`` only when every live
source was read and none shows interop. ``unknown`` is never ``off``. A
``wsl.conf`` value does not override that.

The result is cached for the process, keyed by the root and by ``WSL_INTEROP``.
A second caller with the same key receives the same record. An exception or an
interrupt before the record is complete stores nothing.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import threading

_LOCK = threading.Lock()
_CACHE: dict[tuple, 'Record'] = {}


class _Mark:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


_MISSING = _Mark('missing')
_UNREADABLE = _Mark('unreadable')


@dataclass(frozen=True)
class Record:
    """The WSL facts one process read.

    kind: ``none``, ``wsl1`` or ``wsl2``.
    systemd_pid1: ``True`` or ``False`` when ``/proc/1/comm`` was read, ``None``
    when it was not. ``None`` is not ``False``.
    interop: ``on``, ``off`` or ``unknown``.
    wslconf_interop, wslconf_systemd: ``true``, ``false``, ``absent`` or ``unreadable``.
    automount_root: the ``[automount] root`` path, or ``absent``, or ``unreadable``.
    ``absent`` and ``unreadable`` are not paths. An unreadable config is not ``false``
    and not ``absent``.
    """

    kind: str
    systemd_pid1: bool | None
    interop: str
    wslconf_interop: str
    wslconf_systemd: str
    automount_root: str


def read(root: Path = Path('/'), environ: Mapping[str, str] = os.environ) -> Record:
    """Read the WSL record under ``root`` (default ``/``) and ``environ`` (default the process environment)."""
    if environ is None:
        environ = os.environ
    root = Path(root)
    key = (_root_key(root), _env_key(environ))
    with _LOCK:
        cached = _CACHE.get(key)
    if cached is not None:
        return cached
    record = _probe(root, environ)
    with _LOCK:
        return _CACHE.setdefault(key, record)


def _root_key(root: Path) -> str:
    return os.path.abspath(os.path.normpath(str(root)))


def _env_key(environ: Mapping[str, str]) -> tuple:
    # Membership is checked again while probing, so a mapping that fails on a
    # later check cannot leave a stored record for this key.
    if 'WSL_INTEROP' not in environ:
        return (False, None)
    return (True, environ.get('WSL_INTEROP'))


def _probe(root: Path, environ: Mapping[str, str]) -> Record:
    kind = _kind(root)
    systemd_pid1 = _systemd_pid1(root)
    interop = _interop(root, environ)
    wslconf_interop, wslconf_systemd, automount_root = _wslconf(root)
    return Record(kind, systemd_pid1, interop, wslconf_interop, wslconf_systemd, automount_root)


def _read_text(path: Path):
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return _MISSING
    except (OSError, UnicodeError):
        return _UNREADABLE
    return text.lstrip('\ufeff')


def _kind(root: Path) -> str:
    """``none`` only when both identity files were readable and neither names microsoft.

    An unreadable identity file is not a native kernel. The kind set has no
    ``unknown``, so that case is ``wsl1`` unless a readable sibling names the
    WSL2 kernel (``wsl2`` or ``microsoft-standard``). A missing file is not
    unreadable: it simply does not name microsoft.
    """
    texts = []
    blocked = False
    for relative in ('proc/sys/kernel/osrelease', 'proc/version'):
        got = _read_text(root / relative)
        if got is _UNREADABLE:
            blocked = True
        elif got is not _MISSING:
            texts.append(got.lower())
    blob = '\n'.join(texts)
    if 'wsl2' in blob or 'microsoft-standard' in blob:
        return 'wsl2'
    if 'microsoft' in blob:
        return 'wsl1'
    if blocked:
        return 'wsl1'
    return 'none'


def _systemd_pid1(root: Path) -> bool | None:
    got = _read_text(root / 'proc' / '1' / 'comm')
    if got is _MISSING or got is _UNREADABLE:
        return None
    return got.replace('\x00', '').strip() == 'systemd'


def _interop(root: Path, environ: Mapping[str, str]) -> str:
    states = []
    binfmt = root / 'proc' / 'sys' / 'fs' / 'binfmt_misc'
    for name in ('WSLInterop', 'WSLInterop-late'):
        got = _read_text(binfmt / name)
        if got is _UNREADABLE:
            states.append('unknown')
        elif got is not _MISSING and _enabled(got):
            states.append('on')
        else:
            states.append('off')
    run = root / 'run' / 'WSL'
    try:
        run.lstat()
    except FileNotFoundError:
        states.append('off')
    except OSError:
        states.append('unknown')
    else:
        states.append('on')
    if 'WSL_INTEROP' in environ:
        states.append('on')
    else:
        states.append('off')
    if 'on' in states:
        return 'on'
    if 'unknown' in states:
        return 'unknown'
    return 'off'


def _enabled(text: str) -> bool:
    return any(line.strip() == 'enabled' for line in text.splitlines())


def _wslconf(root: Path) -> tuple[str, str, str]:
    got = _read_text(root / 'etc' / 'wsl.conf')
    if got is _MISSING:
        return ('absent', 'absent', 'absent')
    if got is _UNREADABLE:
        return ('unreadable', 'unreadable', 'unreadable')
    return _parse_conf(got)


def _parse_conf(text: str) -> tuple[str, str, str]:
    interop: list[str] = []
    systemd: list[str] = []
    roots: list[str] = []
    section = ''
    for raw in text.splitlines():
        line = _strip_inline_comment(raw).strip()
        if not line or line[0] in '#;':
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip().lower()
            continue
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().lower()
        value = _unquote(value.strip())
        if section == 'interop' and key == 'enabled':
            interop.append(value)
        elif section == 'boot' and key == 'systemd':
            systemd.append(value)
        elif section == 'automount' and key == 'root':
            roots.append(value)
    return (_collapse_bool(interop), _collapse_bool(systemd), _collapse_root(roots))


def _strip_inline_comment(line: str) -> str:
    out = []
    quote = ''
    for index, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = ''
            continue
        if ch in '"\'':
            quote = ch
            out.append(ch)
            continue
        if ch == '#' or (ch == ';' and index > 0 and line[index - 1].isspace()):
            break
        out.append(ch)
    return ''.join(out)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
        return value[1:-1]
    return value


def _collapse_bool(values: list[str]) -> str:
    if not values:
        return 'absent'
    parsed = []
    for value in values:
        token = value.strip().lower()
        if token not in ('true', 'false'):
            return 'unreadable'
        parsed.append(token)
    if len(set(parsed)) != 1:
        return 'unreadable'
    return parsed[0]


def _collapse_root(values: list[str]) -> str:
    if not values:
        return 'absent'
    paths = []
    for value in values:
        if not value.startswith('/') or '\x00' in value:
            return 'unreadable'
        paths.append(value)
    if len(set(paths)) != 1:
        return 'unreadable'
    return paths[0]
