"""What write and edit share: where they may write, whether a file is as it was read, and how it is replaced.

- A path to write resolves like a path to read (through links, inside the workspace), but a spill reference
  is refused, and so is any path with a ``.git`` folder in it: a file there (a hook, the config) runs code
  the next time anyone uses git in the repository.
- An existing file is changed only if this session read it and it is unchanged since (vault record
  20261028-recursion-frontier-dr-tool-editing-03: read before edit; the record's typed names are used:
  ``not_read``, ``stale_snapshot``). Its size and SHA-256 must match the read ledger (a new modification
  time alone, with the same bytes, is not a change).
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


def resolve_writable(ws: Workspace, given: object) -> Path:
    if isinstance(given, str) and given.startswith(SPILL_PREFIX):
        raise ToolError("a saved output cannot be written")
    path = ws.resolve(given)
    rel = Path(ws.display(path))
    if ".git" in rel.parts:
        raise ToolError(f"{ws.display(path)} is inside a .git folder; it is not written")
    if path.is_dir():
        raise ToolError(f"{ws.display(path)} is a folder")
    return path


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
        raise ToolError(f"stale_snapshot: {shown} no longer exists; read the folder again") from None
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
