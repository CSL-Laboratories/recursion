"""The checks on a folder a run is asked to work in. They read the file system and change nothing.

``validate(path, state_dir=...)`` answers the folder as it really is, every link resolved, or raises
``WorkspaceInvalid`` whose ``reason`` is one of ``REASONS``. The rules, in the order they are checked:

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
``not_unit_safe``     the launcher could not put it in a unit property: it holds a character outside
                      ``UNIT_PATH_SAFE`` (a space, for example). The launcher refuses such a folder
                      (``working_directory_not_unit_safe``) after a run record exists; refusing it here
                      answers the caller before any record is made.

LINKS. Every check is made on the RESOLVED folder, and the resolved folder is what the caller gets back and
what a run then works in. So a link that leads out of the operator's own files, to a folder another user
owns or into the state directory, is refused by the rule its target breaks; and a link changed after the
check cannot move the run, because the run never works through the link, only in the folder the link led
to when it was checked. The provider that makes the run's folder at launch (seam S4) checks again then.

``git_work_tree(folder)`` says whether a folder is inside a git work tree, by reading the file system the
way git looks for its repository (a ``.git`` folder or file in the folder or above it, not across a mount),
and runs no git. ``valid_base_ref(text)`` says whether a base given in a request is a revision this lane
passes to git: a ref name as git's own rules allow one, or a commit id, with ``~N`` and ``^N`` steps.
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
    if not all(char in UNIT_PATH_SAFE for char in str(resolved)):
        raise WorkspaceInvalid('not_unit_safe')
    return resolved


def git_work_tree(folder) -> Path | None:
    """The top of the git work tree ``folder`` is in, or None: the nearest folder at or above it that holds
    ``.git`` (a folder with a ``HEAD`` file, or a file that begins ``gitdir:``), not looked for across a
    mount, as git itself does by default. It reads the file system and runs nothing."""
    current = Path(folder)
    try:
        device = os.stat(current).st_dev
    except OSError:
        return None
    while True:
        marker = current / '.git'
        try:
            info = os.lstat(marker)
        except OSError:
            info = None
        if info is not None:
            if stat.S_ISDIR(info.st_mode) and os.path.isfile(marker / 'HEAD'):
                return current
            if stat.S_ISREG(info.st_mode):
                try:
                    with open(marker, 'rb') as handle:
                        if handle.read(8) == b'gitdir: ':
                            return current
                except OSError:
                    pass
        parent = current.parent
        if parent == current:
            return None
        try:
            if os.stat(parent).st_dev != device:
                return None
        except OSError:
            return None
        current = parent


_SPLIT = re.compile(r'(.*?)((?:[~^][0-9]{0,6})*)', re.DOTALL)
_FORBIDDEN = set(' ~^:?*[\\') | {chr(code) for code in range(0x20)} | {'\x7f'}


def _ref_name(name: str) -> bool:
    """A ref name by git's own rules (``git check-ref-format --allow-onelevel``). A commit id in hex is one too.
    "No empty part" is what refuses a leading, trailing or doubled ``/``."""
    if name == '@' or name.endswith('.') or '..' in name or '@{' in name or any(char in _FORBIDDEN for char in name):
        return False
    return all(part and not part.startswith('.') and not part.endswith('.lock') for part in name.split('/'))


def valid_base_ref(text) -> bool:
    """Whether ``text`` is a base this lane hands to git: at most 256 characters, not beginning with ``-``, a ref
    name or a commit id, then any number of ``~N`` or ``^N`` steps."""
    if not isinstance(text, str) or not text or len(text) > 256 or text.startswith('-') or not text.isascii():
        return False
    return _ref_name(_SPLIT.fullmatch(text).group(1))     # the shortest name that the steps can follow
