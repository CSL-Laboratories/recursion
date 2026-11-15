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
system the way git 2.43 reads it to find a repository (``setup.c``: ``setup_git_directory_gently_1``,
``read_gitfile_gently``, ``is_git_directory``, ``validate_headref``, ``check_repository_format_gently``,
``setup_explicit_git_dir``) and runs no git. From the folder as it really is, upwards, never across a mount
(git's default), each folder is asked two things in turn:

- its ``.git``, links followed. A FILE there must be a gitfile git accepts: at most 1 MiB, ``gitdir: <path>``
  (a relative path is the file's folder's), naming a git directory. One git refuses ends the search with None,
  because git refuses the folder too; it does not go on to a repository further up. A git DIRECTORY there is
  the repository found. Anything else (nothing, a dangling link, a folder that is no git directory, a pipe) is
  passed over, as git passes it over.
- the folder itself: a git directory (a bare repository, or the ``.git`` folder of one) is the repository found.

The first repository found decides; the search goes no further up. Its own configuration decides the answer:

- ``core.worktree`` names the top: an absolute name as it really is, a relative one from the git directory as
  it really is. The folder is in the work tree when it is that top or inside it, and in none otherwise.
  ``core.bare`` true as well: no work tree (git warns).
- else ``core.bare`` true: no work tree.
- else the folder whose ``.git`` was found is the top, and a folder inside a git directory is in no work tree.

git takes the two settings from the ``config`` in the repository's common directory, and only when that file
gives ``core.repositoryformatversion`` as 0 or more. A linked worktree's git directory has a common directory of
its own, and git ignores the two settings there, unless ``extensions.worktreeConfig`` is on. Then the
worktree's ``config.worktree`` may override them. git reads no global, system or included file to find a
repository, and neither does this. git refuses the repository (the answer is None) when:

- a config file it reads cannot be read, or breaks its syntax (sections, ``"subsections"``, quoting, the four
  escapes, comments, continued lines, a byte-order mark);
- a value is one git does not take. A boolean is true, yes, on, false, no, off, empty, or a number as
  ``strtoimax`` reads one in base 0, with a unit k, m or g, within an int. ``core.worktree`` needs a value;
- the version is above 1, or names at version 0 an extension only version 1 knows;
- ``extensions.objectFormat`` is not ``sha1`` or ``sha256``.

Four things git refuses later, when it reads the whole configuration or opens the repository, are not read
here. The provider's own git meets them at launch:

- an extension git 2.43 does not know is not held against a repository (a newer git may know it:
  ``refStorage``);
- the values of other keys;
- the files an ``include`` names;
- the owner check (``safe.directory``).

A config file over 16 MiB, or one that is not a regular file, is refused here: git would read the first and
wait on a pipe.

A git directory has a ``HEAD`` git accepts (a link into ``refs/``, or a file that begins ``ref:`` and, after
git's own spaces, ``refs/``, or one that begins with an object id). In its common directory (the folder its
``commondir`` file names, which a linked worktree's git directory has; else itself) it has an ``objects`` and
a ``refs`` that may be entered. A file is read without waiting, so a pipe where git expects a file answers at
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


# ---------------------------------------------------------------- the repository's own configuration
_CONFIG_MAX = 1 << 24                           # this lane's limit on a config file (git has none)
_C_SPACE = b' \t\n\v\f\r'                        # the C library's isspace, which strtoimax skips
_INT_MAX = (1 << 31) - 1
_UNITS = {b'': 1, b'k': 1 << 10, b'm': 1 << 20, b'g': 1 << 30}
_DIGITS = {2: b'01', 8: b'01234567', 10: b'0123456789', 16: b'0123456789abcdefABCDEF'}
_ALPHA = frozenset(b'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ')
_KEY = _ALPHA | frozenset(b'0123456789-')       # git's iskeychar
_ESCAPES = {ord('t'): ord('\t'), ord('b'): ord('\b'), ord('n'): ord('\n'), ord('\\'): ord('\\'), ord('"'): ord('"')}
_LF = ord('\n')


def _int(value: bytes | None) -> int | None:
    """git's ``git_parse_int``: ``strtoimax`` in base 0 (``0x`` hexadecimal, ``0b`` binary as this C library
    reads it, a leading ``0`` octal), then nothing or a unit ``k``, ``m``, ``g``, within an int; else None."""
    if not value:
        return None
    text = value.lstrip(_C_SPACE)
    sign = -1 if text[:1] == b'-' else 1
    if text[:1] in (b'-', b'+'):
        text = text[1:]
    base = 10
    if text[:1] == b'0':                       # a 0x or 0b with no digit after it is refused either way
        base = 8
        if text[1:2] in (b'x', b'X'):
            base, text = 16, text[2:]
        elif text[1:2] in (b'b', b'B'):
            base, text = 2, text[2:]
    end = 0
    while end < len(text) and text[end] in _DIGITS[base]:
        end += 1
    factor = _UNITS.get(text[end:].lower())
    if not end or factor is None:
        return None
    number = sign * int(text[:end], base)
    return None if abs(number) > _INT_MAX // factor else number * factor


def _bool(value: bytes | None) -> bool | None:
    """git's ``git_config_bool``; None where git refuses the value."""
    if value is None:
        return True
    if not value:
        return False
    if value.lower() in (b'true', b'yes', b'on'):
        return True
    if value.lower() in (b'false', b'no', b'off'):
        return False
    number = _int(value)
    return None if number is None else number != 0


def _lower(char: int) -> int:
    return char + 32 if 65 <= char <= 90 else char


class _Source:
    """git's ``get_next_char``: a byte at a time, a CR before an LF dropped, the end read as an LF (``eof``)."""

    def __init__(self, data: bytes) -> None:
        self.data, self.at, self.eof = data, 0, False

    def next(self) -> int:
        if self.at >= len(self.data):
            self.eof = True
            return _LF
        char = self.data[self.at]
        self.at += 1
        if char == ord('\r') and self.data[self.at:self.at + 1] == b'\n':
            self.at += 1
            return _LF
        return char


def _subsection(source: _Source, name: bytearray, char: int) -> bytes | None:
    """git's ``get_extended_base_var``: after the section's name and a space, ``"<subsection>"]``."""
    while True:
        if char == _LF:
            return None
        char = source.next()
        if char not in _GIT_SPACE:
            break
    if char != ord('"'):
        return None
    name.append(ord('.'))
    while True:
        char = source.next()
        if char == _LF:
            return None
        if char == ord('"'):
            break
        if char == ord('\\'):
            char = source.next()
            if char == _LF:
                return None
        name.append(char)
    return bytes(name) if source.next() == ord(']') else None


def _section(source: _Source) -> bytes | None:
    """git's ``get_base_var``: after ``[``, the section's name lower-cased, and its subsection as written."""
    name = bytearray()
    while True:
        char = source.next()
        if source.eof:
            return None
        if char == ord(']'):
            return bytes(name)
        if char in _GIT_SPACE:
            return _subsection(source, name, char)
        if char not in _KEY and char != ord('.'):
            return None
        name.append(_lower(char))


def _value(source: _Source) -> bytes | None:
    """git's ``parse_value``: after ``=``, to the end of the line."""
    value, quoted, comment, trim = bytearray(), False, False, 0
    while True:
        char = source.next()
        if char == _LF:
            if quoted:
                return None
            if trim:
                del value[trim:]
            return bytes(value)
        if comment:
            continue
        if char in _GIT_SPACE and not quoted:
            if not trim:
                trim = len(value)
            if value:
                value.append(char)
            continue
        if char in b';#' and not quoted:
            comment = True
            continue
        trim = 0
        if char == ord('\\'):
            char = source.next()
            if char == _LF:
                continue
            if char not in _ESCAPES:
                return None
            value.append(_ESCAPES[char])
        elif char == ord('"'):
            quoted = not quoted
        else:
            value.append(char)


def _entry(source: _Source, name: bytearray) -> tuple | None:
    """git's ``get_value``: the rest of a key, and ``= value`` or nothing (the value None)."""
    while True:
        char = source.next()
        if char not in _KEY:
            break
        name.append(_lower(char))
    while char in b' \t':
        char = source.next()
    value = None
    if char != _LF:
        value = _value(source) if char == ord('=') else None
        if value is None:
            return None
        value = value.split(b'\0')[0]                 # git hands its callback a C string
    return bytes(name).split(b'\0')[0], value


def _entries(data: bytes) -> list | None:
    """git's ``git_parse_source``: a config file's ``(name, value)`` entries in order, or None where git refuses
    the file. A name is ``section.key`` (``section.subsection.key``), lower-cased but for the subsection."""
    if data[:1] == b'\xef':
        if data[:3] != b'\xef\xbb\xbf':               # a byte-order mark, whole or not at all
            return None
        data = data[3:]
    source, entries, stem, comment = _Source(data), [], b'', False
    while True:
        char = source.next()
        if char == _LF:
            if source.eof:
                return entries
            comment = False
        elif comment or char in _GIT_SPACE:
            continue
        elif char in b'#;':
            comment = True
        elif char == ord('['):
            section = _section(source)
            if not section:
                return None
            stem = section + b'.'
        elif char in _ALPHA:
            entry = _entry(source, bytearray(stem + bytes((_lower(char),))))
            if entry is None:
                return None
            entries.append(entry)
        else:
            return None


def _config(path: Path) -> list | None:
    """The entries of the config file at ``path``: none when there is no file, None where it is refused (it
    cannot be read, it is not a regular file, it is over ``_CONFIG_MAX``, git would not parse it)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except (OSError, ValueError):
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks, size = [], 0
        while size <= _CONFIG_MAX:
            chunk = os.read(fd, _CONFIG_MAX + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        return None
    finally:
        os.close(fd)
    return None if size > _CONFIG_MAX else _entries(b''.join(chunks))


def _core(name: bytes, value: bytes | None, settings: tuple) -> tuple | None:
    """git's ``read_worktree_config``: ``core.bare`` and ``core.worktree`` into ``(bare, worktree)``."""
    bare, worktree = settings
    if name == b'core.bare':
        bare = _bool(value)
        if bare is None:
            return None
    elif name == b'core.worktree':
        if value is None:
            return None
        worktree = value
    return bare, worktree


def _settings(gitdir: Path) -> tuple | None:
    """git's ``check_repository_format_gently``: the ``(bare, worktree)`` the repository's configuration gives
    discovery, each None when not given, or None where git refuses the repository. See the module's text."""
    common = _common_dir(gitdir)
    entries = None if common is None else _config(common / 'config')
    if entries is None:
        return None
    version, per_worktree, newer, settings = -1, False, False, (None, None)
    for name, value in entries:
        if name == b'core.repositoryformatversion':
            version = _int(value)
            if version is None:
                return None
        elif name in (b'extensions.preciousobjects', b'extensions.worktreeconfig'):
            flag = _bool(value)
            if flag is None:
                return None
            per_worktree = flag if name == b'extensions.worktreeconfig' else per_worktree
        elif name == b'extensions.partialclone' and value is None:
            return None
        elif name == b'extensions.objectformat' and value not in (b'sha1', b'sha256'):
            return None
        elif name in (b'extensions.objectformat', b'extensions.noop-v1'):
            newer = True
        else:
            settings = _core(name, value, settings)
            if settings is None:
                return None
    if version < 0:
        return None, None
    if version > 1 or (version == 0 and newer):
        return None
    if not per_worktree:
        return (None, None) if os.path.exists(gitdir / 'commondir') else settings
    entries = _config(gitdir / 'config.worktree')
    if entries is None:
        return None
    for name, value in entries:
        settings = _core(name, value, settings)
        if settings is None:
            return None
    return settings


def _named_top(gitdir: Path, value: bytes) -> Path | None:
    """The folder ``core.worktree`` names, as git resolves it (see the module's text); None where git could not."""
    name = os.fsdecode(value)
    if not name:                                  # git: "cannot chdir to ''"
        return None
    if not name.startswith('/'):
        name = os.path.join(gitdir, name)         # resolved below as git's chdir resolves it: links first
    try:
        return Path(os.path.realpath(name, strict=True))
    except (OSError, ValueError):
        return None


def _top(gitdir: Path, found: Path | None, folder: Path) -> Path | None:
    """The answer once ``gitdir`` is found: ``found`` is the folder whose ``.git`` it is, None when ``folder`` is
    inside the git directory itself."""
    settings = _settings(gitdir)
    if settings is None:
        return None
    bare, worktree = settings
    if worktree is not None and not bare:
        top = _named_top(gitdir, worktree)
        return top if top is not None and _inside(folder, top) else None
    return None if bare or found is None else found


def git_work_tree(folder) -> Path | None:
    """The top of the git work tree ``folder`` is in, or None, as git's own discovery finds it. See the
    module's text. It reads the file system and runs nothing."""
    try:
        start = Path(os.path.realpath(folder, strict=True))
        device = os.stat(start).st_dev
    except (OSError, ValueError):
        return None
    current = start
    while True:
        marker = current / '.git'
        try:
            kind = os.stat(marker).st_mode
        except (OSError, ValueError):
            kind = None
        if kind is not None and stat.S_ISREG(kind):
            gitdir = _gitfile(marker)
            return None if gitdir is None else _top(gitdir, current, start)
        if kind is not None and _is_git_directory(marker):
            return _top(marker, current, start)
        if _is_git_directory(current):
            return _top(current, None, start)
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
