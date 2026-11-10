"""Filesystem paths for plugin files and installer stores.

SQLite's default ``SQLITE_MAX_PATHNAME`` is 512 bytes, and opening a database also
names ``<path>-journal``. A path of 505 bytes or more fails that open
(``OperationalError``). A path the process filesystem encoding cannot represent
raises ``UnicodeEncodeError`` from ``os.open`` and from ``sqlite3.connect``. Both
become a typed refusal before the file is created.
"""

from __future__ import annotations

import os
import stat

from .errors import Refused

__all__ = ["check_store_path", "resolve_within_root"]

_JOURNAL = b"-journal"
_SQLITE_MAX_PATHNAME = 512


def check_store_path(path: object) -> str:
    """Refuse a store path SQLite cannot open, before it is created."""
    if type(path) is not str or not path or "\0" in path:
        raise Refused("store_path_invalid")
    try:
        encoded = os.fsencode(path)
    except UnicodeEncodeError:
        raise Refused("store_path_not_encodable") from None
    if len(encoded) + len(_JOURNAL) > _SQLITE_MAX_PATHNAME:
        raise Refused("store_path_too_long")
    return path


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def resolve_within_root(root: str, declared: str) -> str:
    """Resolve ``declared`` and refuse anything that leaves ``root``.

    Absolute paths, empty components, ``.`` and ``..`` are refused on the declared
    string. A symlink is followed only when its target stays inside ``root``; a link
    to the outside is refused. ``root`` itself must be a real directory, not a symlink.
    """
    if type(root) is not str or type(declared) is not str or "\0" in root or "\0" in declared:
        raise Refused("plugin_path_invalid")
    if os.path.isabs(declared):
        raise Refused("plugin_path_escapes_root")
    parts = declared.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise Refused("plugin_path_escapes_root")
    try:
        os.fsencode(root)
        os.fsencode(declared)
        if os.path.islink(root) or not os.path.isdir(root):
            raise Refused("plugin_root_invalid")
        root_real = os.path.realpath(root)
        current = root
        for part in parts:
            candidate = os.path.join(current, part)
            try:
                info = os.lstat(candidate)
            except FileNotFoundError:
                current = candidate
                continue
            except OSError:
                raise Refused("plugin_path_unreadable") from None
            if stat.S_ISLNK(info.st_mode):
                try:
                    resolved = os.path.realpath(candidate)
                except OSError:
                    raise Refused("plugin_path_unreadable") from None
                if not _inside(resolved, root_real):
                    raise Refused("plugin_path_escapes_root")
                current = resolved
            else:
                current = candidate
        if os.path.lexists(current):
            final = os.path.realpath(current)
            if not _inside(final, root_real):
                raise Refused("plugin_path_escapes_root")
        elif not _inside(os.path.realpath(os.path.dirname(current)) if os.path.dirname(current) else current, root_real):
            raise Refused("plugin_path_escapes_root")
        return current
    except Refused:
        raise
    except UnicodeEncodeError:
        raise Refused("plugin_path_unreadable") from None
    except OSError:
        raise Refused("plugin_path_unreadable") from None
