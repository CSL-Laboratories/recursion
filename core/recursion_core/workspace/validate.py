"""The checks on a folder a run is asked to work in. They read the file system and change nothing.

``validate(path, state_dir=...)`` answers the folder as it really is, every link resolved, or raises
``WorkspaceInvalid`` whose ``reason`` is one of ``REASONS``. The rules ``validate`` checks, in this order:

``not_text``          the path is not a non-empty string, or it holds a NUL or a line break.
``not_absolute``      a relative path: the daemon's own working folder means nothing to the caller.
``dot_dot``           a ``..`` part: a folder is named as it is, not by a walk up from somewhere else.
``not_found``         nothing is there, or a part of the path is not a folder.
``link_loop``         the links along the path never end.
``unreadable``        the path cannot be followed (no permission on a folder above it, a name too long).
``not_a_directory``   it is a file, a device, a socket, a pipe.
``not_owned``         the folder is not owned by the operator, the user the daemon runs as.
``not_accessible``    its owner may not list it and enter it (the owner bits lack read or execute).
``state_directory``   it is the daemon's state directory or inside it, or inside another folder the
                      caller protects (the runtime folder, for example).

and one rule ``validate`` does NOT check, because it is a rule about the folder a run WORKS in, and in
worktree mode that is not the folder asked for:

``not_unit_safe``     the launcher could not put the run's working folder in a unit property: it holds a
                      character outside ``UNIT_PATH_SAFE`` (a space, for example). The launcher refuses such a
                      folder (``working_directory_not_unit_safe``) after a run record exists. The start
                      normaliser asks ``unit_safe`` of the folder the run will work in (in direct mode the folder
                      asked for; in worktree mode its place in the run's worktree under the state directory),
                      so that the caller hears it before any record is made. A worktree's source folder may be
                      named with any character.

LINKS. Every check is made on the RESOLVED folder, and the resolved folder is what the caller gets back and
what a run then works in. So a link that leads out of the operator's own files, to a folder another user
owns or into the state directory, is refused by the rule its target breaks; and a link changed after the
check cannot move the run, because the run never works through the link, only in the folder the link led
to when it was checked. The provider that makes the run's folder at launch (seam S4) checks again then.

GIT. ``git_work_tree(folder)`` answers the top of the git work tree a folder is in, or None. It reads the file
system the way git's own discovery reads it (``setup.c`` in git 2.43: ``setup_git_directory_gently_1``,
``read_gitfile_gently``, ``is_git_directory``, ``validate_headref``) and runs no git. From the folder upwards,
never across a mount (git's default), each folder is asked two things in turn:

- its ``.git``, links followed. A FILE there must be a gitfile git accepts: at most 1 MiB, ``gitdir: <path>``
  (a relative path is the file's folder's), naming a git directory. One git refuses ends the search with None,
  because git refuses the folder too; it does not go on to a repository further up. A git DIRECTORY there
  makes this folder the top. Anything else (nothing, a dangling link, a folder that is no git directory, a
  pipe) is passed over, as git passes it over.
- the folder itself: a git directory (a bare repository, or the ``.git`` folder of one) is in no work tree,
  so the answer is None.

A git directory has a ``HEAD`` git accepts (a link into ``refs/``, or a file that begins ``ref:`` and, after
git's own spaces, ``refs/``, or one that begins with an object id), and in its common directory (the folder its
``commondir`` file names, which a linked worktree's git directory has; else itself) an ``objects`` and a
``refs`` that may be entered. A file is read without waiting, so a pipe where git expects a file answers at
once. ``valid_base_ref(text)`` says whether a base given in a request is a revision this lane passes to git: a
ref name as git's own rules allow one, or a commit id, with ``~N`` and ``^N`` steps.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from pathlib import Path

REASONS = ('not_text', 'not_absolute', 'dot_dot', 'not_found', 'link_loop', 'unreadable', 'not_a_directory',
           'not_owned', 'not_accessible', 'state_directory', 'not_unit_safe')

# The characters the launcher lets into a unit's working-directory property (daemon/launcher.py, _PATH_SAFE).
# A test holds the two sets equal.
UNIT_PATH_SAFE = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-+@,')


class WorkspaceInvalid(Exception):
    """A folder a run may not work in. ``reason`` is one of ``REASONS``."""

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError(f'no reason named {reason!r}')
        super().__init__(reason)
        self.reason = reason


def _inside(folder: Path, root: Path) -> bool:
    return folder == root or root in folder.parents


def _root(path) -> Path:
    """A protected folder as it really is. It need not exist yet: the part that exists is resolved."""
    return Path(os.path.realpath(os.fspath(path)))


def validate(path, *, state_dir, protected=(), uid: int | None = None) -> Path:
    """The folder ``path`` names, resolved, when a run may work in it; else ``WorkspaceInvalid``.

    ``state_dir`` is the daemon's state directory (None: no state directory is refused, which only ``grant``
    asks, because a run's worktree lives under it); ``protected`` are more folders refused the same way.
    ``uid`` is the operator's, by default the user this process runs as."""
    if not isinstance(path, str) or not path or '\x00' in path or '\n' in path or '\r' in path:
        raise WorkspaceInvalid('not_text')
    if not os.path.isabs(path):
        raise WorkspaceInvalid('not_absolute')
    if '..' in Path(path).parts:
        raise WorkspaceInvalid('dot_dot')
    try:
        resolved = Path(os.path.realpath(path, strict=True))
        info = os.stat(resolved)
    except FileNotFoundError:
        raise WorkspaceInvalid('not_found') from None
    except NotADirectoryError:
        raise WorkspaceInvalid('not_found') from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise WorkspaceInvalid('link_loop') from None
        raise WorkspaceInvalid('unreadable') from None
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceInvalid('not_a_directory')
    if info.st_uid != (os.geteuid() if uid is None else uid):
        raise WorkspaceInvalid('not_owned')
    if info.st_mode & (stat.S_IRUSR | stat.S_IXUSR) != (stat.S_IRUSR | stat.S_IXUSR):
        raise WorkspaceInvalid('not_accessible')
    for root in (state_dir, *protected):
        if root is not None and _inside(resolved, _root(root)):
            raise WorkspaceInvalid('state_directory')
    return resolved


def unit_safe(path) -> bool:
    """Whether the launcher could name the folder ``path`` in a unit's working-directory property."""
    return all(char in UNIT_PATH_SAFE for char in os.fspath(path))


# ------------------------------------------------------------------------------------------------ git
_GITFILE_MAX = 1 << 20                          # git's limit on a .git file (read_gitfile_gently)
_OBJECT_ID = re.compile(rb'[0-9a-fA-F]{40}')    # get_oid_hex_any: a SHA-1 id, or the first 40 of a SHA-256 one
_GIT_SPACE = b' \t\n\r'                          # git's own isspace (ctype.c): not the vertical tab or form feed


def _read(path, limit: int) -> bytes | None:
    """At most ``limit`` bytes of the file at ``path``, links followed, or None. It is opened without waiting,
    so a pipe answers at once (git would wait for a writer)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        return None
    try:
        return os.read(fd, limit)
    except OSError:
        return None
    finally:
        os.close(fd)


def _head_valid(head: Path) -> bool:
    """git's ``validate_headref``: see the module's text."""
    if os.path.islink(head):
        try:
            return os.readlink(head).startswith('refs/')
        except OSError:
            return False
    data = _read(head, 255)
    if data is None:
        return False
    if data.startswith(b'ref:') and data[4:].lstrip(_GIT_SPACE).startswith(b'refs/'):
        return True
    return _OBJECT_ID.match(data) is not None


def _common_dir(gitdir: Path) -> Path | None:
    """git's ``get_common_dir``: the folder the ``commondir`` file names, else the git directory itself; None
    where git could not read the file (it stops)."""
    marker = gitdir / 'commondir'
    if not os.path.exists(marker):
        return gitdir
    data = _read(marker, _GITFILE_MAX)
    if not data:
        return None
    data = data.rstrip(b'\r\n')
    if b'\x00' in data:
        return None
    return gitdir / os.fsdecode(data)


def _is_git_directory(folder: Path) -> bool:
    """git's ``is_git_directory``: see the module's text."""
    if not _head_valid(folder / 'HEAD'):
        return False
    common = _common_dir(folder)
    return common is not None and os.access(common / 'objects', os.X_OK) and os.access(common / 'refs', os.X_OK)


def _gitfile(marker: Path) -> Path | None:
    """git's ``read_gitfile_gently``: the git directory a ``.git`` FILE names, or None where git refuses it."""
    data = _read(marker, _GITFILE_MAX + 1)
    if data is None or len(data) > _GITFILE_MAX or not data.startswith(b'gitdir: '):
        return None
    target = data[8:].rstrip(b'\r\n')
    if not target or b'\x00' in target:
        return None
    folder = marker.parent / os.fsdecode(target)
    return folder if _is_git_directory(folder) else None


def git_work_tree(folder) -> Path | None:
    """The top of the git work tree ``folder`` is in, or None, as git's own discovery finds it. See the
    module's text. It reads the file system and runs nothing."""
    current = Path(folder)
    try:
        device = os.stat(current).st_dev
    except (OSError, ValueError):
        return None
    while True:
        marker = current / '.git'
        try:
            kind = os.stat(marker).st_mode
        except (OSError, ValueError):
            kind = None
        if kind is not None and stat.S_ISREG(kind):
            return current if _gitfile(marker) is not None else None
        if kind is not None and _is_git_directory(marker):
            return current
        if _is_git_directory(current):
            return None
        parent = current.parent
        if parent == current:
            return None
        try:
            if os.stat(parent).st_dev != device:
                return None
        except OSError:
            return None
        current = parent


_SPLIT = re.compile(r'(.*?)((?:[~^][0-9]*)*)', re.DOTALL)
_FORBIDDEN = set(' ~^:?*[\\') | {chr(code) for code in range(0x20)} | {'\x7f'}


def _ref_name(name: str) -> bool:
    """A ref name by git's own rules (``git check-ref-format --allow-onelevel``). A commit id in hex is one too.
    "No empty part" is what refuses a leading, trailing or doubled ``/``."""
    if name == '@' or name.endswith('.') or '..' in name or '@{' in name or any(char in _FORBIDDEN for char in name):
        return False
    return all(part and not part.startswith('.') and not part.endswith('.lock') for part in name.split('/'))


def valid_base_ref(text) -> bool:
    """Whether ``text`` is a base this lane hands to git: at most 256 characters, not beginning with ``-``, a ref
    name or a commit id, then any number of ``~N`` or ``^N`` steps (``N`` any number of digits, or none)."""
    if not isinstance(text, str) or not text or len(text) > 256 or text.startswith('-') or not text.isascii():
        return False
    return _ref_name(_SPLIT.fullmatch(text).group(1))     # the shortest name that the steps can follow
