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
    """A relative POSIX path inside the archive: no absolute path, no '..', no empty part.

    A colon at index 1 of the first component is a drive letter (``C:/...``). That form is absolute
    on Windows and would leave the work tree in a Windows join. A colon later in the name is an
    ordinary POSIX character (``docs/v1:2/notes.md``). NUL and the other ASCII controls are refused
    here; a NUL would otherwise fail later as an untyped path error.
    """
    if (not isinstance(name, str) or not name or name.startswith("/") or "\\" in name
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise TxtarError(f"bad file name {name!r}")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TxtarError(f"bad file name {name!r}")
    first = parts[0]
    if len(first) > 1 and first[1] == ":":
        raise TxtarError(f"bad file name {name!r}")
    if posixpath.normpath(name) != name:
        raise TxtarError(f"bad file name {name!r}")
    return name


def _newline_lines(text: str):
    """Lines split on ``\\n`` only, keeping that ending.

    ``str.splitlines`` also splits on ``\\r`` and on U+2028. A carriage return would hide a marker,
    and U+2028 inside a body would start a false header. Bodies keep the endings they arrived with.
    """
    start = 0
    while start <= len(text):
        found = text.find("\n", start)
        if found < 0:
            if start < len(text):
                yield text[start:]
            break
        yield text[start:found + 1]
        start = found + 1


def _refuse_bare_carriage_return(text: str) -> None:
    """Refuse a carriage return that is not the first half of CRLF.

    Line endings here are LF or CRLF. A classic-Mac archive (CR only) is one long line if it is
    split on LF, so the file map is empty and the caller cannot tell it from an empty archive.
    """
    start = 0
    while True:
        found = text.find("\r", start)
        if found < 0:
            return
        if found + 1 == len(text) or text[found + 1] != "\n":
            raise TxtarError("archive has a carriage return that is not part of CRLF")
        start = found + 2


def parse(text: str) -> tuple:
    """Return (comment, {name: content}) in file order. Content keeps its line endings."""
    _refuse_bare_carriage_return(text)
    comment: list = []
    files: dict = {}
    current = None
    for line in _newline_lines(text):
        match = _MARKER.match(line.rstrip("\r\n"))
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
        # A body that already ends in CR or LF is not given another LF. Appending LF after a bare
        # CR would change the bytes, and therefore the digest, of a task that is written back.
        if content and not content.endswith(("\n", "\r")):
            content += "\n"
        out.append(content)
    return "".join(out)
