"""A plain-text archive of several files (the txtar layout): a comment, then ``-- name --`` sections.

The dev set keeps each task in one such file. Keeping the task's files inside one ``.txtar`` file
matters here: the repository's frozen gate fails on any file named like a test (``test_*.py``) that
lies outside its own globs, and a task's hidden tests are files with exactly such names.
"""
from __future__ import annotations

import posixpath
import re

_MARKER = re.compile(r"^-- (.+) --$")


class TxtarError(ValueError):
    pass


def check_name(name: str) -> str:
    """A relative POSIX path inside the archive: no absolute path, no '..', no empty part."""
    if not name or name.startswith("/") or "\\" in name:
        raise TxtarError(f"bad file name {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TxtarError(f"bad file name {name!r}")
    if posixpath.normpath(name) != name:
        raise TxtarError(f"bad file name {name!r}")
    return name


def parse(text: str) -> tuple:
    """Return (comment, {name: content}) in file order. Content keeps its line endings."""
    comment: list = []
    files: dict = {}
    current = None
    for line in text.splitlines(keepends=True):
        match = _MARKER.match(line.rstrip("\n"))
        if match:
            current = check_name(match.group(1))
            if current in files:
                raise TxtarError(f"duplicate file {current!r}")
            files[current] = []
            continue
        if current is None:
            comment.append(line)
        else:
            files[current].append(line)
    return "".join(comment), {name: "".join(lines) for name, lines in files.items()}


def format(comment: str, files: dict) -> str:  # noqa: A001 - mirrors parse
    out = [comment if comment.endswith("\n") or not comment else comment + "\n"]
    for name, content in files.items():
        check_name(name)
        out.append(f"-- {name} --\n")
        if content and not content.endswith("\n"):
            content += "\n"
        out.append(content)
    return "".join(out)
