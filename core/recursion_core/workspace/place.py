"""The directory a run's worktree is put in, chosen once at spawn from facts this machine measures.

``choose(repository, candidates, work_bytes=, object_bytes=)`` is that rule. For each candidate it measures
three facts and keeps the cheapest mechanism those facts can carry out:

- the same filesystem as the repository (``st_dev`` of the repository and of the directory the candidate will
  live in);
- whether a reflink can be made there, and whether a hardlink can (a probe file, removed before the answer);
- how many bytes are free there.

Cheapest first, among mechanisms that do not write the repository's git common directory:

- ``reflink`` shares the file extents. It needs a small headroom (``REFLINK_RESERVE``) and the same filesystem.
- ``hardlink`` shares the git objects and copies the work tree. It needs ``work_bytes`` plus that headroom, on
  the same filesystem.
- ``copy`` writes the work tree and the objects. It needs both sizes plus the headroom, on any filesystem.

The candidate that wins is outside the repository and outside its git common directory: neither holds the
other. A candidate inside either, or one that holds either, is skipped. Nothing here creates the directory
and nothing here writes the repository or its git common directory. The probe files are created only in an
existing directory that is itself outside both, and they are removed before the function returns, including
when the probe is interrupted.

A linked worktree is not in that order. It checks out ``work_bytes`` outside the repository and it writes the
git common directory (git's own registration of the worktree). It is used only when the caller passes
``linked=True``, and then only that mechanism: a candidate that cannot carry it is not replaced with a copy.

``Place`` is frozen. ``engine_worktree_switching`` is false on every one of them: the engines' own worktree
switching is off, and the identity bound at spawn is this object, not a path read again later. Two callers
with the same facts get equal places; a later change in free space does not move a place already returned.

``work_bytes`` is the size of the checkout. ``object_bytes`` is the size of the git objects. Both are counts
the caller measured. A bool, a negative, or anything else is ``ValueError`` before any probe.
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from recursion_core.workspace import validate as V

# Linux FICLONE, _IOW(0x94, 9, int). It shares extents. A plain copy reports success another way, so a failure
# here means this filesystem cannot reflink.
_FICLONE = 0x40049409

# Room kept free for the directory itself and the git files around a shared copy. A full disk is not a place.
REFLINK_RESERVE = 1 << 20

ENGINE_WORKTREE_SWITCHING = False
MECHANISMS = ('reflink', 'hardlink', 'copy')
LINKED = 'linked'
_COST = {'reflink': 0, 'hardlink': 1, 'copy': 2}
_PROBE_PREFIX = '.recursion-place-'


def _count(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'{name} must be a non-negative int')


def _inside(root: Path, folder: Path) -> bool:
    """True when ``folder`` is ``root`` or a directory inside it."""
    return folder == root or root in folder.parents


def _overlaps(one: Path, other: Path) -> bool:
    """True when the two directories are the same or one holds the other."""
    return _inside(one, other) or _inside(other, one)


@dataclass(frozen=True)
class Facts:
    """What ``measure`` read. Frozen, so a later reading does not change a choice already made from it."""

    repository: Path
    candidate: Path
    same_filesystem: bool
    reflink: bool
    hardlink: bool
    free_bytes: int
    work_bytes: int
    object_bytes: int
    git_common_dir: Path | None

    def __post_init__(self) -> None:
        _count(self.free_bytes, 'free_bytes')
        _count(self.work_bytes, 'work_bytes')
        _count(self.object_bytes, 'object_bytes')


@dataclass(frozen=True)
class Place:
    """The identity bound at spawn: the directory, the mechanism, and the facts it was chosen from."""

    path: Path
    mechanism: str
    engine_worktree_switching: bool
    repository: Path
    git_common_dir: Path | None
    writes_git_common_dir: bool
    same_filesystem: bool
    reflink: bool
    hardlink: bool
    free_bytes: int
    work_bytes: int
    object_bytes: int

    def __post_init__(self) -> None:
        if self.engine_worktree_switching is not False:
            raise ValueError('engine worktree switching is off')
        if self.mechanism not in (*MECHANISMS, LINKED):
            raise ValueError(f'mechanism {self.mechanism!r}')
        if self.writes_git_common_dir is not (self.mechanism == LINKED):
            raise ValueError('only a linked worktree writes the git common directory')
        if self.mechanism == LINKED and self.git_common_dir is None:
            raise ValueError('a linked worktree needs the git common directory')
        _count(self.free_bytes, 'free_bytes')
        _count(self.work_bytes, 'work_bytes')
        _count(self.object_bytes, 'object_bytes')


def _resolve(path, *, must_exist: bool) -> Path | None:
    try:
        text = os.fspath(path)
    except TypeError:
        return None
    if not isinstance(text, str) or not text or '\x00' in text or not os.path.isabs(text):
        return None
    try:
        resolved = Path(os.path.realpath(text))
    except (OSError, ValueError):
        return None
    if must_exist and not resolved.is_dir():
        return None
    return resolved


def _anchor(place: Path) -> Path | None:
    """The existing directory a not-yet-created ``place`` will be made in. It is not created here."""
    current = place
    while True:
        if current.is_dir():
            return current
        if current.exists():
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _administration(folder: Path) -> tuple[Path, Path] | None:
    """The git directory and its common directory, as ``validate`` finds them, or None when there is none."""
    top = V.git_work_tree(folder)
    gitdir = None
    if top is not None:
        marker = top / '.git'
        try:
            kind = marker.stat().st_mode
        except OSError:
            kind = None
        if kind is not None and stat.S_ISREG(kind):
            gitdir = V._gitfile(marker)
        elif kind is not None and V._is_git_directory(marker):
            gitdir = marker
    if gitdir is None and V._is_git_directory(folder):
        gitdir = folder
    if gitdir is None:
        return None
    common = V._common_dir(gitdir)
    if common is None:
        return None
    try:
        return Path(os.path.realpath(gitdir)), Path(os.path.realpath(common))
    except OSError:
        return None


def _lands_inside(name: str, forbidden) -> bool:
    try:
        parent = Path(os.path.realpath(name)).parent
    except OSError:
        return True
    return any(root is not None and _inside(root, parent) for root in forbidden)


def _unlink(name: str | None) -> None:
    if name is None:
        return
    try:
        os.unlink(name)
    except OSError:
        pass


def reflink_possible(directory, *, forbidden=()) -> bool:
    """Whether ``directory``'s filesystem can reflink one fresh file onto another. The files are removed."""
    src = dst = None
    src_fd = dst_fd = None
    try:
        src_fd, src = tempfile.mkstemp(prefix=_PROBE_PREFIX, dir=directory)
        os.write(src_fd, b'place')
        if _lands_inside(src, forbidden):
            return False
        dst_fd, dst = tempfile.mkstemp(prefix=_PROBE_PREFIX, dir=directory)
        if _lands_inside(dst, forbidden):
            return False
        import fcntl
        fcntl.ioctl(dst_fd, _FICLONE, src_fd)
        return True
    except (OSError, ImportError, AttributeError):
        return False
    finally:
        for fd in (src_fd, dst_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        _unlink(src)
        _unlink(dst)


def hardlink_possible(directory, *, forbidden=()) -> bool:
    """Whether ``directory``'s filesystem can hardlink a fresh file. The files are removed."""
    src = dst = None
    src_fd = None
    try:
        src_fd, src = tempfile.mkstemp(prefix=_PROBE_PREFIX, dir=directory)
        os.close(src_fd)
        src_fd = None
        if _lands_inside(src, forbidden):
            return False
        dst = src + '.link'
        os.link(src, dst)
        if _lands_inside(dst, forbidden):
            return False
        return True
    except OSError:
        return False
    finally:
        if src_fd is not None:
            try:
                os.close(src_fd)
            except OSError:
                pass
        _unlink(src)
        _unlink(dst)


def measure(repository, candidate, *, work_bytes: int, object_bytes: int) -> Facts | None:
    """The facts for one candidate, or None when it cannot be a place. See the module's text."""
    _count(work_bytes, 'work_bytes')
    _count(object_bytes, 'object_bytes')
    repo = _resolve(repository, must_exist=True)
    place = _resolve(candidate, must_exist=False)
    if repo is None or place is None:
        return None
    if place.exists() and not place.is_dir():
        return None
    admin = _administration(repo)
    common = None if admin is None else admin[1]
    if _overlaps(place, repo) or (common is not None and _overlaps(place, common)):
        return None
    anchor = _anchor(place)
    if anchor is None or _inside(repo, anchor) or (common is not None and _inside(common, anchor)):
        return None
    if not os.access(anchor, os.W_OK | os.X_OK):
        return None
    try:
        same = os.stat(repo).st_dev == os.stat(anchor).st_dev
        free = shutil.disk_usage(anchor).free
    except OSError:
        return None
    forbidden = (repo, common)
    return Facts(repository=repo, candidate=place, same_filesystem=same,
                 reflink=reflink_possible(anchor, forbidden=forbidden),
                 hardlink=hardlink_possible(anchor, forbidden=forbidden),
                 free_bytes=free, work_bytes=work_bytes, object_bytes=object_bytes, git_common_dir=common)


def _bind(facts: Facts, mechanism: str, *, writes: bool) -> Place:
    return Place(path=facts.candidate, mechanism=mechanism, engine_worktree_switching=ENGINE_WORKTREE_SWITCHING,
                 repository=facts.repository, git_common_dir=facts.git_common_dir, writes_git_common_dir=writes,
                 same_filesystem=facts.same_filesystem, reflink=facts.reflink, hardlink=facts.hardlink,
                 free_bytes=facts.free_bytes, work_bytes=facts.work_bytes, object_bytes=facts.object_bytes)


def pick(facts: Facts, *, linked: bool = False) -> Place | None:
    """The place ``facts`` support, or None. ``linked=True`` is the only way to get a linked worktree."""
    if not isinstance(facts, Facts):
        raise TypeError('facts must be Facts')
    if not isinstance(linked, bool):
        raise TypeError('linked is true or false')
    if _overlaps(facts.candidate, facts.repository):
        return None
    if facts.git_common_dir is not None and _overlaps(facts.candidate, facts.git_common_dir):
        return None
    if linked:
        if facts.git_common_dir is None or facts.free_bytes < facts.work_bytes + REFLINK_RESERVE:
            return None
        return _bind(facts, LINKED, writes=True)
    if (facts.same_filesystem and facts.reflink and facts.free_bytes >= REFLINK_RESERVE):
        return _bind(facts, 'reflink', writes=False)
    if (facts.same_filesystem and facts.hardlink
            and facts.free_bytes >= facts.work_bytes + REFLINK_RESERVE):
        return _bind(facts, 'hardlink', writes=False)
    if facts.free_bytes >= facts.work_bytes + facts.object_bytes + REFLINK_RESERVE:
        return _bind(facts, 'copy', writes=False)
    return None


def choose(repository, candidates, *, work_bytes: int, object_bytes: int, linked: bool = False) -> Place | None:
    """The place for ``repository`` among ``candidates``. See the module's text."""
    _count(work_bytes, 'work_bytes')
    _count(object_bytes, 'object_bytes')
    if not isinstance(linked, bool):
        raise TypeError('linked is true or false')
    best: Place | None = None
    best_cost: int | None = None
    for candidate in candidates:
        facts = measure(repository, candidate, work_bytes=work_bytes, object_bytes=object_bytes)
        if facts is None:
            continue
        chosen = pick(facts, linked=linked)
        if chosen is None:
            continue
        if linked:
            return chosen
        cost = _COST[chosen.mechanism]
        if best is None or cost < best_cost:
            best = chosen
            best_cost = cost
    return best
