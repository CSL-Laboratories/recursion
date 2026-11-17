"""Make the worktree a ``Place`` already names, by that place's mechanism.

``make(place, base_ref=)`` creates ``place.path`` and returns it. It does not choose again, and it does
not grant anyone write access to the source repository.

The mechanism is the one ``place.choose`` bound at spawn:

- ``reflink`` shares each regular file's extents (a later write in the new tree copies that file only).
- ``hardlink`` shares the git object files and copies every other file. Refs, ``HEAD`` and the index are
  copies, so a write in the new tree does not change a file in the source git directory.
- ``copy`` writes new bytes for the work tree and the git directory.
- ``linked`` is ``git worktree add --detach``. It is the only mechanism that writes the source git
  common directory, and only a place that was chosen with ``linked=True`` carries it.

Symlinks are recreated and not followed. A partial tree is removed only when this function's own stamp
is present and the path is outside the source repository. A finished tree is returned as it is.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from recursion_core.workspace import validate as V
from recursion_core.workspace.place import LINKED, Place

# Linux FICLONE, the same call ``place`` probes with. A failure means this file cannot share extents.
_FICLONE = 0x40049409

_GIT = '/usr/bin/git'
_GIT_ARGS = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'credential.helper=')
_ENV = {
    'PATH': '/usr/local/bin:/usr/bin:/bin',
    'HOME': '/nonexistent/recursion-workspace-spawn',
    'XDG_CONFIG_HOME': '/nonexistent/recursion-workspace-spawn',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': '/dev/null',
    'GIT_TERMINAL_PROMPT': '0',
    'LC_ALL': 'C',
}
_PACK_SUFFIX = {'.pack', '.idx', '.rev', '.bitmap', '.keep', '.mtimes', '.promisor'}
_HEX = frozenset('0123456789abcdef')
_MARKER = 'recursion-spawn-complete'


class SpawnFailed(RuntimeError):
    """The worktree was not made. ``reason`` is a short word, not a git message."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _overlaps(one: Path, other: Path) -> bool:
    one, other = Path(os.path.realpath(one)), Path(os.path.realpath(other))
    return one == other or one in other.parents or other in one.parents


def _stamp(dest: Path) -> Path:
    return dest.parent / ('.' + dest.name + '.partial')


def _complete(dest: Path) -> bool:
    git = dest / '.git'
    try:
        kind = git.lstat().st_mode
    except OSError:
        return False
    if stat.S_ISREG(kind):
        try:
            return git.read_bytes().startswith(b'gitdir: ')
        except OSError:
            return False
    if not stat.S_ISDIR(kind):
        return False
    return (git / _MARKER).is_file()


def _empty(dest: Path) -> bool:
    try:
        return dest.is_dir() and not any(dest.iterdir())
    except OSError:
        return False


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        wrote = os.write(fd, view)
        if wrote <= 0:
            raise OSError('short write')
        view = view[wrote:]


def _copy_file(src: Path, dst: Path) -> None:
    sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        perm = stat.S_IMODE(os.fstat(sfd).st_mode) & 0o777
        dfd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, perm or 0o644)
        try:
            while True:
                chunk = os.read(sfd, 1024 * 1024)
                if not chunk:
                    break
                _write_all(dfd, chunk)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def _reflink_file(src: Path, dst: Path) -> None:
    import fcntl
    sfd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        perm = stat.S_IMODE(os.fstat(sfd).st_mode) & 0o777
        dfd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, perm or 0o644)
        try:
            fcntl.ioctl(dfd, _FICLONE, sfd)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)


def _put_file(src: Path, dst: Path, mode: str) -> None:
    if mode == 'hardlink':
        os.link(src, dst)
        return
    if mode == 'reflink':
        _reflink_file(src, dst)
        return
    if mode != 'copy':
        raise SpawnFailed('mechanism')
    _copy_file(src, dst)


def _object_mode(rel: Path, mechanism: str) -> str:
    """How one git-directory file is transferred. Only object bytes are hardlinked."""
    if mechanism == 'reflink':
        return 'reflink'
    if mechanism != 'hardlink':
        return 'copy'
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == 'objects' and len(parts[1]) == 2 and all(char in _HEX for char in parts[1]):
        return 'hardlink'
    if len(parts) >= 3 and parts[0] == 'objects' and parts[1] == 'pack' and rel.suffix in _PACK_SUFFIX:
        return 'hardlink'
    return 'copy'


def _transfer(src: Path, dst: Path, mode_of, *, skip=()) -> None:
    """Copy the tree at ``src`` onto a new ``dst``. ``mode_of(relative)`` is ``copy``, ``reflink`` or ``hardlink``."""
    os.mkdir(dst, 0o700)
    _transfer_into(src, dst, Path(), mode_of, skip=skip)


def _transfer_into(src: Path, dst: Path, rel: Path, mode_of, *, skip=()) -> None:
    with os.scandir(src) as scan:
        entries = list(scan)
    for entry in entries:
        name = entry.name
        child = rel / name
        if name.endswith('.lock') or child in skip:
            continue
        target = dst / name
        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), target)
            continue
        if entry.is_dir(follow_symlinks=False):
            os.mkdir(target, 0o700)
            _transfer_into(Path(entry.path), target, child, mode_of, skip=skip)
            continue
        if entry.is_file(follow_symlinks=False):
            _put_file(Path(entry.path), target, mode_of(child))


def _git_admin(work: Path) -> tuple[Path, Path]:
    marker = work / '.git'
    gitdir = None
    try:
        kind = marker.lstat().st_mode
    except OSError:
        kind = None
    if kind is not None and stat.S_ISREG(kind):
        gitdir = V._gitfile(marker)
    elif kind is not None and stat.S_ISDIR(kind) and V._is_git_directory(marker):
        gitdir = marker
    if gitdir is None:
        raise SpawnFailed('not_a_repository')
    common = V._common_dir(gitdir)
    if common is None:
        raise SpawnFailed('not_a_repository')
    return Path(os.path.realpath(gitdir)), Path(os.path.realpath(common))


def _git(folder: Path, *args: str, allow_fail: bool = False) -> bytes:
    if not os.access(_GIT, os.X_OK):
        raise SpawnFailed('git_missing')
    env = dict(_ENV)
    env['GIT_DIR'] = str(folder / '.git')
    env['GIT_WORK_TREE'] = str(folder)
    done = subprocess.run([_GIT, '-C', os.fspath(folder), *_GIT_ARGS, *args], stdin=subprocess.DEVNULL,
                          capture_output=True, env=env, timeout=120, check=False)
    if done.returncode != 0 and not allow_fail:
        raise SpawnFailed('git_failed')
    return done.stdout


def _independent(place: Place, dest: Path, base_ref: str | None) -> None:
    work = place.repository
    gitdir, common = _git_admin(work)
    if _overlaps(dest, common):
        raise SpawnFailed('place_overlaps_repository')
    os.mkdir(dest, 0o700)
    file_mode = 'reflink' if place.mechanism == 'reflink' else 'copy'
    # ``.git`` is copied below as an independent directory, never as the source's own git directory.
    _transfer_into(work, dest, Path(), lambda _rel: file_mode, skip={Path('.git')})
    git = dest / '.git'
    if git.exists() or git.is_symlink():
        raise SpawnFailed('nested_git')
    os.mkdir(git, 0o700)
    objects = common / 'objects'
    if objects.is_dir():
        _transfer(objects, git / 'objects', lambda rel: _object_mode(Path('objects') / rel, place.mechanism),
                  skip={Path('info/alternates')})
    for name in ('refs', 'info'):
        src = common / name
        if src.is_dir():
            _transfer(src, git / name, lambda rel, name=name: _object_mode(Path(name) / rel, place.mechanism))
    for name in ('config', 'packed-refs', 'description', 'shallow'):
        src = common / name
        if src.is_file():
            _put_file(src, git / name, _object_mode(Path(name), place.mechanism))
    head = gitdir / 'HEAD'
    if head.is_symlink() or head.is_file():
        if head.is_symlink():
            os.symlink(os.readlink(head), git / 'HEAD')
        else:
            _put_file(head, git / 'HEAD', 'copy')
    index = gitdir / 'index'
    if index.is_file():
        _put_file(index, git / 'index', 'copy')
    # Pin both directory variables before any git command, then drop a core.worktree that names the source.
    _git(dest, 'config', '--local', '--unset-all', 'core.worktree', allow_fail=True)
    if base_ref is not None:
        # The ref is one argument and cannot start with '-'. ``--`` would make git take it as a path.
        _git(dest, 'checkout', '--detach', '--force', base_ref)
    (git / _MARKER).write_text('complete\n', encoding='ascii')


def _linked(place: Place, dest: Path, base_ref: str | None) -> None:
    if place.git_common_dir is None:
        raise SpawnFailed('not_a_repository')
    ref = base_ref if base_ref is not None else 'HEAD'
    # ``git -C`` the source is what registers the worktree. The child environment still refuses hooks.
    if not os.access(_GIT, os.X_OK):
        raise SpawnFailed('git_missing')
    done = subprocess.run(
        [_GIT, '-C', os.fspath(place.repository), *_GIT_ARGS, 'worktree', 'add', '--detach', '-q',
         os.fspath(dest), ref],
        stdin=subprocess.DEVNULL, capture_output=True, env=_ENV, timeout=120, check=False)
    if done.returncode != 0 or not _complete(dest):
        if dest.exists() and not dest.is_symlink():
            shutil.rmtree(dest, ignore_errors=True)
        subprocess.run(
            [_GIT, '-C', os.fspath(place.repository), *_GIT_ARGS, 'worktree', 'prune'],
            stdin=subprocess.DEVNULL, capture_output=True, env=_ENV, timeout=120, check=False)
        raise SpawnFailed('git_failed')


def _remove(dest: Path, repo: Path, common: Path | None) -> None:
    if dest.is_symlink() or not dest.is_dir():
        raise SpawnFailed('place_not_a_directory')
    if _overlaps(dest, repo) or (common is not None and _overlaps(dest, common)):
        raise SpawnFailed('place_overlaps_repository')
    shutil.rmtree(dest)


def make(place: Place, *, base_ref: str | None = None) -> Path:
    """Create ``place.path`` by ``place.mechanism`` and return it. See the module text."""
    if not isinstance(place, Place):
        raise TypeError('place must be Place')
    if place.engine_worktree_switching:
        raise SpawnFailed('engine_worktree_switching')
    if base_ref is not None and (not isinstance(base_ref, str) or not base_ref or base_ref.startswith('-')):
        raise SpawnFailed('base_ref')
    dest = place.path
    repo = place.repository
    common = place.git_common_dir
    if _overlaps(dest, repo) or (common is not None and _overlaps(dest, common)):
        raise SpawnFailed('place_overlaps_repository')
    if dest.is_symlink():
        raise SpawnFailed('place_is_a_link')
    stamp = _stamp(dest)
    if _complete(dest):
        if stamp.exists():
            try:
                stamp.unlink()
            except OSError:
                pass
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dest.exists():
        if stamp.is_file() or _empty(dest):
            _remove(dest, repo, common)
        else:
            raise SpawnFailed('place_not_empty')
    try:
        stamp.write_text('partial\n', encoding='ascii')
    except OSError as exc:
        raise SpawnFailed('place_unwritable') from exc
    try:
        if place.mechanism == LINKED:
            _linked(place, dest, base_ref)
        elif place.mechanism in ('reflink', 'hardlink', 'copy'):
            _independent(place, dest, base_ref)
        else:
            raise SpawnFailed('mechanism')
    except BaseException:
        # A finished tree stays. Removing it would drop the identity the next ask returns.
        if (dest.exists() and not _complete(dest) and not _overlaps(dest, repo)
                and (common is None or not _overlaps(dest, common))):
            shutil.rmtree(dest, ignore_errors=True)
        raise
    try:
        stamp.unlink()
    except OSError:
        pass
    return dest
