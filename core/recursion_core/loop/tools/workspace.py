"""Where tools may look: the workspace, the spill folder, and what has been read this session.

A path a model gives is resolved against the workspace root and through every symbolic link; it is refused if
the real path is outside the root. The one exception is a spill reference (``spill:<sha256>``), which names a
file in the engine's own spill folder, so the model can page through an output that was too large to show.

The read ledger records the digest of every file the read tool read in this session. The edit tool (second
pull request) refuses a file that is not in the ledger or has changed since it was read.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

SPILL_PREFIX = "spill:"
_SPILL_RE = re.compile(r"^spill:([0-9a-f]{64})$")


class ToolError(Exception):
    """A tool refused its input. The message is shown to the model as the tool's error result."""


def check_private_dir(path: Path, what: str) -> None:
    """Refuse a folder that is a symbolic link, not ours, or open to other users (POSIX only)."""

    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ToolError(f"{what} is not a plain directory")
    if hasattr(os, "getuid"):
        if info.st_uid != os.getuid():
            raise ToolError(f"{what} is not owned by this user")
        if info.st_mode & 0o077:
            raise ToolError(f"{what} is open to other users (mode {stat.S_IMODE(info.st_mode):o})")


class SpillStore:
    """Content-addressed files for tool outputs that were too large to show.

    A file is named by the SHA-256 of its bytes (``<home>/spill/sha256/<aa>/<hex>``), written to a private
    temporary file opened with O_EXCL at mode 0600, flushed to disk, then linked into place without replacing
    anything, so a reader never sees half a file and the same output is stored once."""

    def __init__(self, home: Path) -> None:
        self.root = Path(home) / "spill"
        self._lock = threading.Lock()

    def _ensure(self) -> Path:
        objects = self.root / "sha256"
        for folder in (self.root, objects):
            try:
                os.mkdir(folder, 0o700)
            except FileExistsError:
                pass
            check_private_dir(folder, "the spill folder")
        return objects

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            objects = self._ensure()
            shard = objects / digest[:2]
            try:
                os.mkdir(shard, 0o700)
            except FileExistsError:
                pass
            check_private_dir(shard, "the spill folder")
            final = shard / digest
            if final.exists():
                return SPILL_PREFIX + digest
            tmp = shard / f".{digest}.{os.getpid()}.{threading.get_ident()}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, final)
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
            if hasattr(os, "O_DIRECTORY"):
                dfd = os.open(shard, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
        return SPILL_PREFIX + digest

    def path_for(self, ref: str) -> Path | None:
        match = _SPILL_RE.fullmatch(ref)
        if not match:
            return None
        digest = match.group(1)
        return self.root / "sha256" / digest[:2] / digest


@dataclass(frozen=True)
class ReadMark:
    sha256: str
    size: int
    mtime_ns: int


class ReadLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._marks: dict[str, ReadMark] = {}

    def record(self, path: Path, mark: ReadMark) -> None:
        with self._lock:
            self._marks[str(path)] = mark

    def get(self, path: Path) -> ReadMark | None:
        with self._lock:
            return self._marks.get(str(path))

    def forget(self, path: Path) -> None:
        with self._lock:
            self._marks.pop(str(path), None)


class Workspace:
    def __init__(self, root: Path, spill: SpillStore) -> None:
        real = os.path.realpath(root)
        if not os.path.isdir(real):
            raise ToolError("the workspace is not a directory")
        self.root = Path(real)
        self.spill = spill
        self.ledger = ReadLedger()

    def inside(self, real: str) -> bool:
        root = str(self.root)
        return real == root or real.startswith(root.rstrip(os.sep) + os.sep)

    def resolve(self, given: object) -> Path:
        """The real path for a path the model gave, or ToolError."""

        if not isinstance(given, str) or not given.strip():
            raise ToolError("path is empty")
        if "\x00" in given:
            raise ToolError("path contains a NUL character")
        if given.startswith(SPILL_PREFIX):
            spilled = self.spill.path_for(given)
            if spilled is None or not spilled.is_file():
                raise ToolError(f"no saved output is named {given}")
            return spilled
        candidate = Path(os.path.expanduser(given)) if given.startswith("~") else Path(given)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        real = os.path.realpath(candidate)
        if not self.inside(real):
            raise ToolError(f"{given} is outside the workspace ({self.root}); only files inside it can be used")
        return Path(real)

    def display(self, path: Path) -> str:
        text = str(path)
        root = str(self.root)
        if text == root:
            return "."
        if text.startswith(root.rstrip(os.sep) + os.sep):
            return text[len(root.rstrip(os.sep)) + 1:].replace(os.sep, "/")
        spill_root = str(self.spill.root / "sha256")
        if text.startswith(spill_root + os.sep):
            return SPILL_PREFIX + path.name
        return text
