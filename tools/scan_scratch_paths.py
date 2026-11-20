#!/usr/bin/env python3
"""Scratch-path scan: report lines that name a builder's scratch folder by path.

A builder runs its tests with TMPDIR set to a scratch folder; that folder's path
must never be written into a file of the repository. This script takes file
paths on its command line and prints ``file:line`` for every line that holds the
path form of such a folder (under /var/tmp or /tmp, named after the engine). A
bare engine name such as an engine id is not a hit; only the path form is, and
only where a path starts: a tmp folder nested inside another path (a
repository's own tmp folder, a nested var/tmp) is not a hit. A path also starts
right after a written escape of a line break, carriage return or tab (a
backslash and n, r or t, as JSON and logs write them) and right after the host
of a file URL (file://localhost, file://<host>).

A line that carries the marker ``scratch-path: allowed`` is not reported.

    python -X utf8 -B tools/scan_scratch_paths.py <file> [<file> ...]

Exit 0 when no line is reported, 1 when any line is, 2 when a file cannot be
read. It reads only the files it is given; it does not walk the repository.
"""
from __future__ import annotations

import re
import sys

# Built from pieces so that this file holds none of the forms it looks for.
_ENGINE = "grok"
FORMS = (
    "/" + "var" + "/" + "tmp" + "/" + _ENGINE + "-",
    "/" + "tmp" + "/" + _ENGINE + "-",
)
MARKER = "scratch-path: " + "allowed"
# A form counts only where a path starts: at the line start, or after a
# character that cannot be inside a path name (a quote, a space, "=", ":" and
# the like). After a letter, digit, "_", ".", "~" or "-" the tmp folder sits
# inside another path (a repository's own tmp, a nested var/tmp) and is no hit.
_PATTERN = re.compile(r"(?<![\w.~-])(?:" + "|".join(re.escape(form) for form in FORMS) + ")")
# Where the character before a form is one of those, the form still starts a path
# when the text before it ends in a written escape (an odd run of backslashes
# then n, r or t) or in the scheme and host of a file URL.
_FORM = re.compile("|".join(re.escape(form) for form in FORMS))
_ESCAPED_SPACE = re.compile(r"(?<!\\)(?:\\\\)*\\[nrt]$")
_FILE_HOST = re.compile(r"(?:^|[^\w+.-])file://[^/\s]*$", re.IGNORECASE)


def _names_scratch_path(line: str) -> bool:
    if _PATTERN.search(line):
        return True
    for match in _FORM.finditer(line):
        before = line[:match.start()]
        if _ESCAPED_SPACE.search(before) or _FILE_HOST.search(before):
            return True
    return False


def hit_lines(text: str) -> list[int]:
    """Return the 1-based numbers of the lines that name a scratch path."""
    out = []
    for number, line in enumerate(text.split("\n"), start=1):
        if MARKER in line:
            continue
        if _names_scratch_path(line):
            out.append(number)
    return out


def main(argv: list[str]) -> int:
    found = False
    unreadable = False
    for path in argv:
        try:
            with open(path, "rb") as handle:
                text = handle.read().decode("utf-8", "replace")
        except OSError as exc:
            print(f"{path}: cannot read: {exc.strerror or exc}", file=sys.stderr)
            unreadable = True
            continue
        for number in hit_lines(text):
            print(f"{path}:{number}")
            found = True
    if unreadable:
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
