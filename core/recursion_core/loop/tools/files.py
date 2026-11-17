"""What write and edit share: where they may write, whether a file is as it was read, and how it is replaced.

- A path to write resolves like a path to read (through links, inside the workspace), but a spill reference
  is refused, and so is any path with a ``.git`` folder in it: a file there (a hook, the config) runs code
  the next time anyone uses git in the repository. Both are refused by where the path leads, not only by how
  it is spelt: ``.git`` in any case (a case-insensitive drive takes ``.GIT`` for ``.git``), a folder on the
  way that is the repository's ``.git`` or the spill folder under another name, and any path inside the spill
  folder (the engine home may lie inside the workspace; a saved output is named by its bytes and must not
  change under its name).
- An existing file is changed only if this session read it and it is unchanged since (vault record
  20261028-recursion-frontier-dr-tool-editing-03: read before edit; the record's typed names are used:
  ``not_read``, ``stale_snapshot``). Its size and SHA-256 must match the read ledger (a new modification
  time alone, with the same bytes, is not a change). A file this session read that is gone when it is to be
  changed is ``stale_snapshot`` too, not a new file (sol R3); the read is forgotten then, so a later write,
  asked for again, creates it.
- The new bytes go to a private temporary file in the same folder, are flushed to disk, keep the old
  file's permission bits, and replace the file in one rename; the ledger then records the new bytes, so the
  next edit needs no fresh read. A reader never sees half a file.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from recursion_core.loop.tools.workspace import SPILL_PREFIX, ReadMark, ToolError, Workspace


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def _git_folders(ws: Workspace) -> list[Path]:
    """The repository's ``.git`` folder (the nearest one at or above the workspace root), if there is one."""

    here = ws.root
    while True:
        candidate = here / ".git"
        if candidate.is_dir():
            return [candidate]
        if candidate.exists() or here.parent == here:
            return []
        here = here.parent


def _inside(path: Path, folder: str) -> bool:
    text = str(path)
    return text == folder or text.startswith(folder.rstrip(os.sep) + os.sep)


def resolve_writable(ws: Workspace, given: object) -> Path:
    if isinstance(given, str) and given.startswith(SPILL_PREFIX):
        raise ToolError("a saved output cannot be written")
    path = ws.resolve(given)
    shown = ws.display(path)
    if _inside(path, os.path.realpath(ws.spill.root)):
        raise ToolError(f"{shown} is a saved output's folder; it is not written")
    if ".git" in (part.casefold() for part in Path(shown).parts):
        raise ToolError(f"{shown} is inside a .git folder; it is not written")
    guarded = {i for i in (_identity(f) for f in [ws.spill.root, *_git_folders(ws)]) if i is not None}
    for folder in (path, *path.parents):
        if not ws.inside(str(folder)):
            break
        if _identity(folder) in guarded:
            raise ToolError(f"{shown} is inside a .git folder or a saved output's folder; it is not written")
    if path.is_dir():
        raise ToolError(f"{shown} is a folder")
    return path


def request_facts(ws: Workspace, given: str, path: Path) -> dict[str, str] | None:
    """What a permission request shows beside the model's input when the path leads elsewhere (a link): the file
    that would change, relative to the workspace (DeepSeek F4). None when the path is the file itself."""

    given = os.path.expanduser(given) if given.startswith("~") else given
    lexical = Path(os.path.normpath(given if os.path.isabs(given) else ws.root / given))
    if lexical == path:
        return None
    return {"resolved_path": ws.display(path)}


def keep_approved(ctx, given: str, path: Path) -> None:
    """In the run of an approved call: the file the path leads to now must be the one the request showed (the same
    ``resolved_path``, or none when the path was the file itself); otherwise nothing is changed (sol 1).

    A context that has no approved-facts record at all is refused: there is nothing to check it against, so
    nothing is changed. ``UNBOUND`` (a tool run outside dispatch, which the registry documents as skipping
    these rules) still changes nothing here and does not raise."""

    from recursion_core.loop.tools.registry import CHANGED_SINCE_APPROVAL, UNBOUND

    try:
        approved = ctx.approved_facts
    except AttributeError:
        raise ToolError("refused: this call has no record of what was approved") from None
    if approved is UNBOUND:
        return
    if request_facts(ctx.workspace, given, path) != (approved or None):
        raise ToolError(CHANGED_SINCE_APPROVAL)


def mark_of(data: bytes, info: os.stat_result) -> ReadMark:
    return ReadMark(hashlib.sha256(data).hexdigest(), len(data), info.st_mtime_ns)


def read_fresh(ws: Workspace, path: Path) -> bytes:
    """The file's bytes, if this session read it and it has not changed since. Otherwise ToolError."""

    shown = ws.display(path)
    mark = ws.ledger.get(path)
    if mark is None:
        raise ToolError(f"not_read: read {shown} with the read tool before changing it")
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode):
            raise ToolError(f"{shown} is not a regular file")
        data = path.read_bytes()
    except FileNotFoundError:
        ws.ledger.forget(path)  # told once; a later write is a new file and is asked for again
        raise ToolError(f"stale_snapshot: {shown} was read in this session and no longer exists; it was not "
                        f"written (to create it again, write it again)") from None
    current = mark_of(data, info)
    if (current.sha256, current.size) != (mark.sha256, mark.size):
        raise ToolError(f"stale_snapshot: {shown} changed since it was read; read it again before changing it")
    return data


def atomic_write(ws: Workspace, path: Path, data: bytes) -> None:
    folder = path.parent
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        mode = None
    tmp = folder / f".{path.name}.recursion-loop.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        if hasattr(os, "fchmod"):
            if mode is None:  # a new file gets the mode the user's umask gives any new file
                umask = os.umask(0)
                os.umask(umask)
                mode = 0o666 & ~umask
            os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    ws.ledger.record(path, mark_of(data, os.stat(path)))
