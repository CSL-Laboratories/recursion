"""Checked open and install-time creation of the three plugin-security stores (lane INSTALL).

The authority ledger, the breaker store and the revocation store are SQLite files that SQLite opens
by path. Before this module, each was opened with ``os.path.isfile`` and ``sqlite3.connect``: a
symlink was followed, another user's file or a group-writable one was read as ours, a link planted
at ``<store>-wal`` redirected the write-ahead log, and ``sqlite3.connect`` creates an empty
database when the file is gone (review R4, finding 2). Decision D4: "a revocation store that goes
missing after first install fails closed". Lucas, Nov 11: "Agreed, installer creates both" (the
ledger and the breaker store); the revocation store is created the same way.

FROZEN INTERFACE (lane INSTALL slice 1, Nov 14, 2026). Other lanes call these and nothing else here;
a change to a name, a parameter or a code below is a request to the keeper of this file:

    LEDGER_MAGIC, BREAKER_MAGIC, REVOCATION_MAGIC   the ``meta.magic`` value of each store
    KINDS                                           magic -> the prefix of its refusal codes
    check_store(path, magic) -> None                the file-system checks alone
    open_store(path, magic) -> sqlite3.Connection   the checks, then an open that never creates
    create_ledger(path, anchor_public_key) -> None  installer steps: a new store at a new path
    create_breaker_store(path) -> None
    create_revocation_store(path) -> None

Every refusal is ``Refused(code, detail)`` with ``code`` one of, for the store's prefix P (``ledger``,
``breaker_store`` or ``revocation_store``):

    P_missing     the store, or the folder it is in, does not exist
    P_untrusted   a link; not a regular file (or not a folder); another owner; group or world write;
                  the same for an existing ``-wal``, ``-shm`` or ``-journal`` beside it; a path that
                  is not absolute
    P_invalid     it cannot be opened or read as that store (not SQLite, no ``meta``, another magic)
    P_exists      create only: something is already at the path or at one of its side files

A magic that is not in ``KINDS`` is a programming error (``ValueError``), not a refusal. A store
another lane adds gets its entry in ``KINDS`` here.

What the checks rely on, and what they do not cover. The folder a store is in must be owned by this
user and not writable by group or world, so no other user can swap the file between the check and
SQLite's open. Code running as the same user can (the B17 limit in ``authority.py``). The open uses
SQLite's ``mode=rw``: a store deleted after the check is refused ``P_missing``, never created empty.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
from pathlib import Path

from .errors import Refused

__all__ = ["BREAKER_MAGIC", "KINDS", "LEDGER_MAGIC", "REVOCATION_MAGIC", "check_store",
           "create_breaker_store", "create_ledger", "create_revocation_store", "open_store"]

# The same strings as ``authority._LEDGER_MAGIC``, ``breaker._STORE_MAGIC`` and
# ``manifest._REVOCATION_MAGIC``, which write them; ``tests/test_stores.py`` holds them together.
# They are written out here so that those modules can import this one without a cycle.
LEDGER_MAGIC = "csl-authority-ledger/v0"
BREAKER_MAGIC = "csl-breaker-store/v0"
REVOCATION_MAGIC = "csl-revocation-store/v0"

KINDS = {
    LEDGER_MAGIC: "ledger",
    BREAKER_MAGIC: "breaker_store",
    REVOCATION_MAGIC: "revocation_store",
}

# SQLite's own files beside a database. A link at any of them redirects SQLite's writes, and a
# leftover one is read back into a new database, so each is checked when it exists.
_SIDE_FILES = ("-wal", "-shm", "-journal")
_UNSAFE_BITS = stat.S_IWGRP | stat.S_IWOTH


def _prefix(magic: object) -> str:
    if magic not in KINDS:
        raise ValueError(f"not a store this module knows: {magic!r}")
    return KINDS[magic]


def _absolute(path: object, prefix: str) -> str:
    try:
        text = os.fspath(path)
    except TypeError:
        raise Refused(f"{prefix}_untrusted", "the path is not a file-system path") from None
    if type(text) is not str or not os.path.isabs(text):
        raise Refused(f"{prefix}_untrusted", "the path is not absolute")
    return text


def _check_entry(path: str, prefix: str, *, folder: bool, required: bool) -> bool:
    """lstat ``path`` and refuse it unless it is ours. False when it is absent and not required."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise Refused(f"{prefix}_missing", os.path.basename(path) or path) from None
        return False
    except OSError as exc:
        raise Refused(f"{prefix}_invalid", f"{os.path.basename(path)}: {type(exc).__name__}") from None
    name = os.path.basename(path) or path
    if stat.S_ISLNK(info.st_mode):
        raise Refused(f"{prefix}_untrusted", f"{name} is a symlink")
    if folder and not stat.S_ISDIR(info.st_mode):
        raise Refused(f"{prefix}_untrusted", f"{name} is not a folder")
    if not folder and not stat.S_ISREG(info.st_mode):
        raise Refused(f"{prefix}_untrusted", f"{name} is not a regular file")
    if info.st_uid != os.geteuid():
        raise Refused(f"{prefix}_untrusted", f"{name} is owned by another user")
    if info.st_mode & _UNSAFE_BITS:
        raise Refused(f"{prefix}_untrusted", f"{name} is writable by group or world")
    return True


def check_store(path, magic: str) -> None:
    """Refuse a store that is not an owner-only regular file in an owner-only folder.

    Checks, in order: the path is absolute; its folder exists, is not a link, is ours and is not
    group- or world-writable; the store exists, is a regular file (a link is refused, not
    followed), is ours and is not group- or world-writable; and each of ``-wal``, ``-shm`` and
    ``-journal`` that exists beside it passes the same file checks. Nothing is opened.
    """
    prefix = _prefix(magic)
    path = _absolute(path, prefix)
    _check_entry(os.path.dirname(path), prefix, folder=True, required=True)
    _check_entry(path, prefix, folder=False, required=True)
    for suffix in _SIDE_FILES:
        _check_entry(path + suffix, prefix, folder=False, required=False)


def open_store(path, magic: str) -> sqlite3.Connection:
    """``check_store``, then open the store read-write without ever creating it.

    Returns a connection with ``isolation_level=None``, ``check_same_thread=False``, a 30-second
    busy timeout and ``PRAGMA synchronous=FULL``, as every store's own open has used, after reading
    ``meta.magic`` and finding ``magic``. The caller owns the connection.
    """
    prefix = _prefix(magic)
    check_store(path, magic)
    path = _absolute(path, prefix)
    # mode=rw: a file removed after the check is an error here, not a new empty database.
    uri = Path(path).as_uri() + "?mode=rw"
    try:
        db = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False, timeout=30)
    except sqlite3.Error:
        if not os.path.lexists(path):
            raise Refused(f"{prefix}_missing", os.path.basename(path)) from None
        raise Refused(f"{prefix}_invalid", "cannot be opened") from None
    try:
        db.execute("PRAGMA synchronous=FULL")  # SQLite's usual default, stated so a build with another one still syncs
        row =db.execute("SELECT v FROM meta WHERE k='magic'").fetchone()
    except sqlite3.Error:
        db.close()
        raise Refused(f"{prefix}_invalid", "not a readable store") from None
    if row is None or row[0] != magic:
        db.close()
        raise Refused(f"{prefix}_invalid", "another store's magic")
    return db


def create_ledger(path, anchor_public_key: str) -> None:
    """Installer step: a new authority ledger bound to ``anchor_public_key`` (``authority.create_ledger``)."""
    from .authority import create_ledger as factory

    _create(path, LEDGER_MAGIC, lambda tmp: factory(tmp, anchor_public_key))


def create_breaker_store(path) -> None:
    """Installer step: a new breaker store (``breaker.create_breaker_store``)."""
    from .breaker import create_breaker_store as factory

    _create(path, BREAKER_MAGIC, factory)


def create_revocation_store(path) -> None:
    """Installer step: a new revocation store (``manifest.create_revocation_store``)."""
    from .manifest import create_revocation_store as factory

    _create(path, REVOCATION_MAGIC, factory)


def _create(path, magic: str, factory) -> None:
    """Build the store under a temporary name in its folder, then link it to ``path`` exclusively.

    The folder must already exist and pass the same checks as ``check_store``'s. Anything at
    ``path`` or at one of its side files refuses ``P_exists`` and is left as it is: a leftover
    ``-wal`` would be replayed into the new database. A failure before the link leaves no file at
    ``path``, and no temporary file is left either way; the factory's own exception (an ``OSError``
    or ``sqlite3.Error``) is not translated. The new store has the factory's mode, 0600: a umask can
    only take bits away, and one that takes the owner's own makes the factory fail. The folder is
    fsynced after the link, and the result must pass ``open_store``.
    """
    prefix = _prefix(magic)
    path = _absolute(path, prefix)
    folder = os.path.dirname(path)
    _check_entry(folder, prefix, folder=True, required=True)
    # The store's own path is taken exclusively by the link below; its side files are checked here.
    for candidate in (path + suffix for suffix in _SIDE_FILES):
        if os.path.lexists(candidate):
            raise Refused(f"{prefix}_exists", os.path.basename(candidate))
    tmp =os.path.join(folder, f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp")
    try:
        factory(tmp)
        for suffix in _SIDE_FILES:
            if os.path.lexists(tmp + suffix):
                raise Refused(f"{prefix}_invalid", f"the new store left a {suffix} file")
        try:
            os.link(tmp, path, follow_symlinks=False)
        except FileExistsError:
            raise Refused(f"{prefix}_exists", os.path.basename(path)) from None
        except OSError as exc:
            raise Refused(f"{prefix}_invalid", f"cannot link the new store: {type(exc).__name__}") from None
    finally:
        for leftover in (tmp, *(tmp + suffix for suffix in _SIDE_FILES)):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    open_store(path, magic).close()
