"""Instruction files: AGENTS.md and CLAUDE.md from the repository's root down to the workspace, capped, listed.

As Codex walks them (the plan's competitor notes, row 26): from the workspace up to the folder holding
``.git`` (or only the workspace when there is none), then read from the root down, so the nearer file comes
later and can refine the farther one. In each folder AGENTS.md is read, then CLAUDE.md. All of them together
are capped at 32 KiB, Codex's shared limit; a file that would pass the cap is cut at a UTF-8 boundary and the
cut is said in the text and in the list. The cap bounds the reading too: a file is read only up to the room
that is left (its size comes from its metadata), and once the room is used up no file is read at all. A file
that is a link leading outside the repository is skipped.
The list (path, bytes, whether cut or skipped) goes on the ``ready`` line, so what the model was told is on
the record.

The contract records that always-on instruction files did not improve success and raised cost by 20 to 23%
(CONTRACT 3994-3995); they are read once, at start, into the stable part of the prompt (so the provider's
cache keeps them), and a run can turn them off (``--no-project-instructions``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

NAMES = ("AGENTS.md", "CLAUDE.md")
MAX_BYTES = 32 * 1024


@dataclass(frozen=True)
class Found:
    path: str  # relative to the repository root
    bytes: int
    cut: bool = False
    skipped: str | None = None

    def to_wire(self) -> dict:
        return {"path": self.path, "bytes": self.bytes, "cut": self.cut, "skipped": self.skipped}


def repository_root(start: Path) -> Path:
    here = start
    while True:
        if (here / ".git").exists():
            return here
        if here.parent == here:
            return start
        here = here.parent


def discover(workspace: Path, max_bytes: int = MAX_BYTES) -> tuple[str, list[Found]]:
    start = Path(os.path.realpath(workspace))
    top = repository_root(start)
    chain = [start]
    while chain[-1] != top:
        chain.append(chain[-1].parent)
    chain.reverse()
    parts: list[str] = []
    found: list[Found] = []
    used = 0
    for folder in chain:
        for name in NAMES:
            candidate = folder / name
            if not candidate.is_file():
                continue
            rel = os.path.relpath(candidate, top).replace(os.sep, "/")
            real = os.path.realpath(candidate)
            if not (real == str(top) or real.startswith(str(top).rstrip(os.sep) + os.sep)):
                found.append(Found(rel, 0, skipped="links outside the repository"))
                continue
            room = max_bytes - used
            try:
                # Never more than the room left (and one byte to tell that there is more); nothing at all once
                # the room is used up (astra R7, sol R6). The size comes from the file's metadata.
                data, size = _read_at_most(real, room + 1 if room > 0 else 0)
            except OSError as exc:
                found.append(Found(rel, 0, skipped=f"unreadable: {exc.strerror or exc}"))
                continue
            size = max(size, len(data))
            if room <= 0:
                found.append(Found(rel, size, skipped="over the size limit for instruction files"))
                continue
            cut = len(data) > room or size > room
            shown = data[:room].decode("utf-8", errors="ignore") if cut else data.decode("utf-8", errors="replace")
            used += len(shown.encode("utf-8"))
            note = f"\n[{rel} was cut at the {max_bytes // 1024} KiB limit for instruction files]" if cut else ""
            parts.append(f"## {rel}\n{shown}{note}")
            found.append(Found(rel, size, cut=cut))
    return "\n\n".join(parts), found


def _read_at_most(path: str, limit: int) -> tuple[bytes, int]:
    """At most ``limit`` bytes from the start of the file, and its size; with ``limit`` 0 the file is opened
    for its size only and nothing is read."""

    with open(path, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        return (f.read(limit) if limit > 0 else b""), size
